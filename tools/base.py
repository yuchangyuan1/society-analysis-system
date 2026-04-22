"""Tool base class + unified exceptions.

Tools are plain Python modules (not MCP protocol servers for the first
version). Each tool function accepts a Pydantic Input and returns a
Pydantic Output. The abstract `Tool` class is provided as a convenience
for tools with more than trivial state; most tools are written as module
level functions that simply type their args.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel


class ToolInput(BaseModel):
    """Base for tool inputs."""


class ToolOutput(BaseModel):
    """Base for tool outputs."""


TIn = TypeVar("TIn", bound=ToolInput)
TOut = TypeVar("TOut", bound=ToolOutput)


class ToolError(Exception):
    """Generic tool failure (I/O, external service, etc.)."""


class ToolInputError(ToolError):
    """Tool input is malformed or references missing data (run_id, etc.)."""


class Tool(ABC, Generic[TIn, TOut]):
    """Optional base class for stateful tools. Stateless tools can just
    be module-level functions."""

    name: str = ""

    @abstractmethod
    def run(self, input: TIn) -> TOut:
        ...
