"""Capability base class + registry.

A Capability is one complete domain task closure that maps 1:1 to a user
intent. It receives a Pydantic input, composes Tools, and returns a
Pydantic output. Capability code MUST NOT read `data/runs/*.json` or
import `services.chroma_service` directly — go through Tools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel


class CapabilityInput(BaseModel):
    """Base class for capability inputs. Subclass per capability."""


class CapabilityOutput(BaseModel):
    """Base class for capability outputs. Subclass per capability."""


TIn = TypeVar("TIn", bound=CapabilityInput)
TOut = TypeVar("TOut", bound=CapabilityOutput)


class CapabilityError(Exception):
    """Raised when a capability cannot produce a meaningful result."""


class Capability(ABC, Generic[TIn, TOut]):
    """Abstract capability. Subclasses declare Input/Output and implement run()."""

    # Unique intent name (must match router output values)
    name: ClassVar[str] = ""

    # Pydantic classes — subclasses MUST override
    Input: ClassVar[type[CapabilityInput]] = CapabilityInput
    Output: ClassVar[type[CapabilityOutput]] = CapabilityOutput

    @abstractmethod
    def run(self, input: TIn) -> TOut:
        ...


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
