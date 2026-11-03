"""Composition-root mapping of backend failures to safe read failures.

This module deliberately receives the storage module from a composition root.
The transport-neutral application service therefore never imports a facade or
storage adapter merely to classify implementation exceptions.
"""

from __future__ import annotations

from .v4_application_contracts import GraphReadErrorCategory, GraphReadFailure


def _is_storage_error(error: Exception, storage: object, name: str) -> bool:
    candidate = getattr(storage, name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseException):
        return False
    return isinstance(error, candidate)


def classify_graph_backend_error(error: Exception, storage: object) -> GraphReadFailure:
    """Map known backend failures without returning private implementation text."""

    if _is_storage_error(error, storage, "PlanTokenError"):
        return GraphReadFailure(GraphReadErrorCategory.INVALID_REQUEST, "Malformed signed token")
    if _is_storage_error(error, storage, "StoreFormatError") or _is_storage_error(
        error, storage, "ValidationError"
    ):
        return GraphReadFailure(GraphReadErrorCategory.UNHEALTHY_GRAPH, "graph is unhealthy")
    if _is_storage_error(error, storage, "PathSafetyError") or isinstance(error, OSError):
        return GraphReadFailure(
            GraphReadErrorCategory.UNAVAILABLE_CAPABILITY, "graph is unavailable"
        )
    if _is_storage_error(error, storage, "ConcurrencyConflict") or _is_storage_error(
        error, storage, "IdentityConflict"
    ):
        return GraphReadFailure(
            GraphReadErrorCategory.CONFLICT, "graph state changed; retry request"
        )
    if _is_storage_error(error, storage, "SubscriptionError"):
        return GraphReadFailure(
            GraphReadErrorCategory.SCOPE_DENIED, "subscription scope is unavailable"
        )
    if isinstance(error, ValueError):
        if "cursor" in str(error).lower():
            return GraphReadFailure(GraphReadErrorCategory.INVALID_REQUEST, "cursor is invalid")
        return GraphReadFailure(GraphReadErrorCategory.INVALID_REQUEST, "graph request is invalid")
    return GraphReadFailure(
        GraphReadErrorCategory.INTERNAL_ERROR, "graph request could not be completed"
    )


__all__ = ["classify_graph_backend_error"]
