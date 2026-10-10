"""Sanitized, synthetic Morpho GraphQL responses for radar discovery and collectors.

Every address and amount below is invented; shapes mirror the public Morpho API
(`chains`, `markets`, `vaults`, `vaultV2s`, `marketPositions`), including its
integer-or-string BigInt values and binary-float USD figures.
"""

import copy
import json
from decimal import Decimal

import httpx


BASE_URL = "https://api.morpho.org"


def address(digit: str) -> str:
    return "0x" + digit * 40


def market_key(digit: str) -> str:
    return "0x" + digit * 64


WETH = {"address": address("e"), "symbol": "WETH", "decimals": 18, "price": {"usd": 2500.5}}
USDC = {"address": address("c"), "symbol": "USDC", "decimals": 6, "price": {"usd": 0.9998}}
WSTETH = {"address": address("d"), "symbol": "wstETH", "decimals": 18, "price": {"usd": 3000.25}}
BASE_USDC = {"address": address("b"), "symbol": "USDC", "decimals": 6, "price": {"usd": 1.0001}}
BASE_WETH = {"address": address("a"), "symbol": "WETH", "decimals": 18, "price": {"usd": 2501}}

ORACLE_WETH_USDC = address("4")
ORACLE_WSTETH_WETH = address("5")
IRM = address("6")

M_WETH_USDC = market_key("1")
M_WSTETH_WETH = market_key("2")
M_IDLE_USDC = market_key("3")
M_BASE = market_key("4")

CATALOG_VAULT = address("1")
NEIGHBOR_VAULT = address("2")
V2_VAULT = address("3")
BASE_VAULT = address("9")


def _market(
    market_id: str,
    loan: dict,
    collateral: dict | None,
    oracle: str | None,
    lltv: str,
    *,
    supply: int,
    borrow: int,
    collateral_assets: int,
    price: str | None,
    timestamp: int = 1791633000,
) -> dict:
    return {
        "marketId": market_id,
        "listed": True,
        "lltv": lltv,
        "irmAddress": IRM if collateral else "0x0000000000000000000000000000000000000000",
        "oracle": {"address": oracle, "type": "ChainlinkOracleV2"} if oracle else None,
        "loanAsset": copy.deepcopy(loan),
        "collateralAsset": copy.deepcopy(collateral),
        "badDebt": {"underlying": 0, "usd": 0},
        "state": {
            "blockNumber": 26163000,
            "timestamp": timestamp,
            "supplyAssets": supply,
            "borrowAssets": borrow,
            "liquidityAssets": supply - borrow,
            "collateralAssets": collateral_assets,
            "supplyAssetsUsd": 1000.5,
            "borrowAssetsUsd": 800.25,
            "liquidityAssetsUsd": 200.25,
            "collateralAssetsUsd": 1500.75,
            "utilization": 0.8,
            "price": price,
        },
    }


def default_world() -> dict:
    return {
        "chains": [{"id": 1, "network": "Ethereum"}, {"id": 8453, "network": "Base"}],
        "markets": {
            1: [
                _market(
                    M_WETH_USDC, USDC, WETH, ORACLE_WETH_USDC, "860000000000000000",
                    supply=1_000_000_000, borrow=800_000_000,
                    collateral_assets=500_000_000_000_000_000,
                    # 2500 USDC per WETH scaled by 1e(36 + 6 - 18).
                    price="2500000000000000000000000000",
                ),
                _market(
                    M_WSTETH_WETH, WETH, WSTETH, ORACLE_WSTETH_WETH, 965000000000000000,
                    supply=10**21, borrow=5 * 10**20, collateral_assets=10**21,
                    price="1200000000000000000000000000000000000",
                ),
                _market(
                    M_IDLE_USDC, USDC, None, None, 0,
                    supply=50_000_000, borrow=0, collateral_assets=0, price=None,
                ),
            ],
            8453: [
                _market(
                    M_BASE, BASE_USDC, BASE_WETH, address("7"), "860000000000000000",
                    supply=2_000_000, borrow=1_000_000, collateral_assets=10**18,
                    price="2501000000000000000000000000",
                ),
            ],
        },
        "vaults": {
            1: [
                {
                    "address": CATALOG_VAULT,
                    "name": "Catalog WETH",
                    "symbol": "cWETH",
                    "listed": True,
                    "asset": copy.deepcopy(WETH),
                    "liquidity": {"underlying": "400000000000000000000", "usd": 1000000.5},
                    "state": {
                        "blockNumber": 26163000,
                        "timestamp": 1791633000,
                        "curator": address("8"),
                        "totalAssets": "1000000000000000000000",
                        "totalAssetsUsd": 2500500.25,
                        "allocation": [
                            {
                                "supplyAssets": "600000000000000000000",
                                "supplyAssetsUsd": 1500300.1,
                                "supplyCap": "1000000000000000000000",
                                "market": {"marketId": M_WSTETH_WETH},
                            },
                            {
                                "supplyAssets": "400000000000000000000",
                                "supplyAssetsUsd": 1000200.1,
                                "supplyCap": None,
                                "market": {"marketId": M_IDLE_USDC},
                            },
                        ],
                    },
                },
                {
                    "address": NEIGHBOR_VAULT,
                    "name": "Neighbor WETH",
                    "symbol": "nWETH",
                    "listed": False,
                    "asset": copy.deepcopy(WETH),
                    "liquidity": {"underlying": 0, "usd": 0},
                    "state": {
                        "blockNumber": 26163000,
                        "timestamp": 1791633000,
                        "curator": address("8"),
                        "totalAssets": 0,
                        "totalAssetsUsd": 0,
                        "allocation": [
                            {
                                "supplyAssets": 0,
                                "supplyAssetsUsd": 0,
                                "supplyCap": 0,
                                "market": {"marketId": M_WSTETH_WETH},
                            }
                        ],
                    },
                },
            ],
            8453: [],
        },
        "vaultV2s": {
            1: [
                {
                    "address": V2_VAULT,
                    "name": "Vault V2 USDC",
                    "symbol": "v2USDC",
                    "listed": True,
                    "asset": copy.deepcopy(USDC),
                    "curator": {"address": address("8")},
                    "totalAssets": 3_000_000,
                    "totalAssetsUsd": 2.9994,
                    "liquidity": 1_000_000,
                    "liquidityUsd": 0.9998,
                    "idleAssets": 500_000,
                    "adapters": {
                        "items": [
                            {
                                "address": address("f"),
                                "type": "MorphoMarketV1",
                                "assets": 2_000_000,
                                "positions": {
                                    "items": [
                                        {
                                            "market": {"marketId": M_WETH_USDC},
                                            "state": {
                                                "supplyAssets": 1_500_000,
                                                "supplyAssetsUsd": 1.4997,
                                            },
                                        }
                                    ]
                                },
                            },
                            {
                                "address": address("0"),
                                "type": "MetaMorpho",
                                "assets": 500_000,
                                "metaMorpho": {"address": NEIGHBOR_VAULT},
                            },
                        ]
                    },
                }
            ],
            8453: [
                {
                    "address": BASE_VAULT,
                    "name": "Base USDC",
                    "symbol": "bUSDC",
                    "listed": True,
                    "asset": copy.deepcopy(BASE_USDC),
                    "curator": None,
                    "totalAssets": 2_000_000,
                    "totalAssetsUsd": 2.0002,
                    "liquidity": 1_000_000,
                    "liquidityUsd": 1.0001,
                    "idleAssets": 0,
                    "adapters": {
                        "items": [
                            {
                                "address": address("f"),
                                "type": "MorphoMarketV1",
                                "assets": 2_000_000,
                                "positions": {
                                    "items": [
                                        {
                                            "market": {"marketId": M_BASE},
                                            "state": {
                                                "supplyAssets": 2_000_000,
                                                "supplyAssetsUsd": 2.0002,
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            ],
        },
        "marketPositions": {
            1: [
                _position(M_WSTETH_WETH, address("a"), supply="600000000000000000000"),
                _position(M_WSTETH_WETH, address("b"), supply="400000000000000000000"),
                _position(
                    M_WSTETH_WETH, address("c"),
                    borrow="400000000000000000000", collateral="350000000000000000000",
                    health_factor=1.02,
                ),
                _position(
                    M_WSTETH_WETH, address("d"),
                    borrow="100000000000000000000", collateral="200000000000000000000",
                    health_factor=2.5,
                ),
            ],
            8453: [],
        },
    }


def _position(
    market_id: str,
    user: str,
    *,
    supply: str | int = 0,
    borrow: str | int = 0,
    collateral: str | int = 0,
    health_factor: float | None = None,
) -> dict:
    return {
        "healthFactor": health_factor,
        "market": {
            "marketId": market_id,
            "loanAsset": {"decimals": 18},
            "collateralAsset": {"decimals": 18},
        },
        "user": {"address": user},
        "state": {"supplyAssets": supply, "borrowAssets": borrow, "collateral": collateral},
    }


class FakeMorphoGraphql:
    """Serves the synthetic world with real pagination and filtering semantics."""

    def __init__(self, world: dict | None = None) -> None:
        self.world = world if world is not None else default_world()
        self.requests: list[dict] = []
        self.failing_chains: set[int] = set()
        self.failing_roots: set[str] = set()
        # Requests whose id filter names any of these fail like an oversized
        # filter does upstream.
        self.failing_ids: set[str] = set()
        self.mode = "ok"

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("simulated outage", request=request)
        if request.url.path != "/graphql":
            return httpx.Response(404, json={"message": "not found"})
        body = json.loads(request.content, parse_float=Decimal)
        self.requests.append(body)
        query, variables = body["query"], body.get("variables") or {}
        root = next(
            name
            for name in ("marketPositions", "vaultV2s", "vaults", "markets", "chains")
            if f"{name}(" in query or f"{name} {{" in query
        )
        chain_ids = variables.get("chainIds") or []
        if root in self.failing_roots or any(c in self.failing_chains for c in chain_ids):
            return httpx.Response(503, json={"message": "unavailable"})
        filtered = (variables.get("marketIds") or []) + (variables.get("addresses") or [])
        if root != "marketPositions" and self.failing_ids.intersection(filtered):
            return httpx.Response(413, json={"message": "payload too large"})
        if root == "chains":
            return httpx.Response(200, json={"data": {"chains": self.world["chains"]}})
        items = [
            item
            for chain_id in chain_ids
            for item in self.world[root].get(chain_id, [])
            if _matches(root, item, variables)
        ]
        first, skip = variables["first"], variables["skip"]
        page = items[skip : skip + first]
        payload = {
            "items": page,
            "pageInfo": {
                "count": len(page),
                "countTotal": len(items),
                "limit": first,
                "skip": skip,
            },
        }
        return httpx.Response(
            200, content=json.dumps({"data": {root: payload}}, default=str)
        )

    def requests_for(self, root: str) -> list[dict]:
        return [r for r in self.requests if f"{root}(" in r["query"]]


def _matches(root: str, item: dict, variables: dict) -> bool:
    if root == "markets" and variables.get("marketIds") is not None:
        return item["marketId"] in variables["marketIds"]
    if root in ("vaults", "vaultV2s") and variables.get("addresses") is not None:
        return item["address"] in variables["addresses"]
    if root == "marketPositions":
        return item["market"]["marketId"] in variables["marketIds"]
    return True
