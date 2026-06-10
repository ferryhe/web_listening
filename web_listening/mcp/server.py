from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from web_listening.mcp import tools

SERVER_NAME = "web-listening"


def create_server() -> FastMCP:
    """Create the thin MCP server that exposes web_listening acquisition tools."""

    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Use these tools to inspect acquisition capabilities, probe one adapter, "
            "recommend the next adapter, or run the shared fallback acquisition engine."
        ),
    )
    server.tool(name="web_listening_list_acquisition_tools")(tools.web_listening_list_acquisition_tools)
    server.tool(name="web_listening_probe_tool_once")(tools.web_listening_probe_tool_once)
    server.tool(name="web_listening_recommend_next_tool")(tools.web_listening_recommend_next_tool)
    server.tool(name="web_listening_acquire_with_fallback")(tools.web_listening_acquire_with_fallback)
    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
