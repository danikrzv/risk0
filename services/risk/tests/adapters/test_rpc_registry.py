import json

import httpx
import pytest


SECRET = "s3cr3t-key-123"
KEYED_PATH = f"https://eth-mainnet.example-rpc.io/v2/{SECRET}"
KEYED_QUERY = f"https://rpc.example-two.io/?apikey={SECRET}"
KEYED_USERINFO = f"https://user:{SECRET}@rpc.example-three.io/"


class FakeRpc:
    """JSON-RPC endpoints keyed by host, each scripted with a behavior."""

    def __init__(self, behaviors: dict[str, str], chain_id: int = 1) -> None:
        self.behaviors = behaviors
        self.chain_id = chain_id
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = json.loads(request.content)
        self.calls.append((host, body["method"]))
        behavior = self.behaviors.get(host, "ok")
        if behavior == "down":
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        if behavior == "503":
            return httpx.Response(503, text=f"upstream error for {request.url}")
        if behavior == "rpc-error":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": f"bad key {SECRET}"}},
            )
        if behavior == "garbage":
            return httpx.Response(200, text="<html>not json</html>")
        if body["method"] == "eth_chainId":
            chain_id = 8453 if behavior == "wrong-chain" else self.chain_id
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(chain_id)}
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x10"}
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def registry(urls, fake: FakeRpc):
    from app.adapters.rpc_registry import RpcRegistry

    return RpcRegistry(urls, http_client=fake.client())


def test_environment_urls_are_ordered_before_legacy_and_public_defaults():
    from app.config import PUBLIC_RPC_URLS, RpcSettings

    settings = RpcSettings.from_environment(
        {
            "RISK0_RPC_1_URLS": f" {KEYED_PATH} , {KEYED_QUERY},,",
            "ETHEREUM_RPC_URL": KEYED_PATH,
            "RISK0_RPC_42_URLS": "https://rpc.chain-42.example",
            "UNRELATED": "https://ignored.example",
        }
    )

    assert settings.urls[1][:2] == (KEYED_PATH, KEYED_QUERY)
    assert settings.urls[1][2:] == PUBLIC_RPC_URLS[1]
    assert settings.urls[42] == ("https://rpc.chain-42.example",)
    assert settings.urls[8453] == PUBLIC_RPC_URLS[8453]
    assert SECRET not in repr(settings)


def test_public_rpc_defaults_carry_no_credentials():
    from app.config import PUBLIC_RPC_URLS

    for urls in PUBLIC_RPC_URLS.values():
        for url in urls:
            parsed = httpx.URL(url)
            assert parsed.scheme == "https"
            assert not parsed.userinfo
            assert not parsed.query
            assert parsed.path in ("", "/", "/evm", "/rpc", "/public")


def test_invalid_rpc_url_is_rejected_without_echoing_it():
    from app.config import RpcSettings

    with pytest.raises(ValueError) as error:
        RpcSettings.from_environment({"RISK0_RPC_1_URLS": f"ftp://{SECRET}.example"})

    assert "RISK0_RPC_1_URLS" in str(error.value)
    assert SECRET not in str(error.value)


def test_provider_labels_are_redacted():
    fake = FakeRpc({})
    providers = registry({1: (KEYED_PATH, KEYED_QUERY, KEYED_USERINFO)}, fake).providers(1)

    assert [provider.label for provider in providers] == [
        "https://eth-mainnet.example-rpc.io",
        "https://rpc.example-two.io",
        "https://rpc.example-three.io",
    ]
    assert all(SECRET not in repr(provider) for provider in providers)
    assert registry({}, fake).providers(1) == ()


def test_duplicate_hosts_get_distinct_labels():
    fake = FakeRpc({})
    providers = registry(
        {1: ("https://rpc.example.io/v2/a", "https://rpc.example.io/v2/b")}, fake
    ).providers(1)

    assert [provider.label for provider in providers] == [
        "https://rpc.example.io",
        "https://rpc.example.io#2",
    ]


@pytest.mark.parametrize("failure", ["down", "503", "rpc-error", "garbage", "wrong-chain"])
def test_call_falls_back_to_the_next_provider_in_order(failure):
    fake = FakeRpc({"eth-mainnet.example-rpc.io": failure})

    result = registry({1: (KEYED_PATH, KEYED_QUERY)}, fake).call(1, "eth_blockNumber", [])

    assert result.result == "0x10"
    assert result.chain_id == 1
    assert result.provider == "https://rpc.example-two.io"
    assert len(result.failures) == 1
    assert result.failures[0].startswith("https://eth-mainnet.example-rpc.io: ")
    assert SECRET not in " ".join(result.failures)


def test_chain_id_is_verified_once_per_provider():
    fake = FakeRpc({})
    rpc = registry({1: (KEYED_PATH,)}, fake)

    rpc.call(1, "eth_blockNumber", [])
    rpc.call(1, "eth_gasPrice", [])

    assert [method for _, method in fake.calls] == [
        "eth_chainId",
        "eth_blockNumber",
        "eth_gasPrice",
    ]


def test_total_failure_raises_a_redacted_unavailable_error():
    from app.adapters.rpc_registry import RpcUnavailableError

    fake = FakeRpc(
        {
            "eth-mainnet.example-rpc.io": "down",
            "rpc.example-two.io": "503",
            "rpc.example-three.io": "rpc-error",
        }
    )

    with pytest.raises(RpcUnavailableError) as error:
        registry({1: (KEYED_PATH, KEYED_QUERY, KEYED_USERINFO)}, fake).call(
            1, "eth_blockNumber", []
        )

    assert error.value.chain_id == 1
    assert error.value.method == "eth_blockNumber"
    assert len(error.value.failures) == 3
    rendered = f"{error.value} {error.value!r} {error.value.failures} {error.value.__cause__}"
    assert SECRET not in rendered
    assert "user:" not in rendered
    assert error.value.__cause__ is None


def test_chain_without_providers_is_unavailable():
    from app.adapters.rpc_registry import RpcUnavailableError

    with pytest.raises(RpcUnavailableError) as error:
        registry({1: (KEYED_PATH,)}, FakeRpc({})).call(10, "eth_blockNumber", [])

    assert error.value.chain_id == 10
    assert error.value.failures == ()


def test_registry_reads_the_environment():
    from app.adapters.rpc_registry import RpcRegistry

    rpc = RpcRegistry.from_environment(
        {"RISK0_RPC_1_URLS": KEYED_PATH}, http_client=FakeRpc({}).client()
    )

    assert rpc.providers(1)[0].label == "https://eth-mainnet.example-rpc.io"
    assert 8453 in rpc.chain_ids()
    assert SECRET not in repr(rpc)
