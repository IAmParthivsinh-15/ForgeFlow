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
