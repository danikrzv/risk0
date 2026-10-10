import pytest

from app.config import RADAR_CHAIN_IDS_ENV, radar_chain_ids


def test_radar_chain_ids_default_to_ethereum_mainnet():
    assert radar_chain_ids({}) == (1,)
    assert radar_chain_ids({RADAR_CHAIN_IDS_ENV: "  "}) == (1,)


def test_radar_chain_ids_are_parsed_deduplicated_and_sorted():
    assert radar_chain_ids({RADAR_CHAIN_IDS_ENV: "8453, 1,1,"}) == (1, 8453)


@pytest.mark.parametrize("value", ["mainnet", "1,base", "0", "-1", "1.5", ",", "01"])
def test_invalid_radar_chain_ids_fail_fast_with_the_variable_name(value):
    with pytest.raises(ValueError, match=RADAR_CHAIN_IDS_ENV):
        radar_chain_ids({RADAR_CHAIN_IDS_ENV: value})
