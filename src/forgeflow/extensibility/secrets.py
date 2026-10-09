"""Credential storage behind a reference (spec sections 71, 204).

Connector and MCP records hold only `credential_ref` ("secret://connector/con_123").
The value itself is encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using
FORGEFLOW_SECRET_KEY, which lives in the environment, never in the database. The
ciphertext is kept in its own `secrets` collection.

Production (additional.md section 4): SECRET_BACKEND=vault keeps the values in
HashiCorp Vault KV v2 instead; ForgeFlow stores only the reference and receives the
Vault token from the environment at runtime. The interface is the same either way.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from cryptography.fernet import Fernet, InvalidToken

from forgeflow.core.errors import ForgeFlowError, NotFoundError
from forgeflow.core.ids import utcnow
from forgeflow.extensibility.store import DocumentStore


class SecretStoreError(ForgeFlowError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBackend(Protocol):
    async def put(self, ref: str, value: str) -> None: ...

    async def get(self, ref: str) -> str: ...

    async def delete(self, ref: str) -> None: ...


class VaultBackend:
    """HashiCorp Vault KV v2: `secret://connector/c1` -> <mount>/data/<prefix>/connector/c1."""

    def __init__(
        self,
        addr: str,
        token: str,
        mount: str = "secret",
        prefix: str = "forgeflow",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not addr or not token:
            raise SecretStoreError("SECRET_BACKEND=vault needs VAULT_ADDR and VAULT_TOKEN")
        self.addr = addr.rstrip("/")
        self.mount = mount.strip("/")
        self.prefix = prefix.strip("/")
        self._token = token
        self.transport = transport

    def __repr__(self) -> str:  # never print the token
        return f"VaultBackend(addr={self.addr!r}, mount={self.mount!r})"

    def _path(self, ref: str) -> str:
        key = ref.removeprefix("secret://").strip("/")
        if not key or ".." in key.split("/"):
            raise SecretStoreError("invalid secret reference")
        return f"{self.prefix}/{key}"

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=10, transport=self.transport, headers={"X-Vault-Token": self._token}
            ) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise SecretStoreError(f"Vault unreachable: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise SecretStoreError("Vault refused the token")
        return response

    async def put(self, ref: str, value: str) -> None:
        r = await self._request(
            "POST",
            f"{self.addr}/v1/{self.mount}/data/{self._path(ref)}",
            json={"data": {"value": value}},
        )
        if r.status_code >= 400:
            raise SecretStoreError(f"Vault write failed: {r.status_code}")

    async def get(self, ref: str) -> str:
        r = await self._request("GET", f"{self.addr}/v1/{self.mount}/data/{self._path(ref)}")
        if r.status_code == 404:
            raise NotFoundError(f"secret {ref} not found (revoked?)")
        if r.status_code >= 400:
            raise SecretStoreError(f"Vault read failed: {r.status_code}")
        return str(r.json()["data"]["data"]["value"])

    async def delete(self, ref: str) -> None:
        r = await self._request("DELETE", f"{self.addr}/v1/{self.mount}/metadata/{self._path(ref)}")
        if r.status_code >= 400 and r.status_code != 404:
            raise SecretStoreError(f"Vault delete failed: {r.status_code}")


class SecretStore:
    def __init__(
        self, store: DocumentStore, key: str, backend: SecretBackend | None = None
    ) -> None:
        self.store = store
        self.backend = backend
        self._fernet: Fernet | None = None
        if key:
            try:
                self._fernet = Fernet(key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise SecretStoreError("FORGEFLOW_SECRET_KEY is not a valid Fernet key") from exc

    @property
    def available(self) -> bool:
        return self.backend is not None or self._fernet is not None

    def _require(self) -> Fernet:
        if self._fernet is None:
            raise SecretStoreError(
                "FORGEFLOW_SECRET_KEY is not set; generate one with "
                "`python -m forgeflow.scripts.generate_secret_key` and add it to .env"
            )
        return self._fernet

    async def put(self, ref: str, value: str) -> str:
        if not ref.startswith("secret://"):
            raise SecretStoreError("secret references must start with secret://")
        if self.backend is not None:
            await self.backend.put(ref, value)
            await self.store.put(
                "secrets", {"ref": ref, "backend": "vault", "created_at": utcnow()}
            )
            return ref
        token = self._require().encrypt(value.encode("utf-8")).decode("ascii")
        await self.store.put("secrets", {"ref": ref, "ciphertext": token, "created_at": utcnow()})
        return ref

    async def get(self, ref: str) -> str:
        if self.backend is not None:
            return await self.backend.get(ref)
        doc = await self.store.get("secrets", ref)
        if doc is None:
            raise NotFoundError(f"secret {ref} not found (revoked?)")
        try:
            return self._require().decrypt(doc["ciphertext"].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretStoreError("secret cannot be decrypted with the current key") from exc

    async def delete(self, ref: str) -> None:
        if self.backend is not None:
            await self.backend.delete(ref)
        await self.store.delete("secrets", ref)
