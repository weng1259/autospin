"""Generate validated protocol batches from external candidate parameters."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ..protocol import ExperimentProtocol
from ..protocol.templates import TEMPLATES
from .parameter_space import ParameterSpace


class ParameterCandidateProvider(Protocol):
    """Adapter point for LHS, Bayesian, and active-learning libraries."""

    def candidates(
        self, space: ParameterSpace, count: int
    ) -> Iterable[dict[str, Any]]: ...


class GeneratedExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    template: str
    parameters: dict[str, Any]
    protocol: ExperimentProtocol


class ExperimentGenerator:
    def __init__(self, space: ParameterSpace) -> None:
        if space.template not in TEMPLATES:
            raise ValueError(f"unknown protocol template {space.template!r}")
        self.space = space

    def generate(self, candidate: dict[str, Any]) -> GeneratedExperiment:
        parameters = self.space.validate_candidate(candidate)
        experiment_id = self.experiment_id(self.space.template, parameters)
        factory = TEMPLATES[self.space.template]
        factory_parameters = dict(parameters)
        if "antisolvent" in factory_parameters:
            factory_parameters["antisolvent_volume"] = factory_parameters.pop(
                "antisolvent"
            )
        protocol = factory(
            sample_id=experiment_id,
            **factory_parameters,
        )
        return GeneratedExperiment(
            experiment_id=experiment_id,
            template=self.space.template,
            parameters=parameters,
            protocol=protocol,
        )

    def generate_batch(
        self, candidates: Iterable[dict[str, Any]]
    ) -> list[GeneratedExperiment]:
        result = [self.generate(candidate) for candidate in candidates]
        ids = [item.experiment_id for item in result]
        if len(ids) != len(set(ids)):
            raise ValueError("batch contains duplicate parameter candidates")
        return result

    def generate_from_provider(
        self,
        provider: ParameterCandidateProvider,
        count: int,
    ) -> list[GeneratedExperiment]:
        if count <= 0:
            raise ValueError("count must be positive")
        candidates = list(provider.candidates(self.space, count))
        if len(candidates) != count:
            raise ValueError(
                f"candidate provider returned {len(candidates)}, expected {count}"
            )
        return self.generate_batch(candidates)

    @staticmethod
    def experiment_id(template: str, parameters: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"template": template, "parameters": parameters},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return f"{template}-{digest}"
