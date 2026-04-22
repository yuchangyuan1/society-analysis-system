"""Tools Layer — atomic, reusable query/generation operations.

A Tool is a plain Python callable with Pydantic input/output schema.
Tools encapsulate access to storage, search, graph, and generation
services. They do NOT make business decisions (stance classification,
intervention routing, etc.) — that is the Capability's job.

First-version note: these are regular Python modules, NOT real MCP
protocol servers. See `interactive_agent_transformation_plan_skills_mcp.md`
§1 for the rationale. A real MCP wrapping can be layered on in Phase 4.
"""

from tools.base import (
    Tool,
    ToolError,
    ToolInputError,
    ToolInput,
    ToolOutput,
)

__all__ = [
    "Tool",
    "ToolError",
    "ToolInputError",
    "ToolInput",
    "ToolOutput",
]
