"""Validated protocol execution through semantic actions and DeviceRegistry."""
from __future__ import annotations

from typing import Any
import threading

from pydantic import BaseModel, Field

from .actions import ActionExecutor, ActionLogger, ActionRecord
from .coordinates import CoordinateRegistry
from .production_safety import SafetyReport, validate_execution
from .protocol import ExperimentProtocol
from .workflows import ExperimentPlan, compile_protocol
from .multi_round import MultiRoundPlan


class ExperimentResult(BaseModel):
    sample_id: str
    dry_run: bool
    success: bool
    safety: SafetyReport
    action_count: int
    records: list[ActionRecord]
    error: str | None = None
    sample_states: dict[int, dict[str, Any]] = Field(default_factory=dict)


class ExperimentService:
    def __init__(
        self,
        registry: Any,
        coordinates: CoordinateRegistry,
        *,
        sleep: Any = None,
    ) -> None:
        self.registry = registry
        self.coordinates = coordinates
        self.sleep = sleep

    def prepare(
        self, protocol: ExperimentProtocol, *, dry_run: bool
    ) -> tuple[ExperimentPlan, SafetyReport]:
        plan = compile_protocol(protocol)
        report = validate_execution(
            protocol,
            plan,
            self.registry,
            self.coordinates,
            dry_run=dry_run,
        )
        return plan, report

    def execute(
        self,
        protocol: ExperimentProtocol,
        *,
        dry_run: bool,
        cancel_event: threading.Event | None = None,
    ) -> ExperimentResult:
        plan, safety = self.prepare(protocol, dry_run=dry_run)
        if not safety.ready:
            return ExperimentResult(
                sample_id=protocol.sample_id,
                dry_run=dry_run,
                success=False,
                safety=safety,
                action_count=0,
                records=[],
                error="production safety validation rejected execution",
            )
        logger = ActionLogger()
        kwargs: dict[str, Any] = {"dry_run": dry_run, "logger": logger}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        executor = ActionExecutor(self.registry, self.coordinates, **kwargs)
        try:
            for action in plan.actions:
                executor.execute(action)
        except Exception as exc:
            if not dry_run:
                self.registry.estop.halt_all()
            return ExperimentResult(
                sample_id=protocol.sample_id,
                dry_run=dry_run,
                success=False,
                safety=safety,
                action_count=len(logger.records()),
                records=logger.records(),
                error=f"{type(exc).__name__}: {exc}",
                sample_states=executor.sample_states(),
            )
        return ExperimentResult(
            sample_id=protocol.sample_id,
            dry_run=dry_run,
            success=True,
            safety=safety,
            action_count=len(logger.records()),
            records=logger.records(),
            sample_states=executor.sample_states(),
        )

    def execute_plan(
        self,
        plan: MultiRoundPlan,
        *,
        dry_run: bool,
        sample_id: str = "multi-round",
        cancel_event: threading.Event | None = None,
    ) -> ExperimentResult:
        """Execute an already safety-validated semantic batch plan."""
        logger = ActionLogger()
        kwargs: dict[str, Any] = {"dry_run": dry_run, "logger": logger}
        if self.sleep is not None:
            kwargs["sleep"] = self.sleep
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        executor = ActionExecutor(self.registry, self.coordinates, **kwargs)
        try:
            for action in plan.actions:
                executor.execute(action)
        except Exception as exc:
            if not dry_run:
                self.registry.estop.halt_all()
            return ExperimentResult(
                sample_id=sample_id,
                dry_run=dry_run,
                success=False,
                safety=SafetyReport(ready=True),
                action_count=len(logger.records()),
                records=logger.records(),
                error=f"{type(exc).__name__}: {exc}",
                sample_states=executor.sample_states(),
            )
        return ExperimentResult(
            sample_id=sample_id,
            dry_run=dry_run,
            success=True,
            safety=SafetyReport(ready=True),
            action_count=len(logger.records()),
            records=logger.records(),
            sample_states=executor.sample_states(),
        )
