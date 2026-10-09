"""A tiny stdio MCP server used by the tests (python tests/mcp_test_server.py)."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("forgeflow-test")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return f"Sunny in {city}"


@server.tool()
def create_ticket(title: str) -> str:
    """Create a ticket."""
    return f"created ticket: {title}"


@server.tool()
def delete_everything() -> str:
    """Delete all data."""
    return "deleted"


if __name__ == "__main__":
    server.run("stdio")
