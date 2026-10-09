"""GitHub plugin: typed, deterministic operations (spec sections 58, 192).

Agents never call GitHub directly; ForgeFlow performs these operations through the
capability gateway (policy, approval, audit). Only forgeflow/* branches are pushed;
merging is intentionally not implemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from forgeflow.core.errors import ForgeFlowError, ValidationFailed
from forgeflow.tools.git.client import validate_ref, validate_sha

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REMOTE_RE = re.compile(r"github\.com[:/](?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitHubError(ForgeFlowError):
    retryable = True


class GitHubAuthError(GitHubError):
    retryable = False


def validate_repository(repository: str) -> str:
    if not _REPO_RE.match(repository):
        raise ValidationFailed(f"repository must look like owner/repo: {repository!r}")
    return repository


def repository_from_remote(url: str) -> str | None:
    """'git@github.com:o/r.git' or 'https://github.com/o/r' -> 'o/r'."""
    match = _REMOTE_RE.search(url.strip())
    return match.group("repo") if match else None


@dataclass
class PullRequest:
    number: int
    url: str
    state: str
    head: str
    base: str
    draft: bool = False
    head_sha: str | None = None
    merged: bool = False


@dataclass
class Issue:
    repository: str
    number: int
    url: str
    title: str
    body: str
    state: str
    labels: list[str]
    updated_at: str | None
    is_pull_request: bool = False


def verify_webhook_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """GitHub `X-Hub-Signature-256` (HMAC-SHA256 of the raw body), constant-time."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def issue_from_api(repository: str, data: dict[str, Any]) -> Issue:
    return Issue(
        repository=repository,
        number=int(data["number"]),
        url=str(data.get("html_url") or ""),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or "open"),
        labels=sorted(
            str(label["name"] if isinstance(label, dict) else label)
            for label in data.get("labels") or []
        ),
        updated_at=data.get("updated_at"),
        is_pull_request="pull_request" in data,
    )


def marker(trace_id: str, kind: str) -> str:
    """Hidden marker that makes ForgeFlow comments idempotent across retries/resumes."""
    return f"<!-- forgeflow:{trace_id}:{kind} -->"


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport

    def __repr__(self) -> str:  # never print the token
        return f"GitHubClient(api_url={self.api_url!r})"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ForgeFlow",
            },
            timeout=30.0,
            transport=self.transport,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise GitHubAuthError(
                f"GitHub refused the request ({response.status_code}): "
                f"{response.json().get('message', '') if response.content else ''}"
            )
        return response

    async def get_user(self) -> str:
        r = await self._request("GET", "/user")
        if r.status_code != 200:
            raise GitHubError(f"GET /user failed: {r.status_code}")
        return str(r.json()["login"])

    async def get_repository(self, repository: str) -> dict[str, Any]:
        r = await self._request("GET", f"/repos/{validate_repository(repository)}")
        if r.status_code == 404:
            raise GitHubAuthError(f"repository {repository} not found or not accessible")
        if r.status_code != 200:
            raise GitHubError(f"GET repository failed: {r.status_code}")
        return r.json()

    async def find_open_pull_request(self, repository: str, head: str) -> PullRequest | None:
        owner = repository.split("/", 1)[0]
        r = await self._request(
            "GET", f"/repos/{repository}/pulls", params={"head": f"{owner}:{head}", "state": "open"}
        )
        if r.status_code == 200 and r.json():
            return _pr(r.json()[0])
        return None

    async def create_pull_request(
        self, repository: str, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        validate_repository(repository)
        validate_ref(head)
        validate_ref(base)
        r = await self._request(
            "POST",
            f"/repos/{repository}/pulls",
            json={
                "title": title[:250],
                "head": head,
                "base": base,
                "body": body[:65_000],
                "draft": draft,
                "maintainer_can_modify": True,
            },
        )
        if r.status_code == 201:
            return _pr(r.json())
        if r.status_code == 422 and "already exists" in r.text:
            existing = await self.find_open_pull_request(repository, head)
            if existing:
                return existing  # idempotent: a retry finds the PR it already opened
        raise GitHubError(f"creating the pull request failed: {r.status_code} {r.text[:300]}")

    async def comment(self, repository: str, number: int, body: str) -> None:
        r = await self._request(
            "POST", f"/repos/{repository}/issues/{number}/comments", json={"body": body[:65_000]}
        )
        if r.status_code != 201:
            raise GitHubError(f"commenting failed: {r.status_code}")

    async def get_issue(self, repository: str, number: int) -> Issue:
        r = await self._request("GET", f"/repos/{validate_repository(repository)}/issues/{number}")
        if r.status_code == 404:
            raise GitHubError(f"issue {repository}#{number} not found")
        if r.status_code != 200:
            raise GitHubError(f"reading the issue failed: {r.status_code}")
        return issue_from_api(repository, r.json())

    async def list_issues(
        self, repository: str, labels: list[str], state: str = "open", limit: int = 100
    ) -> list[Issue]:
        """Issues (not pull requests) with all `labels`, newest update first."""
        r = await self._request(
            "GET",
            f"/repos/{validate_repository(repository)}/issues",
            params={
                "labels": ",".join(labels),
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": min(limit, 100),
            },
        )
        if r.status_code != 200:
            raise GitHubError(f"listing issues failed: {r.status_code}")
        issues = [issue_from_api(repository, d) for d in r.json()]
        return [i for i in issues if not i.is_pull_request]

    async def list_comments(self, repository: str, number: int) -> list[str]:
        r = await self._request(
            "GET",
            f"/repos/{validate_repository(repository)}/issues/{number}/comments",
            params={"per_page": 100},
        )
        if r.status_code != 200:
            raise GitHubError(f"listing comments failed: {r.status_code}")
        return [str(c.get("body") or "") for c in r.json()]

    async def comment_once(
        self, repository: str, number: int, body: str, trace_id: str, kind: str
    ) -> bool:
        """Post `body` unless a comment with the same trace marker exists. True if posted."""
        tag = marker(trace_id, kind)
        if any(tag in existing for existing in await self.list_comments(repository, number)):
            return False
        await self.comment(repository, number, f"{body}\n\n{tag}")
        return True

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        r = await self._request("GET", f"/repos/{repository}/pulls/{number}")
        if r.status_code != 200:
            raise GitHubError(f"reading the pull request failed: {r.status_code}")
        return _pr(r.json())


def _pr(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(data["number"]),
        url=data["html_url"],
        state=data.get("state", "open"),
        head=data["head"]["ref"],
        base=data["base"]["ref"],
        draft=bool(data.get("draft")),
        head_sha=data["head"].get("sha"),
        merged=bool(data.get("merged")),
    )


async def push_branch(
    repo: Path,
    commit: str,
    branch: str,
    token: str,
    repository: str | None = None,
    remote_url: str | None = None,
    limit_s: float = 300,
) -> None:
    """Push `commit` to refs/heads/<branch> on GitHub.

    The token is passed to git through the environment and an inline credential
    helper, so it never appears in argv, the remote URL, logs or .git/config.
    Only forgeflow/* branches may be pushed, and never with --force.
    """
    validate_sha(commit)
    validate_ref(branch)
    if not branch.startswith("forgeflow/"):
        raise ValidationFailed("ForgeFlow only pushes forgeflow/* branches")
    url = remote_url or f"https://github.com/{validate_repository(repository or '')}.git"
    helper = '!f() { echo username=x-access-token; echo "password=$FORGEFLOW_GIT_TOKEN"; }; f'
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(FORGEFLOW_GIT_TOKEN=token, GIT_TERMINAL_PROMPT="0")
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={helper}",
        "push",
        "--porcelain",
        url,
        f"{commit}:refs/heads/{branch}",
        cwd=str(repo),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError as exc:
        proc.kill()
        raise GitHubError("git push timed out") from exc
    if proc.returncode != 0:
        message = err.decode("utf-8", "replace").replace(token, "***")[:400]
        if "Authentication failed" in message or "403" in message:
            raise GitHubAuthError(f"git push was refused: {message}")
        raise GitHubError(f"git push failed: {message}")
