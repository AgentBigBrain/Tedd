"""Exact source scope checks against existing read-only graph ownership.

No registration, subscription creation, acknowledgment, or global fallback occurs.
Remote checks run outside locks. Callers recheck local graph/configuration state
and archive denials before release; remote checks are not instantaneous revocation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, cast

from .source_contracts import (
    SourceArchiveError,
    SourceMetadata,
    SourceScope,
    decode_scope,
    digest,
    encode,
    identifier,
    object_fields,
    sha256,
    text,
)
from .v4_application_contracts import GraphReadScope, GraphReadSession, ReadScopeKind

_OPERATIONS = frozenset(
    {
        "capture",
        "discover",
        "inspect",
        "evidence",
        "original",
        "render",
        "owner_view",
        "owner_preview",
    }
)
_SENSITIVITIES = frozenset({"ordinary", "confidential", "medical", "restricted", "unknown"})


def access_binding(metadata: SourceMetadata, payload_sha256: str) -> str:
    """Bind a private retention/access decision to one exact observed artifact."""
    return digest(
        encode(
            [
                "source-access:1",
                metadata.scope.key,
                metadata.logical_key,
                metadata.artifact_key,
                sha256(payload_sha256),
            ]
        )
    )


@dataclass(frozen=True, repr=False)
class AccessDecision:
    """Trusted host callback result, never a JSON credential accepted from an agent."""

    binding: str
    operation: str
    audience: str
    status: str

    def __post_init__(self) -> None:
        sha256(self.binding)
        if self.operation not in _OPERATIONS or self.audience not in ("model", "owner"):
            raise SourceArchiveError("scope_denied")
        if self.status not in ("allowed", "denied", "unknown"):
            raise SourceArchiveError("scope_denied")


ProviderCheck = Callable[[SourceMetadata, str, str, str], AccessDecision]


@dataclass(frozen=True, repr=False)
class SourceAccess:
    """One operation audience under owner-approved private configuration.

    Call outside archive/graph/config locks, then recheck current local state before
    releasing content. Remote revocation after a successful check is not observable
    instantaneously. No provider adapter is registered by this value.
    """

    scope: SourceScope
    audience: str
    sensitivities: tuple[str, ...]
    local_retention: bool = False
    retained_bindings: tuple[str, ...] = ()
    provider_check: ProviderCheck | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SourceScope) or self.audience not in ("model", "owner"):
            raise SourceArchiveError("scope_denied")
        if type(self.local_retention) is not bool or type(self.sensitivities) is not tuple:
            raise SourceArchiveError()
        if not set(self.sensitivities) <= _SENSITIVITIES or len(set(self.sensitivities)) != len(
            self.sensitivities
        ):
            raise SourceArchiveError()
        if type(self.retained_bindings) is not tuple or len(self.retained_bindings) > 128:
            raise SourceArchiveError("limit_exceeded")
        for binding in self.retained_bindings:
            sha256(binding)

    def validate_metadata(self, metadata: SourceMetadata, operation: str) -> None:
        """Check local selection policy before acquiring an as-yet unhashed response."""
        if metadata.scope.key != self.scope.key or operation not in _OPERATIONS:
            raise SourceArchiveError("scope_denied")
        if operation.startswith("owner_") and self.audience != "owner":
            raise SourceArchiveError("scope_denied")
        details = metadata.details
        sensitivity = details.sensitivity if details else "unknown"
        if sensitivity not in self.sensitivities:
            raise SourceArchiveError("scope_denied")

    def authorize(self, metadata: SourceMetadata, payload_sha256: str, operation: str) -> None:
        self.validate_metadata(metadata, operation)
        details = metadata.details
        binding = access_binding(metadata, payload_sha256)
        retained = details is not None and details.retention == "retained_copy"
        if (
            retained
            and details is not None
            and details.provider == "local"
            and self.local_retention
        ):
            return
        if retained and operation != "capture" and binding in self.retained_bindings:
            return
        if (
            retained
            and operation != "capture"
            and self.local_retention
            and metadata.capture_method == "owner_confirmed_export"
            and details is not None
            and details.provider == "google"
            and details.sensitivity == "ordinary"
            and details.representation == "provider_export"
            and details.provider_revision is None
        ):
            # This marker comes from the verified archive, never a capture grant.
            # Owner-approved retained copies are offline evidence, not current
            # Google permission. Other provider acquisitions keep their old policy.
            return
        if operation == "owner_preview":
            # A standing attachment cannot perform the next access-dependent check.
            raise SourceArchiveError("capability_unavailable")
        if self.provider_check is None:
            raise SourceArchiveError("capability_unavailable")
        decision = self.provider_check(metadata, payload_sha256, operation, self.audience)
        if not isinstance(decision, AccessDecision) or (
            decision.binding,
            decision.operation,
            decision.audience,
        ) != (binding, operation, self.audience):
            raise SourceArchiveError("scope_denied")
        if decision.status != "allowed":
            raise SourceArchiveError(
                "source_unavailable" if decision.status == "unknown" else "scope_denied"
            )


def decode_access_policy(
    value: object, *, scope: SourceScope, audience: str
) -> SourceAccess | None:
    """Decode only local policy. There is deliberately no serializable provider grant."""
    if value is None:
        return None
    obj = object_fields(
        value,
        {"format", "scope", "audience", "sensitivities", "local_retention", "retained_bindings"},
    )
    if type(obj["format"]) is not int or obj["format"] != 1 or obj["audience"] != audience:
        raise SourceArchiveError("scope_denied")
    sensitivities, bindings, local = (
        obj["sensitivities"],
        obj["retained_bindings"],
        obj["local_retention"],
    )
    if (
        not isinstance(sensitivities, list)
        or not isinstance(bindings, list)
        or type(local) is not bool
    ):
        raise SourceArchiveError()
    result = SourceAccess(
        decode_scope(obj["scope"]),
        audience,
        tuple(text(item) for item in sensitivities),
        local,
        tuple(sha256(item) for item in bindings),
    )
    if result.scope.key != scope.key:
        raise SourceArchiveError("scope_denied")
    return result


class AuthorityStore(Protocol):
    readonly: bool

    def read_snapshot(self) -> AbstractContextManager[None]: ...
    def state(self) -> Mapping[str, object]: ...
    def subscription_identity(self, subscription_id: str) -> Mapping[str, object] | None: ...
    def context_pack(
        self, subscription_id: str, *, max_items: int = 100, max_chars: int = 50_000
    ) -> Mapping[str, object]: ...
    def get_record(self, record_id: str, revision: int | None = None) -> Mapping[str, object]: ...


def authorized_scope(
    store: AuthorityStore,
    session: GraphReadSession,
    scope: GraphReadScope,
    subjects: tuple[str, ...],
) -> SourceScope:
    if not store.readonly or scope.kind is not ReadScopeKind.SUBSCRIPTION:
        raise SourceArchiveError("scope_denied")
    subscription = identifier(scope.subscription_id)
    for value in (session.agent_id, session.task_id, session.consumer_id, session.run_id):
        identifier(value)
    if not subjects or len(set(subjects)) != len(subjects):
        raise SourceArchiveError("scope_denied")
    with store.read_snapshot():
        identity = store.subscription_identity(subscription)
        if (
            not identity
            or identity.get("active") is not True
            or any(
                identity.get(key) != expected
                for key, expected in (
                    ("subscription_id", subscription),
                    ("consumer_id", session.consumer_id),
                    ("agent_id", session.agent_id),
                    ("task_id", session.task_id),
                )
            )
        ):
            raise SourceArchiveError("scope_denied")
        # Existing context_pack exposes the authoritative selector without a new
        # SQL path. Its one bounded record is not returned or persisted here.
        selector_value = store.context_pack(subscription, max_items=1, max_chars=1000).get(
            "selector"
        )
        if not isinstance(selector_value, Mapping):
            raise SourceArchiveError("scope_denied")
        selector = cast(Mapping[str, object], selector_value)
        selected = selector.get("entity_ids")
        records = selector.get("record_ids")
        if (
            selector.get("all") is not False
            or selector.get("collections")
            or selector.get("discovery_kinds")
            or not isinstance(selected, list)
            or not isinstance(records, list)
            or set(selected) != set(subjects)
            or not set(records) <= set(subjects)
        ):
            raise SourceArchiveError("scope_denied")
        namespaces: set[str] = set()
        for subject in subjects:
            current = store.get_record(identifier(subject))
            payload = current.get("payload")
            if (
                current.get("record_id") != subject
                or current.get("collection") != "entities"
                or current.get("payload_available") is not True
                or not isinstance(payload, Mapping)
            ):
                raise SourceArchiveError("scope_denied")
            record = cast(Mapping[str, object], payload)
            if (
                record.get("lifecycle") != "active"
                or record.get("sensitive") is not False
                or record.get("status") != "current"
            ):
                raise SourceArchiveError("scope_denied")
            namespaces.add(text(record.get("namespace"), maximum=128))
        if len(namespaces) != 1:
            raise SourceArchiveError("scope_denied")
        state = store.state()
        generation = state.get("generation")
        if type(generation) is not int or generation < 1:
            raise SourceArchiveError()
        return SourceScope(
            identifier(state.get("store_id")),
            str(generation),
            tuple(sorted(subjects)),
            next(iter(namespaces)),
        )
