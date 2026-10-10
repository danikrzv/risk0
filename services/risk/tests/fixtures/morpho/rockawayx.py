"""Sanitized fixture builder for the RockawayX-curated Morpho Vault V2 entries.

Values are synthetic but shaped like public Morpho REST and GraphQL responses:
integer token units as strings in REST, USD amounts as JSON numbers in GraphQL.
Identity data (addresses, names, assets, Vault V2 type) matches the live Morpho
API as verified on 2026-10-10. No raw responses, credentials, or positions.
"""

from dataclasses import dataclass
from decimal import Decimal


MAINNET = 1
BASE = 8453
MAINNET_BLOCK = 26161049
BASE_BLOCK = 52425808


@dataclass(frozen=True)
class AssetFixture:
    address: str
    symbol: str
    name: str
    decimals: int
    price_usd: str


USDC = AssetFixture(
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", "USD Coin", 6, "0.9998"
)
USDC_BASE = AssetFixture(
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", "USD Coin", 6, "0.9998"
)
WETH = AssetFixture(
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "WETH", "Wrapped Ether", 18, "2500"
)
EUROP = AssetFixture(
    "0x888883b5F5D21fb10Dfeb70e8f9722B9FB0E5E51", "EUROP", "EURØP", 6, "1.167"
)


@dataclass(frozen=True)
class VaultV2Fixture:
    entity_id: str
    address: str
    name: str
    symbol: str
    asset: AssetFixture
    total_assets: str
    withdrawable_assets: str
    total_assets_usd: str
    liquidity_usd: str
    chain_id: int = MAINNET
    listed: bool = True

    @property
    def block(self) -> int:
        return BASE_BLOCK if self.chain_id == BASE else MAINNET_BLOCK

    @property
    def config_path(self) -> str:
        return f"/v0/vaults-v2/{self.chain_id}:{self.address}"

    @property
    def state_path(self) -> str:
        return f"/v1/vaults-v2/{self.chain_id}:{self.address}/state"

    def rest(self) -> dict[str, object]:
        identity = {
            "chain_id": self.chain_id,
            "address": self.address,
            "last_indexed_block": str(self.block),
        }
        return {
            self.config_path: {
                "data": {
                    **identity,
                    "version": "2.0",
                    "name": self.name,
                    "symbol": self.symbol,
                    "asset": {
                        "address": self.asset.address,
                        "decimals": self.asset.decimals,
                        "name": self.asset.name,
                        "symbol": self.asset.symbol,
                    },
                }
            },
            self.state_path: {
                "data": {
                    **identity,
                    "total_assets": self.total_assets,
                    "withdrawable_assets": self.withdrawable_assets,
                    "allocated_assets": self.total_assets,
                    "idle_assets": "0",
                    "share_price_ray": "1010000000000000000000000000",
                }
            },
        }

    def graphql(self) -> dict[str, object]:
        return {
            graphql_key("vault_v2", self.address, self.chain_id): {
                "data": {
                    "vaultV2ByAddress": {
                        "address": self.address,
                        "listed": self.listed,
                        "asset": {
                            "address": self.asset.address,
                            "symbol": self.asset.symbol,
                            "decimals": self.asset.decimals,
                            "price": {"usd": _json_number(self.asset.price_usd)},
                        },
                        "totalAssetsUsd": _json_number(self.total_assets_usd),
                        "liquidityUsd": _json_number(self.liquidity_usd),
                    }
                }
            }
        }


def graphql_key(kind: str, identifier: str, chain_id: int) -> str:
    """Mainnet keys keep the recorded fixture format; other chains are prefixed."""
    if chain_id == MAINNET:
        return f"{kind}:{identifier}"
    return f"{kind}:{chain_id}:{identifier}"


def _json_number(value: str) -> float:
    # Morpho sends USD values as JSON numbers; these strings round-trip exactly.
    number = float(value)
    assert Decimal(repr(number)) == Decimal(value), value
    return number


ROCKAWAYX_VAULTS = (
    VaultV2Fixture(
        "rockawayx-confidential-rwa-usdc",
        "0xc207d3f66537D7F66456808379A0E560cF20Da36",
        "RockawayX Confidential RWA USDC",
        "roxcUSDC",
        USDC,
        total_assets="26400000000000",
        withdrawable_assets="3550000000000",
        total_assets_usd="26394720",
        liquidity_usd="3549290",
    ),
    VaultV2Fixture(
        "rockawayx-fx-protocol-usdc",
        "0x2cA22cb25558fa2018ecb1CE4eD8AF92Ee7ea423",
        "RockawayX f(x) Protocol Ecosystem USDC",
        "roxFX",
        USDC,
        total_assets="20800000000000",
        withdrawable_assets="4800000000000",
        total_assets_usd="20795840",
        liquidity_usd="4799040",
    ),
    VaultV2Fixture(
        "rockawayx-yield-usdc",
        "0xE0181090c22579B6A217f1522cbf8c9f1F0C1965",
        "RockawayX USDC Yield",
        "roxUSDCy",
        USDC,
        total_assets="18500000000000",
        withdrawable_assets="350000000000",
        total_assets_usd="18496300",
        liquidity_usd="349930",
    ),
    VaultV2Fixture(
        "rockawayx-tori-ecosystem",
        "0x3BD9AdAE6643dDcddD02746b8B60075E56DF9478",
        "RockawayX Tori Ecosystem",
        "roxTORI",
        USDC,
        total_assets="13600000000000",
        withdrawable_assets="1900000000000",
        total_assets_usd="13597280",
        liquidity_usd="1899620",
    ),
    VaultV2Fixture(
        "huma-usdc-main",
        "0x8aC91877b93330f52b2979a31a4879506021475c",
        "Huma USDC Main",
        "humaUSDC",
        USDC,
        total_assets="12800000000000",
        withdrawable_assets="3550000000000",
        total_assets_usd="12797440",
        liquidity_usd="3549290",
    ),
    VaultV2Fixture(
        "rockawayx-weth",
        "0x64C18DCC4Ccb3b8D27877a4aeBB4C3126CB39cB9",
        "RockawayX wETH",
        "roxWETH",
        WETH,
        total_assets="849860311194908620",
        # A genuine provider zero: all assets are allocated, none withdrawable.
        withdrawable_assets="0",
        total_assets_usd="2124.65",
        liquidity_usd="0",
    ),
    VaultV2Fixture(
        "rockawayx-nuva-ecosystem",
        "0x93e0F9d502eEfce8D34924ab3478C7EA0CBC5a2E",
        "RockawayX NUVA Ecosystem",
        "roxNUVA",
        USDC,
        total_assets="1547000000000",
        withdrawable_assets="252000000000",
        total_assets_usd="1546690.6",
        liquidity_usd="251949.6",
    ),
    VaultV2Fixture(
        "rockawayx-prime-usdc",
        "0x5f829B1B473cBA86838E1B7BB7E144DbDE228e21",
        "RockawayX USDC Prime",
        "roxpUSDC",
        USDC,
        total_assets="10281331",
        withdrawable_assets="10281329",
        total_assets_usd="10.28",
        liquidity_usd="10.28",
    ),
    VaultV2Fixture(
        "midas-usdc-prime-ethereum",
        "0xe99A27169c2aA26a8f2757949d09Fa3f9A8f0B3B",
        "RockawayX Midas USDC Prime",
        "mpUSDC",
        USDC,
        total_assets="3513293089",
        withdrawable_assets="499860876",
        total_assets_usd="3512.59",
        liquidity_usd="499.76",
    ),
    VaultV2Fixture(
        "figure-usdc-main",
        "0xd65d6E8dbC3Cd3D12418199E6f4014dB3aaa0097",
        "Figure USDC Main",
        "figrUSDC",
        USDC,
        total_assets="11358783",
        withdrawable_assets="11358773",
        total_assets_usd="11.36",
        liquidity_usd="11.36",
    ),
    VaultV2Fixture(
        "rockawayx-europ",
        "0x9C57E02bC31e7e463f439353D97bd8f42F5a7F9c",
        "RockawayX EUROP",
        "roxEUROP",
        EUROP,
        total_assets="9600000000",
        withdrawable_assets="2400000000",
        total_assets_usd="11203.2",
        liquidity_usd="2800.8",
    ),
    VaultV2Fixture(
        "midas-usdc-prime-base",
        "0xAE4181CFB5aaA08bbE77d269c6B595672b9F9Edc",
        "RockawayX Midas USDC Prime",
        "mpUSDC",
        USDC_BASE,
        total_assets="585516841575",
        withdrawable_assets="201284353309",
        total_assets_usd="585399.74",
        liquidity_usd="201244.1",
        chain_id=BASE,
    ),
)
ROCKAWAYX_BY_ID = {vault.entity_id: vault for vault in ROCKAWAYX_VAULTS}
