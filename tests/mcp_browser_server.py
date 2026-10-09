"""A stand-in for Playwright MCP used by the tests (python tests/mcp_browser_server.py).

Same tool names and annotations as Microsoft Playwright MCP (every interaction is
annotated destructive), backed by plain HTTP fetches instead of a real browser.
"""

import re
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
server = MCPServer("fake-playwright")
STATE = {"url": "", "html": ""}
INTERACT = ToolAnnotations(read_only_hint=False, destructive_hint=True)
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False)


@server.tool(annotations=INTERACT)
def browser_navigate(url: str) -> str:
    """Navigate to a URL."""
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - test server
        STATE["html"] = response.read().decode("utf-8", errors="replace")
    STATE["url"] = url
    return f"Navigated to {url}"


@server.tool(annotations=READ)
def browser_snapshot() -> str:
    """Accessibility snapshot of the current page."""
    text = " ".join(re.sub(r"<[^>]+>", " ", STATE["html"]).split())
    return f"- Page URL: {STATE['url']}\n- document: {text}"


@server.tool(annotations=READ)
def browser_take_screenshot(type: str = "png") -> Image:  # noqa: A002 - Playwright's name
    """Take a screenshot of the current page."""
    return Image(data=PNG, format="png")


@server.tool(annotations=INTERACT)
def browser_click(element: str, target: str) -> str:
    """Click an element."""
    return f"Clicked {element}"


@server.tool(annotations=INTERACT)
def browser_run_code_unsafe(code: str) -> str:
    """Run Playwright code (must never be reachable through ForgeFlow)."""
    return "ran arbitrary code"


if __name__ == "__main__":
    server.run("stdio")
