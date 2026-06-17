import json
from pathlib import Path

from autospin_system.example_experiment import ExperimentRunner


class FakeGantry:
    def __init__(self) -> None:
        self.position = {"X": 0.0, "Y": 0.0, "Z": -156.0}
        self.moves = []

    def get_position(self):
        return dict(self.position)

    def move_to(self, x, y, z):
        self.moves.append((float(x), float(y), float(z)))
        self.position = {"X": float(x), "Y": float(y), "Z": float(z)}
        return True

    def home(self):
        return True

    def get_status(self):
        return {"position": self.get_position()}

    def get_homing_diagnostics(self):
        return {}

    def emergency_stop(self):
        return True


class FakeLiquidHandler:
    def __init__(self) -> None:
        self.aspirated = []
        self.dispensed = []

    def aspirate(self, volume):
        self.aspirated.append(int(volume))
        return True

    def dispense(self, volume):
        self.dispensed.append(int(volume))
        return True

    def tip_present(self):
        return True

    def get_status(self):
        return {"tip_present": True}


class FakeSpincoater:
    def __init__(self) -> None:
        self.speeds = []
        self.started = False
        self.stopped = False

    def unlock(self):
        return True

    def start(self, direction="forward"):
        self.started = direction
        return True, "started"

    def set_speed(self, rpm):
        self.speeds.append(float(rpm))
        return True, "speed set"

    def stop(self, use_brake=True):
        self.stopped = use_brake
        return True

    def get_status(self):
        return {"started": self.started, "speeds": self.speeds}


class FakeHotplate:
    def __init__(self) -> None:
        self.sv = None

    def write_sv(self, temp_c):
        self.sv = float(temp_c)

    def read_pv(self):
        return self.sv if self.sv is not None else 25.0

    def get_status(self):
        return {"sv": self.sv, "pv": self.read_pv()}


class FakeMaestro:
    def __init__(self) -> None:
        self.mock = True
        self.gantry = FakeGantry()
        self.spincoater = FakeSpincoater()
        self.liquidhandler = FakeLiquidHandler()
        self.hotplate = FakeHotplate()
        self.hotplates = {"Hotplate1": self.hotplate}
        self.started = False

    def start_experiment(self):
        self.started = True


def _full_cycle_recipe():
    path = Path(__file__).resolve().parent / "examples" / "one_round_full_spin_hotplate_cycle.json"
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def test_full_cycle_recipe_runs_with_fake_mock_hardware():
    maestro = FakeMaestro()
    runner = ExperimentRunner(maestro, time_scale=0)

    runner.run(_full_cycle_recipe())

    assert maestro.started is True
    assert maestro.hotplate.sv == 100.0
    assert maestro.spincoater.started == "forward"
    assert maestro.spincoater.speeds == [1000.0, 3000.0]
    assert maestro.spincoater.stopped is True
    assert maestro.liquidhandler.aspirated == [50, 150]
    assert maestro.liquidhandler.dispensed == [50, 150]
    assert runner._mounted_tip_index == 2


def test_move_gantry_safe_uses_explicit_safe_z_before_xy():
    maestro = FakeMaestro()
    runner = ExperimentRunner(maestro, time_scale=0)

    runner.move_gantry_safe({"x": 10, "y": 20, "z": -120, "safe_z": -50})

    assert maestro.gantry.moves == [
        (0.0, 0.0, -50.0),
        (10.0, 20.0, -50.0),
        (10.0, 20.0, -120.0),
    ]


def test_hotplate_low_level_operations_use_mock_fast_path():
    maestro = FakeMaestro()
    runner = ExperimentRunner(maestro, time_scale=0)

    runner.set_hotplate_temperature({"temperature_C": 100})
    runner.check_hotplate_temperature({"target_C": 100, "tolerance_C": 2, "timeout_s": 300})
    runner.anneal_wait({"time_s": 600})

    assert maestro.hotplate.sv == 100.0
