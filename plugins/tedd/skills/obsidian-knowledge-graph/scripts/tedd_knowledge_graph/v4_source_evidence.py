"""Explicit graph-linked evidence read. Ordinary recall never imports this module."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Protocol, cast

from .source_archive import Archive
from .source_authority import AuthorityStore, authorized_scope
from .source_contracts import (
    SourceArchiveError,
    SourceRef,
    SourceScope,
    digest,
    encode,
    encode_units,
    identifier,
    parse_source_uri,
    positive,
    safe_display,
)
from .source_presentation import (
    MAX_SOURCE_VIEWS,
    PreparedSourceView,
    PreviewPolicy,
    RetainedSourceView,
)
from .v4_application_contracts import GraphReadScope, GraphReadSession


class EvidenceStore(AuthorityStore, Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    def history(
        self, record_id: str, *, limit: int = 100, before_revision: int | None = None
    ) -> Mapping[str, object]: ...
    def current_claims(
        self, surface_ids: Iterable[str], *, limit: int = 500
    ) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True)
class EvidenceRequest:
    session: GraphReadSession
    scope: GraphReadScope
    subject_ids: tuple[str, ...]
    record_id: str
    revision: int | None = None
    max_chars: int = 12_000
    max_versions: int = 5

    def __post_init__(self) -> None:
        identifier(self.record_id)
        if self.revision is not None:
            positive(self.revision)
        if (type(self.max_chars) is int and not 1 <= self.max_chars <= 48_000) or (
            type(self.max_versions) is int and not 1 <= self.max_versions <= 5
        ):
            raise SourceArchiveError("limit_exceeded")
        positive(self.max_chars, maximum=48_000)
        positive(self.max_versions, maximum=5)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SourceArchiveError()
    return cast(Mapping[str, object], value)


def _ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise SourceArchiveError("limit_exceeded")
    return tuple(identifier(item) for item in value)


def _payload(record: Mapping[str, object]) -> Mapping[str, object]:
    if record.get("payload_available") is not True:
        raise SourceArchiveError("scope_denied")
    payload = _mapping(record.get("payload"))
    if (
        payload.get("sensitive") is True
        or payload.get("lifecycle") in ("redacted", "redirected")
        or payload.get("status") == "redacted"
        or payload.get("authority_state") == "redacted"
    ):
        raise SourceArchiveError("scope_denied")
    return payload


def _within(record: Mapping[str, object], scope: SourceScope) -> Mapping[str, object]:
    payload = _payload(record)
    subjects = set(_ids(payload.get("subject_ids"))) | set(_ids(payload.get("entity_refs")))
    subjects |= set(_ids(payload.get("entity_ids")))
    subjects |= set(_ids(payload.get("subject_refs")))
    subjects |= set(_ids(payload.get("owner_refs")))
    if record.get("collection") == "relations":
        subjects.update(identifier(payload.get(field)) for field in ("source", "target"))
    if record.get("collection") == "entities":
        subjects.add(identifier(record.get("record_id")))
    if not subjects or not subjects <= set(scope.subject_ids):
        raise SourceArchiveError("scope_denied")
    return payload


def _at(store: EvidenceStore, record_id: str, watermark: int) -> Mapping[str, object]:
    """Read the lineage version that existed at the cited record's mutation."""
    current = store.get_record(record_id)
    _payload(current)  # Current redaction always wins over old payload access.
    if positive(current.get("watermark")) <= watermark:
        return current
    result = store.history(record_id, limit=100)
    values = result.get("history")
    if not isinstance(values, list):
        raise SourceArchiveError()
    for value in values:
        item = _mapping(value)
        if positive(item.get("watermark")) <= watermark:
            return {**item, "record_id": record_id, "collection": current["collection"]}
    raise SourceArchiveError("limit_exceeded" if result.get("has_more") else "source_unavailable")


def _links(payload: Mapping[str, object]) -> tuple[str, ...]:
    refs = _ids(payload.get("evidence_refs")) + _ids(payload.get("observation_refs"))
    single = payload.get("source_ref")
    if single:
        refs += (identifier(single),)
    return tuple(dict.fromkeys(refs))


def _summary(record: Mapping[str, object]) -> dict[str, object]:
    safe = _mapping(safe_display(dict(record)))
    payload = _mapping(safe.get("payload"))
    return {
        "record_id": record["record_id"],
        "revision": record["revision"],
        "watermark": record["watermark"],
        "collection": record["collection"],
        "payload": {
            key: payload[key]
            for key in (
                "value",
                "summary",
                "title",
                "family",
                "key",
                "lifecycle",
                "status",
                "active",
                "source_tier",
                "valid_from",
                "valid_to",
            )
            if key in payload
        },
    }


def forget_inventory(store: EvidenceStore, archive: Archive, ref: SourceRef) -> dict[str, object]:
    """Bounded, content-free shared impact; never a global content export.

    Inspect exact historical URI bindings as well as current refs. Refuse mixed
    source history because whole-record redaction would erase another original.
    Limits are refusals, never silently truncated approval lists.
    """
    manifests, remembered = archive.forget_identity(ref)
    prefix = f"tedd-source://{ref.archive_id}/{ref.scope_id}/"
    rows = store.connection.execute(
        "SELECT p.record_id, json_extract(p.payload_json, '$.uri') AS uri "
        "FROM record_revision_payloads p JOIN records r ON r.record_id=p.record_id "
        "WHERE r.collection='sources' AND json_extract(p.payload_json, '$.uri') LIKE ? LIMIT 2001",
        (prefix + "%",),
    ).fetchall()
    if len(rows) > 2000:
        raise SourceArchiveError("limit_exceeded")
    roots = {
        str(row[0]) for row in rows if parse_source_uri(str(row[1])).manifest_sha256 in manifests
    }
    if remembered:
        markers = ",".join("?" for _ in remembered)
        ids = store.connection.execute(
            f"SELECT record_id FROM records WHERE collection='sources' AND record_id IN ({markers})",
            remembered,
        ).fetchall()
        if len(ids) != len(remembered):
            raise SourceArchiveError("source_unavailable")
        roots.update(str(row[0]) for row in ids)
    if len(roots) > 500:
        raise SourceArchiveError("limit_exceeded")
    for record_id in roots:
        history = store.history(record_id, limit=100)
        if history.get("has_more"):
            raise SourceArchiveError("limit_exceeded")
        for raw in cast(list[object], history["history"]):
            item = _mapping(raw)
            if not item.get("payload_available"):
                continue
            payload = _mapping(item["payload"])
            if payload.get("lifecycle") == "redacted":
                continue
            uri = payload.get("uri")
            if (
                not isinstance(uri, str)
                or not uri.startswith(prefix)
                or parse_source_uri(uri).manifest_sha256 not in manifests
            ):
                raise SourceArchiveError("scope_denied")
    selected = set(roots)
    memories: set[str] = set()
    pending = set(roots)
    while pending:
        markers = ",".join("?" for _ in pending)
        rows = store.connection.execute(
            "SELECT DISTINCT rr.record_id FROM revision_refs rr "
            "JOIN record_revision_payloads p ON p.record_id=rr.record_id AND p.revision=rr.revision "
            f"WHERE rr.target_record_id IN ({markers}) LIMIT 501",
            tuple(sorted(pending)),
        ).fetchall()
        if len(rows) > 500:
            raise SourceArchiveError("limit_exceeded")
        pending = set()
        for row in rows:
            record_id = str(row[0])
            if record_id in selected or record_id in memories:
                continue
            record = store.get_record(record_id)
            payload = _mapping(record.get("payload"))
            if payload.get("lifecycle") == "redacted":
                continue
            _within(record, archive.scope)
            if record["collection"] == "observations":
                history = store.history(record_id, limit=100)
                if history.get("has_more"):
                    raise SourceArchiveError("limit_exceeded")
                for raw in cast(list[object], history["history"]):
                    item = _mapping(raw)
                    if (
                        not item.get("payload_available")
                        or _mapping(item["payload"]).get("lifecycle") == "redacted"
                    ):
                        continue
                    historical = _within({**item, "collection": "observations"}, archive.scope)
                    links = set(_links(historical))
                    if not links or links - roots:
                        raise SourceArchiveError("scope_denied")
                selected.add(record_id)
                pending.add(record_id)
            else:
                memories.add(record_id)
        if len(selected | memories) > 500:
            raise SourceArchiveError("limit_exceeded")
    targets: list[dict[str, object]] = []
    for record_id in sorted(selected):
        record = store.get_record(record_id)
        payload = _mapping(record.get("payload"))
        if payload.get("lifecycle") != "redacted":
            _payload(record)
            targets.append(
                {
                    "record_id": record_id,
                    "revision": record["revision"],
                    "collection": record["collection"],
                }
            )
    body: dict[str, object] = {
        "scope_sha256": archive.scope.key,
        "archive_id": archive.archive_id,
        "manifests": manifests,
        "state": dict(store.state()),
        "graph_evidence": targets,
        "retained_memories": sorted(memories),
        "source_records": sorted(roots),
    }
    return {**body, "impact_sha256": digest(encode(body))}


@dataclass(frozen=True, repr=False)
class _EvidenceSelection:
    state: dict[str, object]
    checked: Mapping[str, object]
    summaries: list[dict[str, object]]
    refs: tuple[SourceRef, ...]
    missing: int


def _select_evidence(
    store: EvidenceStore, archive: Archive, request: EvidenceRequest
) -> _EvidenceSelection:
    """Shared exact historical lineage selection; no archive bodies are read."""
    refs: list[SourceRef] = []
    with store.read_snapshot():
        scope = authorized_scope(store, request.session, request.scope, request.subject_ids)
        if scope.key != archive.scope.key:
            raise SourceArchiveError("scope_denied")
        state = dict(store.state())
        current = store.get_record(request.record_id)
        _within(current, scope)
        checked = store.get_record(request.record_id, request.revision)
        payload = _within(checked, scope)
        watermark = positive(checked.get("watermark"))
        summaries = [_summary(checked)]
        if checked["revision"] != current["revision"]:
            summaries.append(_summary(current))
        surface = payload.get("surface_ref")
        if isinstance(surface, str) and surface:
            for successor in store.current_claims((identifier(surface),), limit=5):
                _within(successor, scope)
                if successor["record_id"] != checked["record_id"]:
                    summaries.append(_summary(successor))
        pending = list(_links(payload))
        if checked.get("collection") == "sources":
            pending.insert(0, request.record_id)
        seen: set[str] = set()
        missing = 0
        while pending:
            if len(seen) >= 100:
                raise SourceArchiveError("limit_exceeded")
            record_id = pending.pop(0)
            if record_id in seen:
                continue
            seen.add(record_id)
            record = _at(store, record_id, watermark)
            content = _payload(record)
            if record.get("collection") == "sources":
                uri = content.get("uri")
                if isinstance(uri, str) and uri.startswith("tedd-source:"):
                    ref = parse_source_uri(uri)
                    if ref not in refs:
                        refs.append(ref)
                else:
                    missing += 1
            elif record.get("collection") == "observations":
                _within(record, scope)
                pending.extend(_links(content))
            else:
                raise SourceArchiveError("scope_denied")
    return _EvidenceSelection(state, checked, summaries, tuple(refs), missing)


def verify_evidence_state(
    store: EvidenceStore,
    archive: Archive,
    request: EvidenceRequest,
    state_sha256: str,
    refs: tuple[SourceRef, ...],
    history_heads: Mapping[SourceRef, str],
) -> None:
    """Fresh local-only check, usable inside the final publication reservation."""
    with store.read_snapshot():
        refreshed = authorized_scope(store, request.session, request.scope, request.subject_ids)
        if refreshed != archive.scope or digest(encode(dict(store.state()))) != state_sha256:
            raise SourceArchiveError("scope_denied")
        archive._verify_refs_local(refs, expected_heads=history_heads)


@dataclass(frozen=True, repr=False)
class PreparedSourceProjection:
    request: EvidenceRequest
    policy: PreviewPolicy
    state_sha256: str
    views: tuple[PreparedSourceView, ...]
    retained_views: tuple[RetainedSourceView, ...] = ()


def active_preview_memories(store: EvidenceStore, memory_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Retire previously published links that current graph privacy now withholds.

    This is only for prior inventory, never for required new requests. Shared
    source notes can retain their other current memory links. Malformed records
    and missing required source lineage still refuse the subsequent preparation.
    """
    active = []
    with store.read_snapshot():
        for memory_id in memory_ids:
            record = store.get_record(memory_id)
            if record.get("collection") != "claims":
                raise SourceArchiveError("source_unavailable")
            try:
                _payload(record)
            except SourceArchiveError as error:
                if error.code != "scope_denied":
                    raise
            else:
                active.append(memory_id)
    return tuple(active)


def prepare_source_views(
    store: EvidenceStore,
    archive: Archive,
    request: EvidenceRequest | tuple[EvidenceRequest, ...],
    policy: PreviewPolicy,
    *,
    retained_views: tuple[RetainedSourceView, ...] = (),
    prepared_views: tuple[PreparedSourceView, ...] = (),
) -> PreparedSourceProjection:
    """Prepare a bounded batch of current claims at one exact graph state.

    No public tool returns these bodies. Preparation does not authorize publication:
    the caller must serialize a fresh policy/state/denial check with cleanup.
    The first request sets the shared new/changed character/version budget; every
    request carries the same session, scope and budget. Authenticated retained
    inventory has the separate overall view bound, never a lifetime evidence cap.
    Retained bodies are reusable only after fresh authority, exact lineage and
    history checks; prepared new views still consume this request's budget.
    """
    requests = (request,) if isinstance(request, EvidenceRequest) else request
    if not isinstance(requests, tuple) or not 1 <= len(requests) <= MAX_SOURCE_VIEWS:
        raise SourceArchiveError("limit_exceeded")
    first = requests[0]
    if not isinstance(first, EvidenceRequest) or any(
        not isinstance(item, EvidenceRequest)
        or item.revision is not None
        or (item.session, item.scope, item.subject_ids, item.max_chars, item.max_versions)
        != (first.session, first.scope, first.subject_ids, first.max_chars, first.max_versions)
        for item in requests
    ):
        raise SourceArchiveError("scope_denied")
    access = archive.access
    if (
        not policy.enabled
        or policy.scope != archive.scope
        or access is None
        or access.audience != "owner"
        or access.sensitivities != ("ordinary",)
    ):
        raise SourceArchiveError("scope_denied")
    links: dict[SourceRef, list[str]] = {}
    state_hash: str | None = None
    for item in requests:
        selected = _select_evidence(store, archive, item)
        current_hash = digest(encode(selected.state))
        if state_hash is not None and current_hash != state_hash:
            raise SourceArchiveError("scope_denied")
        state_hash = current_hash
        if selected.checked.get("collection") != "claims" or not selected.refs or selected.missing:
            raise SourceArchiveError("source_unavailable")
        for ref in selected.refs:
            memories = links.setdefault(ref, [])
            if item.record_id not in memories:
                memories.append(item.record_id)
        if len(links) > MAX_SOURCE_VIEWS:
            raise SourceArchiveError("limit_exceeded")
    views: list[PreparedSourceView] = []
    reused: list[RetainedSourceView] = []
    retained = {view.ref: view for view in retained_views}
    prepared = {view.excerpt.ref: view for view in prepared_views}
    if len(retained) > MAX_SOURCE_VIEWS or len(prepared) > MAX_SOURCE_VIEWS:
        raise SourceArchiveError("limit_exceeded")
    charged: set[str] = set()
    remaining = first.max_chars
    for ref, memories in links.items():
        prior = retained.get(ref)
        # A required freshly prepared view takes precedence: it has already used
        # the new request's budget and may carry newly observed history/context.
        if prior is not None and ref not in prepared:
            try:
                archive.verify_refs(
                    (ref,), expected_heads={ref: prior.history_head}, operation="owner_preview"
                )
            except SourceArchiveError as error:
                if error.code != "capture_incomplete":
                    raise
                # Changed history or legacy metadata requires fresh bounded work.
            else:
                ordered = tuple(memory for memory in prior.memory_ids if memory in memories)
                ordered += tuple(memory for memory in memories if memory not in ordered)
                reused.append(replace(prior, memory_ids=ordered))
                continue
        charged.add(ref.manifest_sha256)
        if len(charged) > first.max_versions:
            raise SourceArchiveError("limit_exceeded")
        if remaining < 1:
            raise SourceArchiveError("limit_exceeded")
        current = prepared.get(ref)
        excerpt = (
            current.excerpt
            if current is not None
            else archive.read(
                ref, max_chars=min(12_000, remaining), neighbors=True, operation="owner_preview"
            )
        )
        if ref.anchor_id not in {unit.anchor_id for unit in excerpt.units}:
            raise SourceArchiveError("limit_exceeded")
        remaining -= len(encode_units(excerpt.units).decode("utf-8"))
        if remaining < 0:
            raise SourceArchiveError("limit_exceeded")
        views.append(PreparedSourceView(tuple(memories), excerpt, excerpt.title or "Saved source"))
    refs = (*tuple(view.excerpt.ref for view in views), *tuple(view.ref for view in reused))
    heads = {
        **{view.excerpt.ref: view.excerpt.history_head for view in views},
        **{view.ref: view.history_head for view in reused},
    }
    archive.verify_refs(refs, expected_heads=heads, operation="owner_preview")
    assert state_hash is not None
    verify_evidence_state(store, archive, first, state_hash, refs, heads)
    return PreparedSourceProjection(first, policy, state_hash, tuple(views), tuple(reused))


def gather(
    store: EvidenceStore,
    archive: Archive,
    request: EvidenceRequest,
    *,
    retain_selection: Callable[
        [Mapping[str, object], tuple[SourceRef, ...], Mapping[SourceRef, str]], None
    ]
    | None = None,
) -> dict[str, object]:
    """Read bounded evidence, then recheck local state; never infer or correct a fact."""
    selected = _select_evidence(store, archive, request)
    state, checked, summaries = selected.state, selected.checked, selected.summaries
    refs, missing = selected.refs, selected.missing
    sources: list[dict[str, object]] = []
    result: dict[str, object] = {
        "record_id": request.record_id,
        "revision": checked["revision"],
        "snapshot_watermark": state["watermark"],
        "memory": summaries,
        "basis": "linked_source_check",
        "untrusted_source_content": True,
        "sources": sources,
        "complete": True,
        "remaining_sources": 0,
        "remaining_context_links": 0,
        "unavailable_links": missing,
    }
    if not refs or missing:
        result["complete"] = False
    queue = [(ref, "cited") for ref in refs]
    verified: list[SourceRef] = []
    history_heads: dict[SourceRef, str] = {}
    context_links = 0
    while queue and len(verified) < request.max_versions:
        ref, relation = queue.pop(0)
        if ref in verified:
            continue
        available = request.max_chars - len(encode(result).decode("utf-8")) - 700
        if available < 256:
            queue.insert(0, (ref, relation))
            break
        excerpt = archive.read(
            ref,
            max_chars=available,
            max_versions=request.max_versions,
            neighbors=True,
            operation="evidence",
        )
        item: dict[str, object] = {
            "relation": relation,
            **asdict(excerpt),
            "source_uri": excerpt.ref.as_uri(),
        }
        if len(encode({**result, "sources": [*sources, item]}).decode("utf-8")) > request.max_chars:
            queue.insert(0, (ref, relation))
            break
        sources.append(item)
        verified.append(ref)
        # Related metadata is released even when the body budget stops traversal.
        # It needs the same final authorization as every body actually returned.
        history_heads.update(
            (released, excerpt.history_head) for released in (ref, *excerpt.later_versions)
        )
        returned_ids = {unit.source_id for unit in excerpt.units if unit.source_id}
        unresolved = sum(
            len(unit.links) + int(bool(unit.reply_to) and unit.reply_to not in returned_ids)
            for unit in excerpt.units
        )
        context_links += unresolved
        result["remaining_context_links"] = context_links
        if (
            excerpt.coverage != "complete"
            or excerpt.privacy != "original"
            or excerpt.remaining_units
            or excerpt.previous_units
            or excerpt.remaining_versions
            or unresolved
            or any(u.kind in ("ocr_page", "image_page") for u in excerpt.units)
        ):
            result["complete"] = False
        queue.extend(
            (later, "later_capture") for later in excerpt.later_versions if later not in verified
        )
    result["remaining_sources"] = len({ref for ref, _ in queue if ref not in verified})
    if queue:
        result["complete"] = False
    if len(encode(result).decode("utf-8")) > request.max_chars:
        result = {
            "record_id": request.record_id,
            "revision": checked["revision"],
            "snapshot_watermark": state["watermark"],
            "sources": [],
            "complete": False,
            "status": "limit_exceeded",
            "untrusted_source_content": True,
        }
        if len(encode(result).decode("utf-8")) > request.max_chars:
            raise SourceArchiveError("limit_exceeded")
    released_refs = tuple(history_heads)
    archive.verify_refs(released_refs, expected_heads=history_heads, operation="evidence")
    # Re-open the read snapshot so a concurrent permission/record mutation cannot
    # pass merely because the original WAL snapshot remained internally stable.
    verify_evidence_state(
        store, archive, request, digest(encode(state)), released_refs, history_heads
    )
    if retain_selection is not None:
        retain_selection(state, released_refs, history_heads)
    return result
