"""Recorded, sanitized DeFiLlama responses (subsets of api.llama.fi, 2026-10-11).

- `hacks.json`: 13 real `/hacks` rows, including the 2021 Compound V2 incident
  (matched by id), the 2024 Dolomite incident (no id, matched by name), Venus
  incidents recorded under two names, Euler V1 (a protocol now below the TVL
  floor), the 2024 LeadBlock Morpho Blue market incident, incidents outside the
  observation window, a row without an amount, and a CEX incident.
- `protocols.json`: 10 real `/protocols` rows reduced to the fields Risk0 reads
  (descriptions replaced; `chainTvls` limited to a few keys).
- `chains.json`: 3 real `/v2/chains` rows.
"""

import json
from pathlib import Path

import httpx

from app.adapters.defillama import DefiLlamaClient


DIRECTORY = Path(__file__).parent
PATHS = {"/hacks": "hacks.json", "/protocols": "protocols.json", "/v2/chains": "chains.json"}


class FakeDefiLlamaApi:
    def __init__(self) -> None:
        self.bodies = {path: (DIRECTORY / name).read_bytes() for path, name in PATHS.items()}
        self.mode = "ok"
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.mode == "down":
            raise httpx.ConnectError("simulated outage with https://secret.example/key", request=request)
        if self.mode == "error":
            return httpx.Response(500, text="internal secret-token-123 stack trace")
        body = self.bodies.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=body)

    def client(self, clock, cache=None) -> DefiLlamaClient:
        return DefiLlamaClient(
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            cache=cache,
            clock=clock,
            sleep=lambda _: None,
        )


def raw(name: str) -> list:
    return json.loads((DIRECTORY / name).read_text())
