from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from btc_short_horizon.backtest.strategy import (
    BtcOpeningMispricingConfig,
    BtcOpeningMispricingStrategy,
)
from btc_short_horizon.strategy import LayerStructure


def test_nautilus_maker_config_converts_to_pure_planning_config() -> None:
    config = BtcOpeningMispricingConfig(
        market_slug="btc-updown-15m-1776038400",
        up_instrument_id=InstrumentId.from_str("UP.POLYMARKET"),
        down_instrument_id=InstrumentId.from_str("DOWN.POLYMARKET"),
        max_shares=Decimal("12"),
        layer_structure=LayerStructure.TWO_LEVEL.value,
        safety_buffer=0.02,
    )

    maker_config = config.maker_config()

    assert maker_config.structure is LayerStructure.TWO_LEVEL
    assert maker_config.max_shares == pytest.approx(12.0)
    assert maker_config.safety_buffer == pytest.approx(0.02)


def test_nautilus_maker_config_rejects_same_token_and_invalid_structure() -> None:
    instrument_id = InstrumentId.from_str("UP.POLYMARKET")
    with pytest.raises(ValueError, match="must differ"):
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1776038400",
            up_instrument_id=instrument_id,
            down_instrument_id=instrument_id,
        )
    with pytest.raises(ValueError, match="not-a-structure"):
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1776038400",
            up_instrument_id=instrument_id,
            down_instrument_id=InstrumentId.from_str("DOWN.POLYMARKET"),
            layer_structure="not-a-structure",
        )


def test_strategy_reset_preserves_completed_run_order_audit() -> None:
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1776038400",
            up_instrument_id=InstrumentId.from_str("UP.POLYMARKET"),
            down_instrument_id=InstrumentId.from_str("DOWN.POLYMARKET"),
        )
    )
    strategy._order_audit.record(  # type: ignore[attr-defined]
        event_type="submit", ts_ns=1, client_order_id="client-1"
    )

    strategy.on_reset()

    assert strategy.order_audit_events[0]["client_order_id"] == "client-1"
