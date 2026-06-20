"""RS485/串口归一验收：断言每个设备解析到正确的树莓派 udev 名。

这是 Chunk 4「串口配置归一」的回归护栏，**不连真硬件**——只校验
config + 端口解析逻辑 + 各设备 baud。真硬件连通性是 PM 物理 bring-up，
见 docs/decision-log/device-bringup-smoke-gates.md。
"""

import logging

from autospin_system.config.hardware_config import CONFIG
from autospin_system.maestro import resolve_serial_ports
from autospin_system.hardware.spin_motor.motor_controller import MotorController
from autospin_system.hardware.pipette.pipette_controller import PipetteController
from autospin_system.hardware.heating_stage.heating_stage_controller import (
    HeatingStageController,
)


RS485 = "/dev/autospin_rs485"   # CH340 1A86:7523 共享总线（同 grbl 芯片，udev 物理口区分）
RELAY = "/dev/autospin_relay"   # DSTUR-T80 STM32 0483:5740
XYZ = "/dev/autospin_xyz"       # grbl-Mega-5X CH340 1A86:7523（龙门专属）

_LOGGER = logging.getLogger("test_port_mapping")


def test_each_device_resolves_to_expected_udev_name():
    """权威目标映射：每个设备解析出的串口 == 对应 udev 稳定名。"""
    ports = resolve_serial_ports(CONFIG["communication"])
    assert ports["motor"] == RS485
    assert ports["pipette"] == RS485
    assert ports["heating_stage"] == RS485
    assert ports["relay"] == RELAY
    assert ports["gantry"] == XYZ


def test_only_gantry_uses_grbl_ch340_port():
    """除龙门外，任何设备都不许抢到 grbl 的 CH340 口（否则龙门失联）。"""
    ports = resolve_serial_ports(CONFIG["communication"])
    for device in ("motor", "pipette", "heating_stage", "relay"):
        assert ports[device] != XYZ, f"{device} 抢了龙门的 {XYZ}"


def test_three_rs485_devices_share_one_bridge():
    """旋涂/移液/加热共用同一条 CH340 RS485 总线，shared_rs485 自动判真。"""
    ports = resolve_serial_ports(CONFIG["communication"])
    assert ports["motor"] == ports["pipette"] == ports["heating_stage"] == RS485
    assert ports["shared_rs485"] is True


def test_fallback_defaults_never_steal_grbl_port():
    """即使 communication 字段全缺，回退默认值也不得指向龙门 grbl 口。"""
    ports = resolve_serial_ports({})
    assert ports["gantry"] == XYZ
    for device in ("motor", "pipette", "heating_stage", "relay"):
        assert ports[device] != XYZ


def test_rs485_devices_distinguished_by_baud_and_slave_id():
    """共享同一物理桥，靠各自 baud + slave_id 区分（slave_id 1/2/3 互异）。"""
    devices = CONFIG["devices"]
    assert devices["pipette"]["baudrate"] == 115200
    assert devices["spin_motor"]["baudrate"] == 9600
    assert devices["heating_stage"]["baudrate"] == 9600
    slave_ids = {
        devices["pipette"]["slave_id"],
        devices["spin_motor"]["slave_id"],
        devices["heating_stage"]["slave_id"],
    }
    assert slave_ids == {1, 2, 3}


def test_controllers_open_with_per_device_baud():
    """SharedRs485DeviceProxy 是 open-use-close：每次按设备自己的 baud 重开口。

    这里用 mock 实例化三个控制器，断言底层串口对象拿到的 baud 来自各自
    设备配置（pipette 115200、旋涂/加热 9600）——即共享桥上 baud 按设备切换。
    """
    motor = MotorController(port=RS485, mock=True, logger=_LOGGER)
    pipette = PipetteController(port=RS485, mock=True, logger=_LOGGER)
    heating = HeatingStageController(port=RS485, mock=True, logger=_LOGGER)
    assert motor.comm.baudrate == 9600
    assert pipette.comm.baudrate == 115200
    assert heating.baudrate == 9600
