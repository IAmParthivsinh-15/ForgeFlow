"""Jenkins CI provider (spec sections 56, 191).

Typed operations over the Jenkins REST API: create/update a pipeline job from a
rendered Jenkinsfile, trigger it, wait for the queue item and build, and fetch
stages and logs. Credentials come from settings (local instance in docker
compose) and are never shown to agents.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx

from forgeflow.core.errors import CIUnavailable
from forgeflow.schemas.verification import CIBuild, CIStage

LOG_TAIL_CHARS = 20_000


class CIProvider(Protocol):
    name: str

    async def run(self, job: str, pipeline: str, commit: str) -> CIBuild: ...


def job_config_xml(pipeline: str, description: str) -> str:
    return (
        "<?xml version='1.1' encoding='UTF-8'?>\n"
        '<flow-definition plugin="workflow-job">\n'
        f"  <description>{escape(description)}</description>\n"
        "  <keepDependencies>false</keepDependencies>\n"
        "  <properties/>\n"
        '  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" '
        'plugin="workflow-cps">\n'
        f"    <script>{escape(pipeline)}</script>\n"
        "    <sandbox>true</sandbox>\n"
        "  </definition>\n"
        "  <disabled>false</disabled>\n"
        "</flow-definition>\n"
    )


_RESULT_MAP = {
    "SUCCESS": "SUCCESS",
    "FAILURE": "FAILURE",
    "UNSTABLE": "UNSTABLE",
    "ABORTED": "ABORTED",
    "NOT_BUILT": "ABORTED",
}


class JenkinsProvider:
    name = "jenkins"

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        poll_interval: float = 3.0,
        timeout: float = 1800.0,
        transport: httpx.AsyncBaseTransport | None = None,
        public_url: str | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.public_url = (public_url or url).rstrip("/")
        self.auth = httpx.BasicAuth(user, password)
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            auth=self.auth,
            timeout=30.0,
            transport=self.transport,
            follow_redirects=False,
        )

    async def run(self, job: str, pipeline: str, commit: str) -> CIBuild:
        started = time.perf_counter()
        try:
            async with self._client() as client:
                crumb = await self._crumb(client)
                await self._upsert_job(client, crumb, job, pipeline, commit)
                queue_url = await self._trigger(client, crumb, job)
                number = await self._wait_for_build_number(client, queue_url)
                info = await self._wait_for_result(client, job, number, started)
                log = await self._log(client, job, number)
                stages = await self._stages(client, job, number)
        except httpx.HTTPError as exc:
            raise CIUnavailable(f"Jenkins request failed: {type(exc).__name__}: {exc}") from exc
        status = "TIMEOUT" if info is None else _RESULT_MAP.get(info.get("result") or "", "FAILURE")
        return CIBuild(
            provider=self.name,
            job=job,
            build_number=number,
            url=f"{self.public_url}/job/{job}/{number}/",
            status=status,  # type: ignore[arg-type]
            stages=stages,
            log_tail=log[-LOG_TAIL_CHARS:],
            duration_ms=int((info or {}).get("duration") or (time.perf_counter() - started) * 1000),
            commit=commit,
        )

    async def _crumb(self, client: httpx.AsyncClient) -> dict[str, str]:
        r = await client.get("/crumbIssuer/api/json")
        if r.status_code == 404:  # CSRF protection disabled
            return {}
        self._check(r, "fetch CSRF crumb")
        data = r.json()
        return {data["crumbRequestField"]: data["crumb"]}

    async def _upsert_job(
        self, client: httpx.AsyncClient, crumb: dict, job: str, pipeline: str, commit: str
    ) -> None:
        xml = job_config_xml(pipeline, f"ForgeFlow pipeline (last commit {commit[:12]})")
        headers = {**crumb, "Content-Type": "application/xml"}
        exists = await client.get(f"/job/{job}/api/json")
        if exists.status_code == 200:
            r = await client.post(f"/job/{job}/config.xml", content=xml, headers=headers)
        else:
            r = await client.post("/createItem", params={"name": job}, content=xml, headers=headers)
        self._check(r, "create/update job")

    async def _trigger(self, client: httpx.AsyncClient, crumb: dict, job: str) -> str:
        r = await client.post(f"/job/{job}/build", headers=crumb)
        if r.status_code not in (200, 201):
            self._check(r, "trigger build")
        location = r.headers.get("Location")
        if not location:
            raise CIUnavailable("Jenkins did not return a queue location")
        return location

    async def _wait_for_build_number(self, client: httpx.AsyncClient, queue_url: str) -> int:
        # Jenkins builds Location from its configured root URL, which may be the public
        # address; only the path is meaningful from inside the network.
        path = urlsplit(queue_url).path.rstrip("/") + "/api/json"
        deadline = time.monotonic() + min(self.timeout, 600)
        while time.monotonic() < deadline:
            r = await client.get(path)
            self._check(r, "read queue item")
            data = r.json()
            if data.get("cancelled"):
                raise CIUnavailable("build was cancelled while queued")
            executable = data.get("executable")
            if executable and executable.get("number"):
                return int(executable["number"])
            await asyncio.sleep(self.poll_interval)
        raise CIUnavailable("build did not leave the Jenkins queue in time")

    async def _wait_for_result(
        self, client: httpx.AsyncClient, job: str, number: int, started: float
    ) -> dict | None:
        while time.perf_counter() - started < self.timeout:
            r = await client.get(f"/job/{job}/{number}/api/json")
            self._check(r, "read build")
            data = r.json()
            if not data.get("building") and data.get("result"):
                return data
            await asyncio.sleep(self.poll_interval)
        return None

    async def _log(self, client: httpx.AsyncClient, job: str, number: int) -> str:
        r = await client.get(f"/job/{job}/{number}/consoleText")
        return r.text if r.status_code == 200 else ""

    async def _stages(self, client: httpx.AsyncClient, job: str, number: int) -> list[CIStage]:
        r = await client.get(f"/job/{job}/{number}/wfapi/describe")
        if r.status_code != 200:
            return []  # pipeline-stage-view plugin not installed
        return [
            CIStage(
                name=s.get("name", "?"),
                status=s.get("status", "?"),
                duration_ms=int(s.get("durationMillis") or 0),
            )
            for s in r.json().get("stages", [])
        ]

    @staticmethod
    def _check(response: httpx.Response, action: str) -> None:
        if response.status_code >= 400:
            raise CIUnavailable(
                f"Jenkins could not {action}: HTTP {response.status_code} {response.text[:200]}"
            )
