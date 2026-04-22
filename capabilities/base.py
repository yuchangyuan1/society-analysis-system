"""Capability base class + registry + manifest.

A Capability is one complete domain task closure that maps 1:1 to a user
intent. It receives a Pydantic input, composes Tools, and returns a
Pydantic output. Capability code MUST NOT read `data/runs/*.json` or
import `services.chroma_service` directly — go through Tools.

Each Capability also exposes a `manifest()` — a structured description
consumed by the online Planner Agent (`agents/planner.py`) to decide
which capability to include in a workflow DAG.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, Field


class CapabilityInput(BaseModel):
    """Base class for capability inputs. Subclass per capability."""


class CapabilityOutput(BaseModel):
    """Base class for capability outputs. Subclass per capability."""


TIn = TypeVar("TIn", bound=CapabilityInput)
TOut = TypeVar("TOut", bound=CapabilityOutput)


class CapabilityError(Exception):
    """Raised when a capability cannot produce a meaningful result."""


class CapabilityManifest(BaseModel):
    """Planner-facing description of a single capability.

    Returned by `Capability.manifest()`. The Planner Agent reads a list of
    these to pick which capabilities to include in a bounded DAG.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    example_utterances: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Capability(ABC, Generic[TIn, TOut]):
    """Abstract capability. Subclasses declare Input/Output and implement run()."""

    # Unique intent name (must match router output values)
    name: ClassVar[str] = ""

    # One-sentence summary the Planner LLM reads to decide whether to call this.
    description: ClassVar[str] = ""

    # Sample user utterances that SHOULD route to this capability.
    example_utterances: ClassVar[list[str]] = []

    # Optional retrieval tags (free-form; lowercase).
    tags: ClassVar[list[str]] = []

    # Pydantic classes — subclasses MUST override
    Input: ClassVar[type[CapabilityInput]] = CapabilityInput
    Output: ClassVar[type[CapabilityOutput]] = CapabilityOutput

    @abstractmethod
    def run(self, input: TIn) -> TOut:
        ...

    @classmethod
    def manifest(cls) -> CapabilityManifest:
        """Return the planner-facing manifest for this capability."""
        return CapabilityManifest(
            name=cls.name,
            description=cls.description,
            input_schema=cls.Input.model_json_schema(),
            output_schema=cls.Output.model_json_schema(),
            example_utterances=list(cls.example_utterances),
            tags=list(cls.tags),
        )


CAPABILITY_REGISTRY: dict[str, Capability] = {}


def register_capability(capability: Capability) -> Capability:
    """Register an instantiated capability under its `name`."""
    if not capability.name:
        raise ValueError(
            f"Capability {type(capability).__name__} has no `name`"
        )
    if capability.name in CAPABILITY_REGISTRY:
        # Allow re-registration (e.g., reload) but warn via stderr.
        import sys
        print(
            f"[capabilities] Re-registering {capability.name}",
            file=sys.stderr,
        )
    CAPABILITY_REGISTRY[capability.name] = capability
    return capability


def list_manifests() -> list[CapabilityManifest]:
    """Return manifests for all registered capabilities. Used by the Planner Agent."""
    return [type(cap).manifest() for cap in CAPABILITY_REGISTRY.values()]
