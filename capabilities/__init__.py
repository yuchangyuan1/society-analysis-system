"""Capability Layer — domain task closures.

Each Capability:
  - Has a stable user-facing intent (topic_overview, claim_status, ...)
  - Has explicit Pydantic Input/Output schemas
  - Composes one or more Tools
  - Returns a structured object (never a raw LLM string)

Import order note: capability modules register themselves on import.
Importing this package auto-imports every concrete capability so the
CAPABILITY_REGISTRY is populated.
"""

from capabilities.base import (
    Capability,
    CapabilityInput,
    CapabilityOutput,
    CAPABILITY_REGISTRY,
    register_capability,
)

__all__ = [
    "Capability",
    "CapabilityInput",
    "CapabilityOutput",
    "CAPABILITY_REGISTRY",
    "register_capability",
]
