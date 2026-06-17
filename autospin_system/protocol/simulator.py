"""Dry-run rendering for experiment protocols.

The simulator does not touch Maestro or hardware. It renders the compiled task
list into human-readable lines so the operator can inspect the intended
workflow before choosing mock or real execution.
"""

from .compiler import compile_protocol
from .schema import ExperimentProtocol


def simulate_protocol(protocol: ExperimentProtocol) -> list[str]:
    """Return a readable dry-run summary of the compiled protocol."""

    lines = [
        f"[SIM] sample={protocol.sample_id}",
        f"[SIM] description={protocol.description or ''}",
    ]
    for index, task in enumerate(compile_protocol(protocol), start=1):
        name = task["name"]
        details = task.get("details", {})
        if name == "move_sample":
            lines.append(f"[{index}] Move sample: {details['from']} -> {details['to']}")
        elif name == "dispense_liquid":
            lines.append(
                f"[{index}] Dispense {details['volume_ul']:g} uL "
                f"{details['liquid']} at {details['target']}"
            )
        elif name == "spincoat":
            lines.append(f"[{index}] SpinCoat")
            for step_i, step in enumerate(details["profile"], start=1):
                lines.append(
                    f"    step {step_i}: {step['speed']:g} rpm for {step['duration']:g} s"
                )
            for event in details.get("timed_events", []):
                op = event["operation"]
                lines.append(
                    f"    at {event['at_s']:g} s: dispense {op['volume_ul']:g} uL "
                    f"{op['liquid']} at {op['target']}"
                )
        elif name == "anneal":
            lines.append(
                f"[{index}] Anneal at {details['target_temp']:g} C for "
                f"{details['duration']:g} s on {details['hotplate']}"
            )
        elif name == "wait":
            lines.append(f"[{index}] Wait {details['duration']:g} s")
        elif name == "measure":
            lines.append(
                f"[{index}] Measure target={details['target']} method={details['method']}"
            )
        else:
            lines.append(f"[{index}] {name}: {details}")
    return lines
