import inspect
from typing import Any


def register_class_tools(mcp: Any, obj: Any) -> None:
    """Register all public bound methods on the object as MCP tools."""
    for name, fn in inspect.getmembers(obj, inspect.ismethod):
        if name.startswith("_"):
            continue
        mcp.tool(name=name)(fn)
