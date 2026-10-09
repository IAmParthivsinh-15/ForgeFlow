"""Test doubles for external systems (Jenkins, security scanners)."""

from __future__ import annotations

from forgeflow.core.errors import CIUnavailable
from forgeflow.schemas.verification import CIBuild, CIStage


class FakeCI:
    """Records pipelines and returns scripted build results, in order."""

    name = "fake-ci"

    def __init__(self, results: list[str] | None = None, unavailable: int = 0) -> None:
        self.results = list(results or [])
        self.unavailable = unavailable
        self.runs: list[tuple[str, str, str]] = []

    async def run(self, job: str, pipeline: str, commit: str) -> CIBuild:
        if self.unavailable > 0:
            self.unavailable -= 1
            raise CIUnavailable("Jenkins request failed: ConnectError")
        self.runs.append((job, pipeline, commit))
        status = self.results.pop(0) if self.results else "SUCCESS"
        return CIBuild(
            provider=self.name,
            job=job,
            build_number=len(self.runs),
            url=f"http://ci.test/job/{job}/{len(self.runs)}/",
            status=status,  # type: ignore[arg-type]
            stages=[CIStage(name="Test", status="SUCCESS" if status == "SUCCESS" else "FAILED")],
            log_tail="tests ok" if status == "SUCCESS" else "AssertionError: expected 2, got 3",
            commit=commit,
        )


class FakeGitHub:
    """Minimal GitHub REST API double for httpx.MockTransport."""

    def __init__(self, can_push: bool = True, existing_pr: bool = False) -> None:
        self.can_push = can_push
        self.existing_pr = existing_pr
        self.pulls: list[dict] = []
        self.tokens: list[str] = []
        # L4: issues and their comments; `sha_for(branch)` reports pushed head SHAs.
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}
        self.sha_for = None
        self.fail_issue_reads = False

    def add_issue(self, number, title, body="", labels=("forgeflow", "bug"), state="open"):
        self.issues[number] = {
            "number": number,
            "html_url": f"https://github.com/octo/app/issues/{number}",
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": label} for label in labels],
            "updated_at": "2026-10-09T10:00:00Z",
        }
        return self.issues[number]

    def _issues(self, request):
        import json
        import re

        import httpx

        path = request.url.path
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)/comments", path)
        if m:
            number = int(m.group(1))
            if request.method == "POST":
                self.comments.setdefault(number, []).append(json.loads(request.content)["body"])
                return httpx.Response(201, json={"id": len(self.comments[number])})
            return httpx.Response(200, json=[{"body": b} for b in self.comments.get(number, [])])
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)", path)
        if m:
            if self.fail_issue_reads:
                return httpx.Response(502, json={"message": "bad gateway"})
            issue = self.issues.get(int(m.group(1)))
            return httpx.Response(200, json=issue) if issue else httpx.Response(404, json={})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path):
            wanted = {x for x in request.url.params.get("labels", "").split(",") if x}
            found = [
                i
                for i in self.issues.values()
                if i["state"] == request.url.params.get("state", "open")
                and wanted <= {label["name"] for label in i["labels"]}
            ]
            return httpx.Response(200, json=found)
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if m and request.method == "GET":
            pr = next((p for p in self.pulls if p["number"] == int(m.group(1))), None)
            if pr is None:
                return httpx.Response(404, json={})
            sha = self.sha_for(pr["head"]["ref"]) if self.sha_for else None
            return httpx.Response(200, json={**pr, "head": {**pr["head"], "sha": sha}})
        return None

    def __call__(self, request):
        import json

        import httpx

        self.tokens.append(request.headers.get("authorization", ""))
        if (handled := self._issues(request)) is not None:
            return handled
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if path.startswith("/repos/") and path.count("/") == 3 and request.method == "GET":
            return httpx.Response(
                200, json={"full_name": path[7:], "permissions": {"push": self.can_push}}
            )
        if path.endswith("/pulls") and request.method == "POST":
            body = json.loads(request.content)
            if self.existing_pr:
                return httpx.Response(422, json={"message": "A pull request already exists"})
            pr = {
                "number": len(self.pulls) + 1,
                "html_url": f"https://github.com/o/r/pull/{len(self.pulls) + 1}",
                "state": "open",
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "draft": body.get("draft", False),
                "title": body["title"],
                "body": body["body"],
            }
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        if path.endswith("/pulls") and request.method == "GET":
            head = request.url.params.get("head", "").split(":", 1)[-1]
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "html_url": "https://github.com/o/r/pull/7",
                        "state": "open",
                        "head": {"ref": head},
                        "base": {"ref": "main"},
                    }
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})
