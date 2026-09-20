from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.hardware.relay_backend import RelayBackend
from src.hardware.types import RelayActionPlan, RelayActionResult, RelayState


@patch("src.hardware.relay_backend.serial.Serial")
def test_relay_on_off_accept_logical_channel_names(mock_serial_cls: MagicMock) -> None:
    serial = MagicMock()
    mock_serial_cls.return_value = serial
    relay = RelayBackend(
        port="/dev/fake",
        settle_s=0.0,
        channel_map={"vacuum_valve": 3},
    )
    relay.connect()

    on_result = relay.on("vacuum_valve", idempotency_key="vac-on")
    off_result = relay.off("vacuum_valve", idempotency_key="vac-off")

    assert isinstance(on_result, RelayActionResult)
    assert isinstance(off_result, RelayActionResult)
    assert serial.write.call_args_list[0].args[0] == bytes([0xA0, 0x03, 0x01, 0xA4])
    assert serial.write.call_args_list[1].args[0] == bytes([0xA0, 0x03, 0x00, 0xA3])


def test_relay_semantic_dry_run_and_all_off() -> None:
    relay = RelayBackend(port="/dev/fake", channel_map={"spin_power": 4})

    plan = relay.on("spin_power", idempotency_key="dry", dry_run=True)
    plans = relay.all_off(dry_run=True)

    assert isinstance(plan, RelayActionPlan)
    assert plan.channel == 4
    assert isinstance(plans, list)
    assert len(plans) == 8
    assert all(isinstance(item, RelayActionPlan) for item in plans)


@patch("src.hardware.relay_backend.serial.Serial")
def test_relay_emergency_stop_turns_all_channels_off(mock_serial_cls: MagicMock) -> None:
    serial = MagicMock()
    mock_serial_cls.return_value = serial
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()
    relay.ch_on(1, idempotency_key="one")
    relay.ch_on(2, idempotency_key="two")

    state = relay.emergency_stop()

    assert isinstance(state, RelayState)
    assert all(value is False for value in state.channels.values())
    assert serial.write.call_count >= 10
    relay.disconnect()
    assert relay.is_connected() is False
