"""L4 operations: Vault secret backend, operator CLI, webhook signature."""

import json

import httpx
import pytest

from forgeflow import cli
from forgeflow.extensibility.secrets import SecretStore, SecretStoreError, VaultBackend
from forgeflow.extensibility.store import InMemoryDocumentStore
from forgeflow.integrations.github.client import verify_webhook_signature


class FakeVault:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.tokens: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("x-vault-token", ""))
        if request.headers.get("x-vault-token") != "vault-token":
            return httpx.Response(403)
        path = request.url.path
        if request.method == "POST" and "/data/" in path:
            self.data[path.split("/data/", 1)[1]] = json.loads(request.content)["data"]["value"]
            return httpx.Response(200, json={})
        if request.method == "GET" and "/data/" in path:
            key = path.split("/data/", 1)[1]
            if key not in self.data:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": {"data": {"value": self.data[key]}}})
        if request.method == "DELETE" and "/metadata/" in path:
            self.data.pop(path.split("/metadata/", 1)[1], None)
            return httpx.Response(204)
        return httpx.Response(400)


async def test_vault_backend_keeps_values_out_of_the_database():
    vault = FakeVault()
    store = InMemoryDocumentStore()
    secrets = SecretStore(
        store,
        "",
        VaultBackend("https://vault.test", "vault-token", transport=httpx.MockTransport(vault)),
    )
    assert secrets.available
    await secrets.put("secret://connector/con_1", "github_pat_supersecret")
    assert vault.data == {"forgeflow/connector/con_1": "github_pat_supersecret"}
    assert "supersecret" not in json.dumps(store.data["secrets"], default=str)  # reference only
    assert await secrets.get("secret://connector/con_1") == "github_pat_supersecret"
    await secrets.delete("secret://connector/con_1")
    assert vault.data == {}
    with pytest.raises(SecretStoreError):
        await SecretStore(
            store,
            "",
            VaultBackend("https://vault.test", "wrong", transport=httpx.MockTransport(vault)),
        ).get("secret://x/y")
    with pytest.raises(SecretStoreError):
        VaultBackend("", "")
    assert "vault-token" not in repr(VaultBackend("https://vault.test", "vault-token"))


def test_webhook_signature_verification():
    body = b'{"action":"opened"}'
    import hashlib
    import hmac

    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature("s3cret", body, good)
    assert not verify_webhook_signature("s3cret", body + b" ", good)
    assert not verify_webhook_signature("", body, good)  # no secret configured = refuse
    assert not verify_webhook_signature("s3cret", body, None)


def test_cli_emergency_stop_calls_the_api(monkeypatch, capsys):
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        return {
            "status": "STOPPED",
            "stop": {"requested_at": "t1", "stopped_at": "t2", "cancelled_tasks": ["a", "b"]},
        }

    monkeypatch.setattr(cli, "_call", fake_call)
    cli.main(["run", "stop", "trace_abc", "--reason", "operator emergency stop"])
    assert calls == [
        ("POST", "/api/v1/autonomy/runs/trace_abc/stop", {"reason": "operator emergency stop"})
    ]
    out = capsys.readouterr().out
    assert "STOPPED" in out and "halted tasks: 2" in out
    with pytest.raises(SystemExit):
        cli.main(["run", "stop", "trace_abc"])  # a reason is mandatory
