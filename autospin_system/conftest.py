"""Pytest collection guard for manual hardware smoke scripts."""

collect_ignore = [
    "test_full_linkage_sequence.py",
    "test_gantry_gripper_pick.py",
    "test_heating_stage_smoke.py",
    "test_joint_hardware_smoke.py",
    "test_rs485_bus.py",
    "test_spin_heat_link.py",
    "test_z2_stage_smoke.py",
]
