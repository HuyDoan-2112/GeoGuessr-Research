import asyncio
import sys
from pathlib import Path

from fastmcp import FastMCP

# Support both `python -m mcp_server.run` and `python mcp_server/run.py`.
if __package__ in (None, ""):
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from mcp_server.tools.registry import register_class_tools  # type: ignore
    from mcp_server.tools.nav import NavTools  # type: ignore
    from mcp_server.tools.info import InfoTools  # type: ignore
else:
    from .tools.registry import register_class_tools
    from .tools.nav import NavTools
    from .tools.info import InfoTools

mcp = FastMCP("street-agent")
register_class_tools(mcp, NavTools())
register_class_tools(mcp, InfoTools())


def main() -> None:
    asyncio.run(
        mcp.run(
            transport="http",
            host="127.0.0.1",
            port=9000,
        )
    )


if __name__ == "__main__":
    main()
