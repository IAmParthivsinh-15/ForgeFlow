"""Repository parser and chunker (spec section 37: parser -> chunker -> metadata).

Python files are split per top-level function/class (methods separately for large
classes) using `ast`; JavaScript/TypeScript and similar languages at declaration
lines; everything else in overlapping line windows. Likely secret files are never
indexed and token-shaped strings are redacted.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from forgeflow.core.redaction import redact

WINDOW = 60
OVERLAP = 10
MAX_CHUNK_LINES = 150
MAX_FILES = 3000

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "csharp",
    ".php": "php",
    ".md": "markdown",
    ".rst": "text",
    ".txt": "text",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".sql": "sql",
    ".sh": "shell",
    ".html": "html",
    ".css": "css",
    ".graphql": "graphql",
    ".proto": "protobuf",
}
NAMED = {"dockerfile": "dockerfile", "jenkinsfile": "groovy", "makefile": "make"}
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        "vendor",
        ".next",
        "coverage",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
SKIP_FILES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "cargo.lock"}
)
_SECRET_NAME = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|keystore|jks)|id_(rsa|ed25519|ecdsa)"
    r"|credentials(\.json)?|secrets?\.(ya?ml|json))$",
    re.I,
)
_DECLARATION = re.compile(
    r"^\s*(export\s+)?(default\s+)?(async\s+)?(function\*?\s+(?P<f>\w+)|class\s+(?P<c>\w+)"
    r"|(const|let|var)\s+(?P<v>\w+)\s*=\s*(async\s*)?(\(|function|\w+\s*=>)"
    r"|func\s+(\([^)]*\)\s*)?(?P<g>\w+)|(pub\s+)?fn\s+(?P<r>\w+)"
    r"|(public|private|protected)\s+[\w<>\[\], ]+\s+(?P<j>\w+)\s*\()"
)


@dataclass
class Chunk:
    path: str
    language: str
    category: str
    symbol: str | None
    start_line: int
    end_line: int
    text: str


def language_of(path: str) -> str | None:
    p = PurePosixPath(path)
    return NAMED.get(p.name.lower()) or LANGUAGES.get(p.suffix.lower())


def category_of(path: str) -> str:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    dirs = lowered.split("/")[:-1]
    if (
        any(d in ("test", "tests", "spec", "__tests__") for d in dirs)
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith(("_test.go", "_test.py"))
    ):
        return "test"
    if name.endswith((".md", ".rst", ".txt")) or lowered.startswith("docs/"):
        return "docs"
    if lowered.startswith(("k8s/", "deploy/", "helm/", "charts/")) or name in (
        "dockerfile",
        "docker-compose.yml",
        "jenkinsfile",
    ):
        return "deployment"
    if name.endswith((".yaml", ".yml", ".toml", ".ini", ".cfg", ".json")):
        return "config"
    return "source"


def indexable(path: str, size: int, max_bytes: int) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1].lower() in SKIP_FILES:
        return False
    if _SECRET_NAME.search(path):
        return False
    return size <= max_bytes and language_of(path) is not None


def _windows(lines: list[str], first: int, last: int) -> list[tuple[int, int]]:
    spans = []
    start = first
    while start <= last:
        end = min(last, start + WINDOW - 1)
        spans.append((start, end))
        if end == last:
            break
        start = end - OVERLAP + 1
    return spans


def _python_spans(source: str, total: int) -> list[tuple[int, int, str | None]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[int, int, str | None]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        if isinstance(node, ast.ClassDef) and end - start + 1 > MAX_CHUNK_LINES:
            methods = [
                n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            head_end = (methods[0].lineno - 1) if methods else end
            spans.append((start, max(start, head_end), node.name))
            for m in methods:
                spans.append((m.lineno, m.end_lineno or m.lineno, f"{node.name}.{m.name}"))
        else:
            spans.append((start, end, node.name))
    return spans


def _declaration_spans(lines: list[str]) -> list[tuple[int, int, str | None]]:
    starts = []
    for i, line in enumerate(lines, start=1):
        m = _DECLARATION.match(line)
        if m:
            name = next((v for v in m.groupdict().values() if v), None)
            starts.append((i, name))
    spans = []
    for n, (start, name) in enumerate(starts):
        end = (starts[n + 1][0] - 1) if n + 1 < len(starts) else len(lines)
        spans.append((start, end, name))
    return spans


def chunk_file(path: str, text: str) -> list[Chunk]:
    language = language_of(path) or "text"
    category = category_of(path)
    text = redact(text.replace("\r\n", "\n"))
    lines = text.split("\n")
    total = len(lines)
    if not text.strip():
        return []
    if language == "python":
        spans = _python_spans(text, total)
    elif language in ("javascript", "typescript", "go", "rust", "java", "kotlin", "csharp"):
        spans = _declaration_spans(lines)
    else:
        spans = []

    covered = [False] * (total + 1)
    result: list[tuple[int, int, str | None]] = []
    for start, end, symbol in spans:
        # Very long symbols are windowed but keep their symbol name.
        for s, e in (
            _windows(lines, start, end) if end - start + 1 > MAX_CHUNK_LINES else [(start, end)]
        ):
            result.append((s, e, symbol))
        for i in range(start, min(end, total) + 1):
            covered[i] = True
    # Whatever is not part of a symbol (imports, module code, prose) in windows.
    gap_start = None
    for i in range(1, total + 2):
        inside = i <= total and not covered[i]
        if inside and gap_start is None:
            gap_start = i
        elif not inside and gap_start is not None:
            gap = "\n".join(lines[gap_start - 1 : i - 1]).strip()
            if gap:
                result += [(s, e, None) for s, e in _windows(lines, gap_start, i - 1)]
            gap_start = None
    result.sort(key=lambda r: r[0])
    return [
        Chunk(path, language, category, symbol, s, e, "\n".join(lines[s - 1 : e]))
        for s, e, symbol in result
        if "\n".join(lines[s - 1 : e]).strip()
    ]
