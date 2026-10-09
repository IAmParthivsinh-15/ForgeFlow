"""Browser QA support (spec sections 28, 266A).

There is deliberately no browser automation here: the QA agent drives the browser
through Playwright MCP tools. This package only serves the commit under test so the
browser has something to open (preview.py).
"""
