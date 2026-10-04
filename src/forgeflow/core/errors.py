from __future__ import annotations

from typing import Any


class ForgeFlowError(Exception):
    """Base error. `retryable` drives failure-recovery decisions (spec section 49)."""

    retryable: bool = False


class NotFoundError(ForgeFlowError):
    pass


class InvalidStateTransition(ForgeFlowError):
    pass


class ValidationFailed(ForgeFlowError):
    pass


class PolicyViolation(ForgeFlowError):
    pass


class AllProvidersFailed(ForgeFlowError):
    retryable = True

    def __init__(self, message: str, attempts: list[Any] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class ConcurrencyConflict(ForgeFlowError):
    """An optimistic-concurrency check failed; the caller should reload and retry."""

    retryable = True


class GitError(ForgeFlowError):
    """A git command failed. Not retryable: the repository state needs attention."""


class InvalidTaskGraph(ForgeFlowError):
    """The proposed task graph is malformed (cycle, unknown dependency, ...)."""


class MergeConflictUnresolved(ForgeFlowError):
    """Integration could not resolve a merge conflict."""


class CIUnavailable(ForgeFlowError):
    """The CI system could not be reached or rejected the request; worth retrying."""

    retryable = True
