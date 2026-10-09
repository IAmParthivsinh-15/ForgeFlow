"""Knowledge: chunking, repository/history indexing, retrieval, Elasticsearch client."""

import json

import httpx
import pytest

from forgeflow.core.ids import utcnow
from forgeflow.extensibility.store import InMemoryDocumentStore
from forgeflow.knowledge.chunking import chunk_file, indexable
from forgeflow.knowledge.documents import signature
from forgeflow.knowledge.index import (
    ElasticsearchIndex,
    InMemoryKnowledgeIndex,
    KnowledgeUnavailable,
)
from forgeflow.knowledge.service import KnowledgeService, format_hits
from forgeflow.schemas.task import CheckRun, Task, TaskResult, TaskStatus
from forgeflow.tools.git.client import GitClient
from tests.conftest import drive, git

PY = (
    '''import os


def helper(x):
    return x + 1


class AuthService:
    """Login."""

    def login(self, user, password):
        token = "ghp_'''
    + "a" * 36
    + """"
        return token
"""
)


def test_python_files_are_chunked_by_symbol_and_secrets_redacted():
    chunks = chunk_file("backend/auth.py", PY)
    symbols = {c.symbol for c in chunks}
    assert {"helper", "AuthService"} <= symbols
    assert all(c.language == "python" and c.category == "source" for c in chunks)
    auth = next(c for c in chunks if c.symbol == "AuthService")
    assert auth.start_line == 8 and "def login" in auth.text
    assert "ghp_" not in auth.text and "[REDACTED]" in auth.text
    # Module-level code (imports) is kept as an un-named chunk.
    assert any(c.symbol is None and "import os" in c.text for c in chunks)


def test_indexable_skips_secrets_vendored_and_large_files():
    assert indexable("src/app.py", 100, 1000)
    assert indexable("tests/test_app.py", 100, 1000)
    assert not indexable(".env", 10, 1000)
    assert not indexable("config/prod.pem", 10, 1000)
    assert not indexable("node_modules/x/index.js", 10, 1000)
    assert not indexable("package-lock.json", 10, 1000)
    assert not indexable("src/big.py", 5000, 1000)
    assert not indexable("assets/logo.png", 10, 1000)


def test_long_files_are_windowed_with_overlap():
    text = "\n".join(f"line {i}" for i in range(1, 151))
    chunks = chunk_file("notes.txt", text)
    assert len(chunks) == 3
    assert chunks[0].start_line == 1 and chunks[0].end_line == 60
    assert chunks[1].start_line == 51  # 10 lines overlap


def test_failure_signature_ignores_volatile_parts():
    a = signature("AssertionError: expected 3 got 4 at 0x7f3a in wf_abc123")
    b = signature("AssertionError: expected 5 got 9 at 0x1b2c in wf_def456")
    assert a == b
    assert a != signature("KeyError: 'user'")


async def test_git_reads_a_commit_without_checkout(git_repo):
    client = GitClient()
    sha, _ = await client.head(git_repo)
    files = dict(await client.tree_files(git_repo, sha))
    assert set(files) == {"README.md", "forgeflow.yaml", "src/app.py"}
    (git_repo / "src" / "app.py").write_text("changed on disk, not committed\n")
    blobs = await client.read_blobs(git_repo, sha, ["src/app.py", "missing.py", "README.md"])
    assert blobs["src/app.py"].startswith(b"def handler")
    assert "missing.py" not in blobs and blobs["README.md"].strip() == b"# App"


@pytest.fixture
def knowledge(settings):
    return KnowledgeService(
        InMemoryKnowledgeIndex(), InMemoryDocumentStore(), GitClient(), settings
    )


async def test_repository_index_is_searchable_and_replaced_per_commit(knowledge, git_repo):
    client = GitClient()
    sha, ref = await client.head(git_repo)
    summary = await knowledge.index_repository("proj_app", git_repo, sha, ref)
    assert summary["files"] == 3 and summary["chunks"] >= 3
    hits = await knowledge.search_code("handler", "proj_app")
    assert hits[0].source["path"] == "src/app.py" and hits[0].source["symbol"] == "handler"
    # Same commit: skipped (marker says indexed).
    assert (await knowledge.index_repository("proj_app", git_repo, sha, ref))["commit"] == sha

    (git_repo / "src" / "app.py").write_text("def renamed_handler():\n    return 'ok'\n")
    git(git_repo, "commit", "-qam", "rename")
    new_sha, _ = await client.head(git_repo)
    await knowledge.index_repository("proj_app", git_repo, new_sha, ref)
    code = knowledge.index.docs["code"].values()
    assert {d["commit"] for d in code} == {new_sha}  # old commit's chunks removed
    assert (await knowledge.search_code("renamed_handler", "proj_app"))[0].source["symbol"] == (
        "renamed_handler"
    )


def _qa_task(round_no: int, blocking: bool, output: str) -> Task:
    now = utcnow()
    return Task(
        task_id=f"wf_1.V{round_no}-qa",
        workflow_id="wf_1",
        key=f"V{round_no}-qa",
        title="QA",
        kind="qa",
        agent_type="qa",
        status=TaskStatus.COMPLETED,
        round=round_no,
        result=TaskResult(
            verdict="fail" if blocking else "pass",
            blocking=blocking,
            blocking_reasons=["qa test command failed: pytest (exit 1)"] if blocking else [],
            checks=[
                CheckRun(
                    kind="test",
                    command="pytest",
                    exit_code=1 if blocking else 0,
                    passed=not blocking,
                    duration_ms=5,
                    output=output,
                )
            ],
        ),
        created_at=now,
        updated_at=now,
        completed_at=now,
    )


async def test_history_is_indexed_and_resolutions_recorded(knowledge):
    failing = _qa_task(1, True, "FAILED tests/test_auth.py::test_refresh - KeyError: 'exp'")
    now = utcnow()
    repair = Task(
        task_id="wf_1.F1-repair",
        workflow_id="wf_1",
        key="F1-repair",
        title="Repair",
        kind="repair",
        agent_type="developer_subagent",
        status=TaskStatus.COMPLETED,
        round=1,
        result=TaskResult(summary="Set the exp claim on refresh tokens", files_changed=["auth.py"]),
        created_at=now,
        updated_at=now,
    )
    passing = _qa_task(2, False, "1 passed")
    assert await knowledge.index_task(failing, "proj_app", "abc1234", []) == 2  # failure + test
    await knowledge.index_task(passing, "proj_app", "def5678", [])

    hits = await knowledge.similar_failures("KeyError exp in test_refresh", "proj_app")
    assert hits and hits[0].kind == "failures" and not hits[0].source["resolved"]
    assert await knowledge.mark_resolved("wf_1", [failing, repair, passing]) == 1
    [resolved] = [d for d in knowledge.index.docs["failures"].values()]
    assert resolved["resolved"] and "exp claim" in resolved["resolution"]
    assert "resolved by: Set the exp claim" in format_hits(
        await knowledge.similar_failures("KeyError exp", "proj_app", exclude_workflow="wf_9")
    )
    # Other projects and the same workflow are excluded.
    assert await knowledge.similar_failures("KeyError exp", "proj_other") == []
    assert (
        await knowledge.similar_failures("KeyError exp", "proj_app", exclude_workflow="wf_1") == []
    )


async def test_repair_tasks_receive_similar_earlier_failures(container, git_repo, settings):
    """A failure from an earlier workflow is handed to the repair agent with its fix."""
    from forgeflow.platform.orchestration.gateway import ImplementationRequest

    seen: list[ImplementationRequest] = []
    original = container.gateway.implement_subtask

    async def spy(ctx, request):
        seen.append(request)
        assert ctx.knowledge is container.knowledge and ctx.repository_id == "proj_app"
        return await original(ctx, request)

    container.gateway.implement_subtask = spy
    earlier = _qa_task(1, True, "x").model_copy(
        update={"workflow_id": "wf_old", "task_id": "wf_old.V1-review", "kind": "review"}
    )
    earlier.result.blocking_reasons = [
        "review [high] forgeflow-demo/backend/T2-backend.md:1 Demo finding"
    ]
    await container.knowledge.index_task(earlier, "proj_app", "abc1234", [])

    svc = container.service
    wf = await svc.create_workflow("Add a demo-repair change", "app")
    await svc.process_analysis(wf.workflow_id, 1)
    [q] = await container.store.list_questions(wf.workflow_id)
    await svc.answer_question(q.question_id, "A", None)
    await svc.process_analysis(wf.workflow_id, 2)
    await container.execution.start_execution(wf.workflow_id)
    await drive(container, wf.workflow_id)

    repairs = [r for r in seen if r.task.kind == "repair"]
    assert repairs and "Demo finding" in repairs[0].history
    implements = [r for r in seen if r.task.kind == "implement"]
    assert implements and all(r.history == "" for r in implements)


# ---------------------------------------------------------------- elasticsearch


class FakeES:
    def __init__(self):
        self.indices: dict[str, dict] = {}
        self.bulk_lines: list[dict] = []
        self.searches: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/_cluster/health":
            return httpx.Response(200, json={"status": "yellow"})
        if path == "/_bulk":
            lines = [json.loads(x) for x in request.content.decode().splitlines() if x]
            self.bulk_lines += lines
            return httpx.Response(200, json={"errors": False, "items": []})
        name = path.split("/")[1]
        if method == "HEAD":
            return httpx.Response(200 if name in self.indices else 404)
        if method == "PUT" and path.count("/") == 1:
            self.indices[name] = json.loads(request.content)
            return httpx.Response(200, json={"acknowledged": True})
        if path.endswith("/_search"):
            body = json.loads(request.content)
            self.searches.append(body)
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {
                                "_index": "ff-failures",
                                "_id": "t1:0",
                                "_score": 2.5,
                                "_source": {"title": "boom", "embedding": [0.1]},
                            }
                        ]
                    }
                },
            )
        if path.endswith("/_mapping"):
            return httpx.Response(200, json={name: {"mappings": {"properties": {}}}})
        if path.endswith("/_count"):
            return httpx.Response(200, json={"count": 7})
        return httpx.Response(200, json={})


async def test_elasticsearch_client_creates_indices_bulk_indexes_and_searches():
    es = FakeES()
    index = ElasticsearchIndex("http://es:9200", "ff", transport=httpx.MockTransport(es))
    assert await index.ping()
    await index.put_many("failures", [{"id": "t1:0", "title": "boom", "repository_id": "p"}])
    mapping = es.indices["ff-failures"]["mappings"]
    assert mapping["properties"]["repository_id"] == {"type": "keyword"}
    assert es.bulk_lines[0] == {"index": {"_index": "ff-failures", "_id": "t1:0"}}
    assert "id" not in es.bulk_lines[1]

    [hit] = await index.search(["failures"], "boom", {"repository_id": "p"})
    query = es.searches[-1]["query"]["bool"]
    assert query["filter"] == [{"term": {"repository_id": "p"}}]
    assert query["must"][0]["multi_match"]["query"] == "boom"
    assert hit.kind == "failures" and "embedding" not in hit.source
    assert await index.count("failures") == 7


async def test_elasticsearch_outage_degrades_and_backs_off(settings):
    def down(request):
        raise httpx.ConnectError("refused")

    index = ElasticsearchIndex("http://es:9200", transport=httpx.MockTransport(down))
    svc = KnowledgeService(index, InMemoryDocumentStore(), GitClient(), settings)
    assert not await index.ping()
    with pytest.raises(KnowledgeUnavailable):
        await index.put_many("failures", [{"id": "x"}])
    assert await svc.search("anything") == []
    assert not svc.available() and "refused" in svc.last_error  # backing off
    assert (await svc.status())["available"] is False


class FakeEmbedder:
    model = "fake-embed"

    async def embed(self, texts):
        return [[1.0, 0.0] if "token" in t.lower() else [0.0, 1.0] for t in texts]


async def test_embeddings_are_fused_with_keyword_ranking(settings):
    index = InMemoryKnowledgeIndex()
    svc = KnowledgeService(index, InMemoryDocumentStore(), GitClient(), settings, FakeEmbedder())
    await svc.index_task(
        _qa_task(1, True, "refresh TOKEN expired").model_copy(update={"workflow_id": "wf_a"}),
        "proj",
        "abc1234",
        [],
    )
    assert all("embedding" in d for d in index.docs["failures"].values())
    hits = await svc.search("token", ["failures"])
    assert hits and hits[0].kind == "failures"


async def test_indexer_consumes_workflow_events(container, git_repo):
    """The knowledge-indexer consumer group, fed with a real workflow's event stream."""
    from forgeflow.apps.worker.telemetry import handle_knowledge_event

    svc = container.service
    wf = await svc.create_workflow("Run tests and QA", "app")
    await svc.process_analysis(wf.workflow_id, 1)
    await container.execution.start_execution(wf.workflow_id)
    await drive(container, wf.workflow_id)
    for stored in await container.store.list_events(wf.workflow_id):
        await handle_knowledge_event(container, stored.event)
    index = container.knowledge.index
    assert any(d["path"] == "src/app.py" for d in index.docs["code"].values())
    assert any(d["workflow_id"] == wf.workflow_id for d in index.docs["tests"].values())
    assert any(d["agent"] == "qa" for d in index.docs["agent-events"].values())
