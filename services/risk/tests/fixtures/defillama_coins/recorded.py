"""Recorded DeFiLlama coins chart responses (coins.llama.fi, 2026-10-11), unmodified.

- `eurcv-2h-1785600000.json`: `/chart/ethereum:<EURCV>?start=1785600000&span=500&period=2h`,
  499 points from 2026-08-01 to 2026-09-12 (1.1499-1.1716 USD, confidence 0.99,
  one 4-hour gap). The first point is 419 s before `start`: DeFiLlama returns
  the price nearest each step.
- `europ-2h-1756800000.json`: the same chunk grid for EUROP when it was new (43
  points from 2025-09-11, gaps of up to 21 days).

`FakeCoinsApi` serves those bodies for their exact request and, for any other
chunk start, either an empty `{"coins": {}}` (a token DeFiLlama has no price for
then) or a synthetic flat series when `synthetic_price` is set.
"""

import json
from decimal import Decimal
from pathlib import Path

import httpx

from app.adapters.defillama_coins import DefiLlamaCoinsClient


DIRECTORY = Path(__file__).parent
EURCV = "0x5F7827FDeb7c20b443265Fc2F40845B715385Ff2"
EUROP = "0x888883b5F5D21fb10Dfeb70e8f9722B9FB0E5E51"
RECORDED = {
    (f"ethereum:{EURCV.lower()}", 1785600000): "eurcv-2h-1785600000.json",
    (f"ethereum:{EUROP.lower()}", 1756800000): "europ-2h-1756800000.json",
}
PERIOD_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}


class FakeCoinsApi:
    def __init__(self, *, synthetic_price: str | None = None, now: int | None = None) -> None:
        self.bodies = {key: (DIRECTORY / name).read_bytes() for key, name in RECORDED.items()}
        self.synthetic_price = synthetic_price
        self.now = now
        self.mode = "ok"
        self.requests: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if self.mode == "down":
            raise httpx.ConnectError("simulated outage with https://secret.example/key", request=request)
        if self.mode == "error":
            return httpx.Response(500, text="internal secret-token-123 stack trace")
        if self.mode == "bad-request":
            return httpx.Response(400, text="span too large secret-token-123")
        coin = request.url.path.removeprefix("/chart/")
        start = int(request.url.params["start"])
        body = self.bodies.get((coin, start))
        if body is not None:
            return httpx.Response(200, content=body)
        if self.synthetic_price is None:
            return httpx.Response(200, json={"coins": {}})
        step = PERIOD_SECONDS[request.url.params["period"]]
        span = int(request.url.params["span"])
        stamps = [start + index * step for index in range(span)]
        if self.now is not None:
            stamps = [stamp for stamp in stamps if stamp <= self.now]
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "coins": {
                        coin: {
                            "symbol": "SYN",
                            "confidence": 0.99,
                            "decimals": 18,
                            "prices": [
                                {"timestamp": stamp, "price": float(Decimal(self.synthetic_price))}
                                for stamp in stamps
                            ],
                        }
                    }
                }
            ).encode(),
        )

    def client(self, clock, cache=None, **kwargs) -> DefiLlamaCoinsClient:
        return DefiLlamaCoinsClient(
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            cache=cache,
            clock=clock,
            sleep=lambda _: None,
            **kwargs,
        )
