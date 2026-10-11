"""Protocol-level TVL inputs for the dependency graph's protocol node.

The brief asks for "protocols concentrating a large share of on-chain TVL". Two
shares are reported on the Morpho node:

- `chain_tvl_share`: the protocol's TVL on the chain divided by the chain's DeFi
  TVL, both from DeFiLlama's default view (borrowed assets excluded on both
  sides, so the ratio is like for like).
- `entity_share_of_protocol_tvl`: the analysed vault's total assets (or the
  analysed market's total supply), in USD, divided by the protocol's gross
  deposits on the chain, i.e. DeFiLlama TVL plus the `<Chain>-borrowed`
  breakdown. Vault assets and market supply include lent-out funds, so the
  denominator must include borrowed funds too. It also includes posted
  collateral, so the share is a lower bound on the entity's share of lending
  supply.
"""

from decimal import Decimal

from pydantic import Field

from app.contracts.models import DecimalValue
from app.domain.snapshots import SnapshotModel


# A protocol holding at least this share of its chain's DeFi TVL is flagged as a
# concentration of on-chain TVL: a protocol-wide impairment would then also hit
# the collateral markets and exit venues the position depends on.
PROTOCOL_CHAIN_TVL_SHARE_WARNING = Decimal("0.05")
# An analysed vault or market holding at least this share of the protocol's
# gross deposits is flagged: its own exit or impairment moves the protocol.
ENTITY_PROTOCOL_TVL_SHARE_WARNING = Decimal("0.10")
SHARE_QUANTUM = Decimal("0.000001")


class ProtocolTvlSnapshot(SnapshotModel):
    protocol: str = Field(min_length=1)
    label: str = Field(min_length=1)
    chain: str = Field(min_length=1)
    tvl_usd: DecimalValue | None = None
    chain_tvl_usd: DecimalValue | None = None
    chain_borrowed_usd: DecimalValue | None = None
    chain_defi_tvl_usd: DecimalValue | None = None
    chain_tvl_share: DecimalValue | None = None

    @property
    def chain_gross_deposits_usd(self) -> Decimal | None:
        # Unknown borrowed funds leave gross deposits unknown rather than understated.
        if self.chain_tvl_usd is None or self.chain_borrowed_usd is None:
            return None
        return self.chain_tvl_usd + self.chain_borrowed_usd
