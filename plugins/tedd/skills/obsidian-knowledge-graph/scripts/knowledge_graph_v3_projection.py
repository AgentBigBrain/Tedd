"""Deterministic rendering for the legacy v3 knowledge-graph projection."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol, TypeAlias, TypedDict, cast

from tedd_knowledge_graph.legacy_values import explicit_record_id
from tedd_knowledge_graph.source_presentation import (
    MAX_SOURCE_VIEWS,
    PRESENTATION_MARKER,
    PreparedSourceView,
    RetainedSourceView,
    render_source_view,
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ProjectionRecord(TypedDict, total=False):
    """Validated record fields consumed by the deterministic projection."""

    id: str
    name: JsonValue
    title: JsonValue
    family: JsonValue
    key: JsonValue
    value: JsonValue
    namespace: JsonValue
    type: JsonValue
    status: JsonValue
    lifecycle: JsonValue
    redirect_to: JsonValue
    source_tier: JsonValue
    kind: JsonValue
    role: JsonValue
    uri: JsonValue
    excerpt: JsonValue
    summary: JsonValue
    rationale: JsonValue
    authority_state: JsonValue
    priority: JsonValue
    due_at: JsonValue
    next_action: JsonValue
    checkpoint_at: JsonValue
    terminal_condition: JsonValue
    resolution: JsonValue
    surface_ref: JsonValue
    source_ref: JsonValue
    superseded_by: JsonValue
    active: bool
    sensitive: bool
    verified: bool
    aliases: list[str]
    entity_refs: list[str]
    owner_refs: list[str]
    observation_refs: list[str]
    evidence_refs: list[str]
    source_entity_id: str
    target_entity_id: str
    last_seen_at: str | None
    observed_at: str
    occurred_at: str
    decided_at: str
    verified_at: str
    created_at: str
    resolved_at: str | None
    last_observed_at: str | None
    valid_from: str | None
    valid_to: str | None
    asserted_at: str | None
    ended_at: str | None


class ProjectionGraph(TypedDict, total=False):
    updated_at: JsonValue
    journal_anchor: dict[str, JsonValue]
    projection_hot_set: dict[str, JsonValue]
    entities: list[ProjectionRecord]
    relations: list[ProjectionRecord]
    observations: list[ProjectionRecord]
    claim_surfaces: list[ProjectionRecord]
    claims: list[ProjectionRecord]
    events: list[ProjectionRecord]
    decisions: list[ProjectionRecord]
    open_loops: list[ProjectionRecord]
    receipts: list[ProjectionRecord]
    sources: list[ProjectionRecord]
    journal: list[ProjectionRecord]


class ProjectionValidationResult(TypedDict):
    valid: bool
    errors: list[str]


_PROJECTION_COLLECTIONS = (
    "entities",
    "relations",
    "observations",
    "claim_surfaces",
    "claims",
    "events",
    "decisions",
    "open_loops",
    "receipts",
    "sources",
    "journal",
)


class StableHash(Protocol):
    def __call__(self, value: object, length: int = 20) -> str: ...


class RedactText(Protocol):
    def __call__(
        self,
        value: str,
        *,
        redact_paths: bool = True,
        max_length: int = 4000,
    ) -> tuple[str, int]: ...


class WikiRenderer(Protocol):
    def __call__(self, relative_path: str, label: str | None = None) -> str: ...


class BulletRenderer(Protocol):
    def __call__(self, values: Iterable[object], empty: str = "None") -> str: ...


class RecordRenderer(Protocol):
    def __call__(
        self,
        record: ProjectionRecord,
        kind: str,
        entity_paths: dict[str, str],
        entities: dict[str, ProjectionRecord],
        mode: str,
        timestamp_key: str,
    ) -> str: ...


@dataclass(frozen=True)
class ProjectionDependencies:
    """Legacy policy and helpers required by deterministic projection rendering."""

    collections: Sequence[str]
    max_projection_path_length: int
    normalize_space: Callable[[object], str]
    stable_hash: StableHash
    redact_text: RedactText
    validate_graph: Callable[[object], ProjectionValidationResult]
    index_by_id: Callable[[list[ProjectionRecord]], dict[str, ProjectionRecord]]
    relation_visible_in_mode: Callable[[ProjectionRecord, dict[str, ProjectionRecord], str], bool]


@dataclass(frozen=True)
class ProjectionTextHooks:
    """Patchable legacy text-rendering surface used by record notes."""

    frontmatter: Callable[[dict[str, object]], str]
    markdown_literal: Callable[[object], str]
    wiki: WikiRenderer
    bullet: BulletRenderer
    projection_value: Callable[[ProjectionRecord, object, str], str]
    projection_refs: Callable[[ProjectionRecord, str, str], list[str]]


@dataclass(frozen=True)
class ProjectionBuildHooks(ProjectionTextHooks):
    """Patchable legacy composition surface used by the projection builder."""

    note_path: Callable[[str, ProjectionRecord, str], str]
    assert_unique_projection_paths: Callable[[Iterable[str]], None]
    render_record_note: RecordRenderer
    base_file: Callable[[str, str, str, list[str]], str]
    loop_base_file: Callable[[str], str]


def yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(
    properties: dict[str, object],
    *,
    render_scalar: Callable[[object], str],
) -> str:
    lines = ["---"]
    for key, value in properties.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {render_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {render_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def safe_segment(
    value: object,
    max_length: int,
    *,
    dependencies: ProjectionDependencies,
) -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", dependencies.normalize_space(value))
    # Obsidian reserves these characters or sequences inside internal-link
    # targets. Remove only the structural forms that make a generated filename
    # ambiguous; single brackets and percent signs remain stable.
    text = text.replace("#", " ").replace("^", " ")
    for delimiter in ("[[", "]]", "%%"):
        text = text.replace(delimiter, " ")
    text = dependencies.normalize_space(text) or "Untitled"
    if len(text) <= max_length:
        return text
    suffix = dependencies.stable_hash(text, 20)
    return f"{text[: max_length - 21].rstrip()}-{suffix}"


def note_path(
    folder: str,
    record: ProjectionRecord,
    label: str,
    *,
    dependencies: ProjectionDependencies,
    render_segment: Callable[[object, int], str],
) -> str:
    record_id = dependencies.normalize_space(record.get("id"))
    suffix = dependencies.stable_hash({"projection_id": record_id or record}, 24)
    return f"{folder}/{render_segment(f'{label} {suffix}', 96)}.md"


def assert_unique_projection_paths(
    paths: Iterable[str],
    *,
    max_path_length: int,
) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        if len(path) > max_path_length:
            raise ValueError(f"Projection path exceeds the safety length limit: {path!r}")
        normalized = unicodedata.normalize("NFC", path).casefold()
        prior = seen.get(normalized)
        if prior is not None:
            raise ValueError(
                "Projection path collision after Unicode/case normalization: "
                f"{prior!r} and {path!r}"
            )
        seen[normalized] = path


def markdown_literal(value: object, *, normalize_space: Callable[[object], str]) -> str:
    text = normalize_space(value).replace("\\", "\\\\")
    return re.sub(r"([`*_[\]<>#!|])", r"\\\1", text)


def wiki(
    relative_path: str,
    label: str | None = None,
    *,
    escape_markdown: Callable[[object], str],
) -> str:
    target = re.sub(r"\.md$", "", relative_path.replace("\\", "/"), flags=re.I)
    return f"[[{target}|{escape_markdown(label)}]]" if label else f"[[{target}]]"


def bullet(
    values: Iterable[object],
    empty: str = "None",
    *,
    normalize_space: Callable[[object], str],
) -> str:
    items = [normalize_space(value) for value in values if normalize_space(value)]
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def projection_value(
    record: ProjectionRecord,
    value: object,
    mode: str,
    *,
    dependencies: ProjectionDependencies,
) -> str:
    if mode == "review_safe" and record.get("sensitive"):
        return "[redacted in review_safe mode]"
    text, _ = dependencies.redact_text(
        dependencies.normalize_space(value),
        redact_paths=mode == "review_safe",
        max_length=1600,
    )
    return text


def projection_refs(record: ProjectionRecord, key: str, mode: str) -> list[str]:
    record_values: Mapping[str, object] = record
    raw_refs = record_values.get(key, [])
    if mode == "review_safe" and raw_refs:
        return ["Withheld in review_safe mode"]
    if not isinstance(raw_refs, list) or not all(isinstance(ref, str) for ref in raw_refs):
        raise TypeError(f"{key} must be a list of strings")
    return list(cast(list[str], raw_refs))


def _narrow_projection_graph(graph: dict[str, object]) -> ProjectionGraph:
    """Confirm the projection's container shapes without copying its records."""

    list_fields = (
        "aliases",
        "entity_refs",
        "owner_refs",
        "observation_refs",
        "evidence_refs",
    )
    text_fields = (
        "id",
        "source_entity_id",
        "target_entity_id",
        "observed_at",
        "occurred_at",
        "decided_at",
        "verified_at",
        "created_at",
    )
    for collection in _PROJECTION_COLLECTIONS:
        raw_records = graph.get(collection)
        if not isinstance(raw_records, list):
            raise TypeError(f"{collection} must be a list")
        for index, raw_record in enumerate(cast(list[object], raw_records)):
            if not isinstance(raw_record, dict) or not all(
                isinstance(key, str) for key in raw_record
            ):
                raise TypeError(f"{collection}[{index}] must be an object with string keys")
            record = cast(dict[str, object], raw_record)
            for field in list_fields:
                value = record.get(field)
                if value is not None and (
                    not isinstance(value, list) or not all(isinstance(item, str) for item in value)
                ):
                    raise TypeError(f"{collection}[{index}].{field} must be a list of strings")
            for field in text_fields:
                value = record.get(field)
                if value is not None and not isinstance(value, str):
                    raise TypeError(f"{collection}[{index}].{field} must be a string")
    # Normal builds reach this cast only after semantic graph validation.  The
    # compatibility-only skip path still receives the structural checks above.
    return cast(ProjectionGraph, graph)


def _collection_length(graph: ProjectionGraph, name: str) -> int:
    graph_values: Mapping[str, object] = graph
    collection = graph_values[name]
    if not isinstance(collection, list):
        raise TypeError(f"{name} must be a list")
    return len(collection)


@dataclass
class _ProjectionContext:
    graph: ProjectionGraph
    mode: str
    root_name: str
    dependencies: ProjectionDependencies
    hooks: ProjectionBuildHooks
    entities: dict[str, ProjectionRecord]
    records: dict[str, ProjectionRecord]
    entity_paths: dict[str, str]
    navigable_entity_paths: dict[str, str]
    loop_paths: dict[str, str]
    record_paths: dict[str, str]
    records_by_entity: dict[str, dict[str, list[ProjectionRecord]]]
    notes: dict[str, str]


SUMMARY_LIMIT = 25
FOLD_VISIBLE_LIMIT = 5
DISPLAY_TEXT_LIMIT = 160
READING_HELP = (
    "This view can omit private records and material outside its display limit. "
    "A link records why two saved notes are connected; it does not prove the remembered "
    "interpretation is correct."
)
RELATION_PHRASES: dict[str, str] = {
    "collaborates_on": "collaborates on",
    "contributes_to": "contributes to",
    "depends_on": "depends on",
    "hosted_by": "is hosted by",
}
SOURCE_TIER_LABELS = {
    "assistant_inference": "Tedd's inference — needs checking",
    "reconciliation_or_projection": "Reconstructed from saved records",
    "validated_structured_candidate": "Saved from reviewed evidence",
    "explicit_user_statement": "Recorded from a user",
}
_BASE_COLUMN_LABELS = {
    "about": "About",
    "checkpoint_at": "Check-in",
    "decided_at": "Decided",
    "display_context": "Context",
    "due_at": "Due",
    "entity_type": "Kind",
    "next_action": "Next step",
    "observed_at": "Observed",
    "occurred_at": "When",
    "owners": "Owner",
    "resolution": "Outcome",
    "resolved_at": "Completed",
    "valid_from": "Since",
    "formula.readable_source": "Source",
    "formula.readable_state": "State",
}
_BASE_TITLE_LABELS = {
    "claim": "Statement",
    "decision": "Decision",
    "entity": "Name",
    "event": "Event",
    "observation": "Recorded wording",
}
_READABLE_NOTE_EXPRESSION = "link(file.path, if(note.display_title, note.display_title, file.name))"
_READABLE_SOURCE_EXPRESSION = (
    'if(note.source_tier == "explicit_user_statement", "Recorded from a user", '
    'if(note.source_tier == "validated_structured_candidate", '
    '"Saved from reviewed evidence", '
    'if(note.source_tier == "reconciliation_or_projection", '
    '"Reconstructed from saved records", '
    'if(note.source_tier == "assistant_inference", '
    '"Tedd\'s inference — needs checking", '
    'if(note.source_tier, note.source_tier, "Unknown source")))))'
)
_READABLE_STATE_EXPRESSION = (
    'if(note.lifecycle == "conflicting", '
    'if(note.authority_state == "candidate", "Proposed conflict — needs checking", '
    '"Conflict — needs checking"), '
    'if(note.lifecycle == "quarantined", '
    'if(note.authority_state == "candidate", "Proposed — needs checking", '
    '"Needs checking"), '
    'if(note.authority_state == "candidate", "Proposed or needs checking", '
    'if(note.lifecycle, note.lifecycle, if(note.status, note.status, "Unknown")))))'
)
_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _eligible_for_navigation(record: ProjectionRecord, _mode: str) -> bool:
    """Return whether a record may create a new human-visible connection."""

    if record.get("lifecycle") in {"redacted", "redirected"}:
        return False
    # Rich navigation has a stricter privacy contract than legacy full-note
    # rendering. New links and artifacts require an explicit ordinary marker in
    # every mode; missing classification fails closed.
    return record.get("sensitive") is False


def _record_timestamp(record: ProjectionRecord) -> str:
    for key in (
        "resolved_at",
        "ended_at",
        "occurred_at",
        "decided_at",
        "verified_at",
        "created_at",
        "observed_at",
        "asserted_at",
        "valid_from",
        "last_observed_at",
    ):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _display_timestamp(value: object, missing: str) -> str:
    """Render a stable English UTC date while preserving exact values elsewhere."""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return missing
    date = f"{_MONTH_NAMES[parsed.month]} {parsed.day}, {parsed.year}"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date
    return f"{date} at {parsed.hour:02d}:{parsed.minute:02d} UTC"


def _currentness(record: ProjectionRecord) -> int:
    lifecycle = record.get("lifecycle")
    status = record.get("status")
    if lifecycle in {"current", "active"} or status in {"open", "accepted", "current"}:
        return 0
    if lifecycle in {"conflicting", "quarantined", "candidate"} or status == "candidate":
        return 1
    return 2


def _record_sort_key(record: ProjectionRecord) -> tuple[int, float, str]:
    value = _record_timestamp(record)
    parsed = _parse_timestamp(value)
    instant = parsed.timestamp() if parsed is not None else 0.0
    return (_currentness(record), -instant, str(record.get("id", "")))


def _recent_sort_key(record: ProjectionRecord) -> tuple[float, str]:
    parsed = _parse_timestamp(_record_timestamp(record))
    instant = parsed.timestamp() if parsed is not None else 0.0
    return (-instant, str(record.get("id", "")))


def _source_tier_label(record: ProjectionRecord, hooks: ProjectionTextHooks, mode: str) -> str:
    raw = hooks.projection_value(record, record.get("source_tier"), mode)
    return SOURCE_TIER_LABELS.get(raw, raw or "Unknown source")


def _projected_literal(
    context: _ProjectionContext,
    record: ProjectionRecord,
    value: object,
) -> str:
    return context.hooks.markdown_literal(
        context.hooks.projection_value(record, value, context.mode)
    )


def _bounded_display_text(
    record: ProjectionRecord,
    value: object,
    hooks: ProjectionTextHooks,
    mode: str,
    *,
    property_literal: bool = False,
) -> str:
    safe = hooks.projection_value(record, value, mode)
    single_line = re.sub(r"\s+", " ", safe).strip()
    if property_literal:
        single_line = re.sub(r"(?<=\[)(?=\[)", " ", single_line)
        single_line = re.sub(r"(?<=\])(?=\])", " ", single_line)
    if len(single_line) <= DISPLAY_TEXT_LIMIT:
        return single_line
    return single_line[: DISPLAY_TEXT_LIMIT - 1].rstrip() + "…"


def _record_label(
    record: ProjectionRecord,
    hooks: ProjectionTextHooks,
    mode: str,
    *,
    property_literal: bool = False,
) -> str:
    for value in (
        record.get("title"),
        record.get("name"),
        record.get("value"),
        record.get("summary"),
    ):
        label = _bounded_display_text(
            record,
            value,
            hooks,
            mode,
            property_literal=property_literal,
        )
        if label:
            return label
    fallback = _bounded_display_text(
        record,
        record.get("kind") or record.get("type"),
        hooks,
        mode,
        property_literal=property_literal,
    )
    return fallback.replace("_", " ").capitalize() if fallback else "Saved record"


def _record_preview(
    record: ProjectionRecord,
    hooks: ProjectionTextHooks,
    mode: str,
    *,
    property_literal: bool = False,
) -> str:
    for value in (record.get("value"), record.get("summary")):
        preview = _bounded_display_text(
            record,
            value,
            hooks,
            mode,
            property_literal=property_literal,
        )
        if preview:
            return preview
    return ""


def _display_properties(
    record: ProjectionRecord,
    hooks: ProjectionTextHooks,
    mode: str,
) -> dict[str, str]:
    """Return bounded presentation-only properties for generated notes."""

    if record.get("sensitive") is not False:
        return {"display_title": "Saved record", "display_summary": ""}
    return {
        "display_title": _record_label(record, hooks, mode, property_literal=True),
        "display_summary": _record_preview(record, hooks, mode, property_literal=True),
    }


def _entity_display_context(context: _ProjectionContext, entity: ProjectionRecord) -> str:
    """Describe a selected namespace without exposing canonical identifiers."""

    if not _eligible_for_navigation(entity, context.mode):
        return "Context not shown"
    namespace = context.hooks.projection_value(
        entity, entity.get("namespace", "global"), context.mode
    )
    target = context.entities.get(namespace)
    if target is not None:
        if namespace not in context.record_paths or not _eligible_for_navigation(
            target, context.mode
        ):
            return "Context not shown"
        return (
            _bounded_display_text(
                target,
                target.get("name"),
                context.hooks,
                context.mode,
                property_literal=True,
            )
            or "Context not shown"
        )
    try:
        unresolved_record_id = explicit_record_id(namespace)
    except ValueError:
        unresolved_record_id = ""
    if unresolved_record_id:
        return "Context not shown"
    return (
        _bounded_display_text(
            entity,
            namespace,
            context.hooks,
            context.mode,
            property_literal=True,
        )
        or "Context not shown"
    )


def _index_records_by_entity(
    graph: ProjectionGraph,
) -> dict[str, dict[str, list[ProjectionRecord]]]:
    result: dict[str, dict[str, list[ProjectionRecord]]] = {}
    for collection in (
        "claims",
        "observations",
        "decisions",
        "open_loops",
        "events",
        "receipts",
    ):
        seen: set[tuple[str, str]] = set()
        for record in graph[collection]:
            refs = list(record.get("entity_refs", []))
            if collection == "open_loops":
                refs.extend(record.get("owner_refs", []))
            for entity_id in refs:
                identity = (entity_id, str(record.get("id", "")))
                if identity in seen:
                    continue
                seen.add(identity)
                result.setdefault(entity_id, {}).setdefault(collection, []).append(record)
    for grouped in result.values():
        for records in grouped.values():
            records.sort(key=_record_sort_key)
    return result


def _prepare_projection_context(
    graph: ProjectionGraph,
    mode: str,
    root_name: str,
    dependencies: ProjectionDependencies,
    hooks: ProjectionBuildHooks,
) -> _ProjectionContext:
    entities = dependencies.index_by_id(graph["entities"])
    records: dict[str, ProjectionRecord] = {}
    graph_values: Mapping[str, object] = graph
    for collection in _PROJECTION_COLLECTIONS:
        collection_value = graph_values[collection]
        if not isinstance(collection_value, list):
            raise TypeError(f"{collection} must be a list")
        for record in cast(list[ProjectionRecord], collection_value):
            record_id = record.get("id")
            if isinstance(record_id, str):
                records[record_id] = record
    entity_paths = {
        entity["id"]: hooks.note_path(
            "10 Entities",
            entity,
            hooks.projection_value(entity, entity.get("name"), mode) or "Entity",
        )
        for entity in graph["entities"]
    }
    loop_paths = {
        loop["id"]: hooks.note_path(
            "13 Open Loops",
            loop,
            hooks.projection_value(loop, loop.get("title"), mode) or "Open Loop",
        )
        for loop in graph["open_loops"]
    }
    record_paths: dict[str, str] = {
        entity_id: path
        for entity_id, path in entity_paths.items()
        if _eligible_for_navigation(entities[entity_id], mode)
    }

    def add_paths(
        collection: str,
        folder: str,
        label: Callable[[ProjectionRecord], str],
    ) -> None:
        records_value: Mapping[str, object] = graph
        records = records_value[collection]
        if not isinstance(records, list):
            raise TypeError(f"{collection} must be a list")
        for record in cast(list[ProjectionRecord], records):
            if not _eligible_for_navigation(record, mode):
                continue
            record_paths[record["id"]] = hooks.note_path(folder, record, label(record))

    add_paths(
        "claims",
        "11 Claims",
        lambda record: (
            hooks.projection_value(record, f"{record.get('family')} {record.get('key')}", mode)
            or "Claim"
        ),
    )
    add_paths(
        "observations",
        "22 Observations",
        lambda record: (
            (
                f"{record.get('observed_at', '')[:10]} "
                + hooks.projection_value(
                    record, f"{record.get('family')} {record.get('key')}", mode
                )
            ).strip()
            or "Observation"
        ),
    )
    for event in graph["events"]:
        if not _eligible_for_navigation(event, mode):
            continue
        folder = (
            "15 Releases"
            if event.get("kind") in {"release", "deployment"}
            else "12 Incidents and Events"
        )
        event_name = (
            f"{event.get('occurred_at', '')[:10]} "
            f"{hooks.projection_value(event, event.get('title'), mode)}"
        ).strip()
        record_paths[event["id"]] = hooks.note_path(folder, event, event_name)
    add_paths(
        "decisions",
        "14 Decisions",
        lambda record: (
            f"{record.get('decided_at', '')[:10]} "
            f"{hooks.projection_value(record, record.get('title'), mode)}"
        ).strip(),
    )
    for loop_id, path in loop_paths.items():
        loop = records[loop_id]
        if _eligible_for_navigation(loop, mode):
            record_paths[loop_id] = path
    add_paths(
        "receipts",
        "21 Receipts",
        lambda record: (
            f"{record.get('verified_at', '')[:10]} "
            f"{hooks.projection_value(record, record.get('title'), mode)}"
        ).strip(),
    )
    add_paths(
        "sources",
        "20 Evidence",
        lambda record: (
            f"{record.get('observed_at', '')[:10]} "
            f"{hooks.projection_value(record, record.get('title'), mode)}"
        ).strip(),
    )
    return _ProjectionContext(
        graph=graph,
        mode=mode,
        root_name=root_name,
        dependencies=dependencies,
        hooks=hooks,
        entities=entities,
        records=records,
        entity_paths=entity_paths,
        navigable_entity_paths={
            entity_id: path for entity_id, path in entity_paths.items() if entity_id in record_paths
        },
        loop_paths=loop_paths,
        record_paths=record_paths,
        records_by_entity=_index_records_by_entity(graph),
        notes={},
    )


def _link_to_record(context: _ProjectionContext, record_id: str) -> str | None:
    path = context.record_paths.get(record_id)
    record = context.records.get(record_id)
    if path is None or record is None or not _eligible_for_navigation(record, context.mode):
        return None
    return context.hooks.wiki(
        f"{context.root_name}/{path}",
        _record_label(record, context.hooks, context.mode),
    )


def _navigable_records(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
) -> list[ProjectionRecord]:
    """Filter before bounding so hidden records cannot displace visible navigation."""

    return [
        record
        for record in records
        if str(record.get("id", "")) in context.record_paths
        and _eligible_for_navigation(record, context.mode)
    ]


def _reference_links(
    context: _ProjectionContext,
    record: ProjectionRecord,
    key: str,
    *,
    scalar: bool = False,
) -> tuple[list[str], bool]:
    raw: object = record.get(key)
    if scalar:
        refs = [raw] if isinstance(raw, str) and raw else []
    else:
        refs = raw if isinstance(raw, list) and all(isinstance(item, str) for item in raw) else []
    if not _eligible_for_navigation(record, context.mode):
        return [], bool(refs)
    links = [link for ref in refs if (link := _link_to_record(context, ref)) is not None]
    return links, bool(refs)


def _evidence_lines(context: _ProjectionContext, record: ProjectionRecord) -> list[str]:
    links, had_refs = _reference_links(context, record, "evidence_refs")
    if links:
        return links
    return ["Evidence is not available in this view" if had_refs else "No evidence linked"]


def _about_links(context: _ProjectionContext, record: ProjectionRecord) -> list[str]:
    if not _eligible_for_navigation(record, context.mode):
        return []
    links: list[str] = []
    for entity_id in record.get("entity_refs", []):
        entity = context.entities.get(entity_id)
        path = context.record_paths.get(entity_id)
        if entity is None or path is None or not _eligible_for_navigation(entity, context.mode):
            continue
        links.append(
            context.hooks.wiki(
                path,
                context.hooks.projection_value(entity, entity.get("name"), context.mode)
                or "Entity",
            )
        )
    return links


def _summary(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
    line: Callable[[ProjectionRecord], str],
    *,
    overflow_location: str = "backlinks and the local graph",
) -> tuple[list[str], str | None]:
    eligible = _navigable_records(context, records)
    eligible.sort(key=_record_sort_key)
    lines = [line(record) for record in eligible[:SUMMARY_LIMIT]]
    overflow = None
    if len(eligible) > SUMMARY_LIMIT:
        overflow = (
            f"Summary shortened: {len(eligible) - SUMMARY_LIMIT} more permitted notes remain "
            f"available through {overflow_location}."
        )
    return lines, overflow


def _folded_bullets(
    context: _ProjectionContext,
    lines: Sequence[str],
    *,
    empty: str,
    remainder_label: str,
) -> str:
    """Fold display rows only; selection and link targets are already fixed."""

    if len(lines) <= FOLD_VISIBLE_LIMIT:
        return context.hooks.bullet(lines, empty)
    visible = context.hooks.bullet(lines[:FOLD_VISIBLE_LIMIT], empty)
    hidden = context.hooks.bullet(lines[FOLD_VISIBLE_LIMIT:])
    quoted_hidden = "\n".join(f"> {line}" for line in hidden.splitlines())
    return f"{visible}\n\n> [!note]- {remainder_label}\n{quoted_hidden}"


def _render_bounded_summary(
    context: _ProjectionContext,
    summary: tuple[list[str], str | None],
    *,
    empty: str,
    remainder_label: str,
) -> str:
    lines, overflow = summary
    rendered = _folded_bullets(
        context,
        lines,
        empty=empty,
        remainder_label=remainder_label,
    )
    return rendered if overflow is None else f"{rendered}\n\n{overflow}"


def _grouped_summary(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
    line: Callable[[ProjectionRecord], str],
    bucket: Callable[[ProjectionRecord], str],
    headings: tuple[tuple[str, str], ...],
) -> str:
    eligible = _navigable_records(context, records)
    eligible.sort(key=_record_sort_key)
    selected = eligible[:SUMMARY_LIMIT]
    parts: list[str] = []
    for identity, heading in headings:
        lines = [line(record) for record in selected if bucket(record) == identity]
        if lines:
            parts.extend(
                [
                    f"### {heading}",
                    _folded_bullets(
                        context,
                        lines,
                        empty="None",
                        remainder_label=f"More {heading.casefold()}",
                    ),
                    "",
                ]
            )
    if len(eligible) > SUMMARY_LIMIT:
        parts.append(
            f"Summary shortened: {len(eligible) - SUMMARY_LIMIT} more permitted notes remain "
            "available through backlinks and the local graph."
        )
    return "\n".join(parts).rstrip() or "- None"


def _claim_bucket(record: ProjectionRecord) -> str:
    lifecycle = record.get("lifecycle")
    if lifecycle == "current":
        return "current"
    if lifecycle in {"conflicting", "quarantined", "candidate"}:
        return "checking"
    return "historical"


def _decision_bucket(record: ProjectionRecord) -> str:
    status = record.get("status")
    if record.get("authority_state") == "candidate" or status in {"candidate", "proposed"}:
        return "checking"
    if status == "accepted":
        return "accepted"
    if status in {"historical", "rejected", "superseded", "withdrawn"}:
        return "historical"
    return "checking"


def _commitment_bucket(record: ProjectionRecord) -> str:
    status = record.get("status")
    if record.get("authority_state") == "candidate" or status == "candidate":
        return "checking"
    if status == "open":
        return "open"
    if status == "resolved":
        return "resolved"
    return "checking"


def _dashboard_links(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
    *,
    limit: int = 10,
) -> list[str]:
    eligible = _navigable_records(context, records)
    eligible.sort(key=_recent_sort_key)
    return [
        link
        for record in eligible[:limit]
        if (link := _link_to_record(context, str(record["id"]))) is not None
    ]


def _dashboard_attention(
    context: _ProjectionContext,
    active: Iterable[ProjectionRecord],
    checking: Iterable[ProjectionRecord],
) -> str:
    hooks = context.hooks
    return "\n".join(
        [
            "### Active obligations",
            hooks.bullet(
                _dashboard_links(context, active),
                "No active obligations are shown.",
            ),
            "",
            "### Proposals and conflicts",
            hooks.bullet(
                _dashboard_links(context, checking),
                "No proposals or visible conflicts are shown.",
            ),
        ]
    )


def _render_dashboard(context: _ProjectionContext) -> None:
    graph = context.graph
    hooks = context.hooks
    counts = {
        name: _collection_length(graph, name)
        for name in context.dependencies.collections
        if name != "journal"
    }
    current_claims = [
        claim
        for claim in graph["claims"]
        if claim.get("active") and claim.get("lifecycle") == "current"
    ]
    conflict_claims = [
        claim
        for claim in graph["claims"]
        if claim.get("lifecycle") in {"conflicting", "quarantined"}
    ]
    active_loops = sorted(
        [loop for loop in graph["open_loops"] if loop.get("status") == "open"],
        key=_record_sort_key,
    )
    candidate_loops = sorted(
        [loop for loop in graph["open_loops"] if loop.get("status") == "candidate"],
        key=_record_sort_key,
    )
    resolved_loops = sorted(
        [loop for loop in graph["open_loops"] if loop.get("status") == "resolved"],
        key=_record_sort_key,
    )
    people_and_projects = sorted(
        [
            entity
            for entity in graph["entities"]
            if entity.get("type") in {"person", "project"}
            and entity.get("id") in context.record_paths
            and _eligible_for_navigation(entity, context.mode)
        ],
        key=lambda entity: (
            hooks.projection_value(entity, entity.get("name"), context.mode).casefold(),
            str(entity.get("id", "")),
        ),
    )
    checking_attention = [
        *candidate_loops,
        *conflict_claims,
        *[decision for decision in graph["decisions"] if _decision_bucket(decision) == "checking"],
    ]
    anchor_value: object = graph.get("journal_anchor", {})
    anchor = anchor_value if isinstance(anchor_value, dict) else {}
    hot_value: object = graph.get("projection_hot_set", {})
    hot_set = hot_value if isinstance(hot_value, dict) else {}
    revision = anchor.get("watermark", "unknown")
    complete = hot_set.get("complete")
    selection = (
        "complete" if complete is True else "bounded" if complete is False else "not reported"
    )
    summary_counts = [
        f"{('Loop records' if key == 'open_loops' else key.replace('_', ' ').title())}: {value}"
        for key, value in counts.items()
    ]
    context.notes["00 Dashboard.md"] = "\n".join(
        [
            hooks.frontmatter(
                {
                    "kg_type": "dashboard",
                    "updated_at": graph.get("updated_at"),
                    "mode": context.mode,
                    "snapshot_revision": revision,
                    "selection": selection,
                }
            ),
            f"# {hooks.markdown_literal(context.root_name)}",
            "",
            "## People and projects",
            hooks.bullet(
                [
                    hooks.wiki("30 Bases/entities.base", "Open the people and projects table"),
                    *[
                        link
                        for entity in people_and_projects[:10]
                        if (link := _link_to_record(context, entity["id"])) is not None
                    ],
                ]
            ),
            "",
            "## Needs attention",
            _dashboard_attention(context, active_loops, checking_attention),
            "",
            "## Recent decisions",
            hooks.bullet(
                _dashboard_links(context, graph["decisions"]),
                "No decisions are shown.",
            ),
            "",
            "## Recently recorded",
            hooks.bullet(
                _dashboard_links(
                    context,
                    [*graph["observations"], *graph["events"], *graph["receipts"]],
                ),
                "No recent observations, events, or receipts are shown.",
            ),
            "",
            "## Snapshot details",
            "> [!info]- Technical snapshot details\n"
            + "\n".join(
                f"> {line}"
                for line in hooks.bullet(
                    [
                        f"Saved database revision: {revision}",
                        f"Snapshot date: {hooks.markdown_literal(graph.get('updated_at'))}",
                        f"Privacy mode: {hooks.markdown_literal(context.mode)}",
                        f"Selection: {selection}",
                        *summary_counts,
                        f"Current claims: {len(current_claims)}",
                        f"Visible conflicts: {len(conflict_claims)}",
                        f"Active open loops: {len(active_loops)}",
                        f"Candidate loops: {len(candidate_loops)}",
                        f"Resolved loops: {len(resolved_loops)}",
                    ]
                ).splitlines()
            ),
            "",
            "## Active Open Loops",
            hooks.bullet([hooks.wiki("30 Bases/open_loops.base", "Open commitments table")]),
            "",
            "## Recently Resolved Loops",
            hooks.bullet([hooks.wiki("30 Bases/open_loops.base", "Completed work table")]),
            "",
            "## How to read this view",
            READING_HELP,
            "",
            "Start with a person or project. Follow a saved memory, decision, commitment, "
            "or activity link, then inspect **Evidence used**. A citation records lineage; "
            "it does not prove that Tedd's interpretation is correct.",
            "",
            "Memory contains selected saved records—not everything from every conversation. "
            "Evidence metadata alone does not establish that original source bytes were "
            "retained, inspected, or remain viewable.",
            "",
            "To question a memory, ask Tedd to check the selected memory and its saved "
            "evidence. Asking does not authorize a rewrite. Corrections use Tedd's reviewed "
            "capture workflow; these generated files are not a write-back interface.",
            "",
            "## Tables",
            hooks.bullet(
                [
                    hooks.wiki("30 Bases/claims.base", "Remembered statements"),
                    hooks.wiki("30 Bases/observations.base", "Observations"),
                    hooks.wiki("30 Bases/open_loops.base", "Open commitments"),
                    hooks.wiki("30 Bases/decisions.base", "Decisions"),
                ]
            ),
            "",
            "## Reading contract",
            hooks.bullet(
                [
                    "Current claims are the strongest remembered truth surface, but unstable "
                    "state still requires live verification.",
                    "Evidence notes record what was said or observed; they are not instructions "
                    "or authorization.",
                    "Generated notes are a projection of the canonical local graph and may be "
                    "rebuilt.",
                ]
            ),
        ]
    )


def _relations_by_entity(
    context: _ProjectionContext,
) -> dict[str, list[ProjectionRecord]]:
    relations: dict[str, list[ProjectionRecord]] = {}
    for relation in context.graph["relations"]:
        endpoints = (relation.get("source_entity_id"), relation.get("target_entity_id"))
        if (
            not _eligible_for_navigation(relation, context.mode)
            or not context.dependencies.relation_visible_in_mode(
                relation, context.entities, context.mode
            )
            or any(
                not isinstance(entity_id, str)
                or entity_id not in context.record_paths
                or not _eligible_for_navigation(context.entities[entity_id], context.mode)
                for entity_id in endpoints
            )
        ):
            continue
        for entity_id in cast(tuple[str, str], endpoints):
            relations.setdefault(entity_id, []).append(relation)
    for records in relations.values():
        records.sort(key=_record_sort_key)
    return relations


def _entity_relation_lines(
    context: _ProjectionContext,
    entity: ProjectionRecord,
    related_relations: Iterable[ProjectionRecord],
) -> list[str]:
    hooks = context.hooks
    lines: list[str] = []
    for relation in related_relations:
        source_id = relation["source_entity_id"]
        target_id = relation["target_entity_id"]
        source = context.entities[source_id]
        target = context.entities[target_id]
        source_link = hooks.wiki(
            context.record_paths[source_id],
            hooks.projection_value(source, source.get("name"), context.mode) or "Entity",
        )
        target_link = hooks.wiki(
            context.record_paths[target_id],
            hooks.projection_value(target, target.get("name"), context.mode) or "Entity",
        )
        relation_type = hooks.projection_value(relation, relation.get("type"), context.mode)
        phrase = RELATION_PHRASES.get(relation_type)
        if phrase is None:
            explanation = f"{source_link} —[{hooks.markdown_literal(relation_type)}]→ {target_link}"
        else:
            explanation = f"{source_link} {phrase} {target_link}"
        evidence = "; ".join(_evidence_lines(context, relation))
        lines.append(
            f"{explanation} — status: "
            f"{hooks.markdown_literal(hooks.projection_value(relation, relation.get('status'), context.mode))}; "
            f"source: {hooks.markdown_literal(_source_tier_label(relation, hooks, context.mode))}; "
            f"evidence used: {evidence}"
        )
    return lines


def _entity_claim_section(
    context: _ProjectionContext,
    claims: Iterable[ProjectionRecord],
) -> str:
    summary = _grouped_summary(
        context,
        claims,
        lambda claim: (
            f"{_link_to_record(context, claim['id'])} — "
            f"{context.hooks.markdown_literal(context.hooks.projection_value(claim, claim.get('value'), context.mode))}; "
            f"status: {context.hooks.markdown_literal(claim.get('lifecycle'))}; "
            f"source: {context.hooks.markdown_literal(_source_tier_label(claim, context.hooks, context.mode))}"
        ),
        _claim_bucket,
        (
            ("current", "Current"),
            ("checking", "Needs checking"),
            ("historical", "Historical or replaced"),
        ),
    )
    if summary == "- None":
        return (
            "No current claims are shown here. Recorded observations and decisions "
            "are listed separately."
        )
    return summary


def _entity_observation_section(
    context: _ProjectionContext,
    observations: Iterable[ProjectionRecord],
) -> str:
    lines, overflow = _summary(
        context,
        observations,
        lambda observation: (
            f"{_link_to_record(context, observation['id'])} — "
            f"source: {context.hooks.markdown_literal(_source_tier_label(observation, context.hooks, context.mode))}; "
            "observed: "
            + context.hooks.markdown_literal(
                _display_timestamp(observation.get("observed_at"), "Date not recorded")
            )
        ),
        overflow_location="the Observations table, backlinks, and the local graph",
    )
    return _render_bounded_summary(
        context,
        (lines, overflow),
        empty="No recorded observations are shown here.",
        remainder_label="More recorded observations",
    )


def _entity_record_section(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
    bucket: Callable[[ProjectionRecord], str],
    headings: tuple[tuple[str, str], ...],
) -> str:
    return _grouped_summary(
        context,
        records,
        lambda record: (
            f"{_link_to_record(context, record['id'])} — "
            f"status: {_projected_literal(context, record, record.get('status') or record.get('lifecycle') or 'unknown')}; "
            "recorded: "
            + context.hooks.markdown_literal(
                _display_timestamp(_record_timestamp(record), "Date not recorded")
            )
        ),
        bucket,
        headings,
    )


def _commitment_line(context: _ProjectionContext, record: ProjectionRecord) -> str:
    owner_links, had_owners = _reference_links(context, record, "owner_refs")
    if owner_links:
        owner = ", ".join(owner_links)
    elif had_owners:
        owner = "not available in this view"
    else:
        owner = "unknown"
    status = _commitment_bucket(record)
    prefix = (
        f"{_link_to_record(context, record['id'])} — "
        f"status: {_projected_literal(context, record, record.get('status') or 'unknown')}; "
        f"recorded owner: {owner}; "
    )
    if status == "resolved":
        outcome = context.hooks.projection_value(record, record.get("resolution"), context.mode)
        completed = _display_timestamp(record.get("resolved_at"), "Completion date not recorded")
        return (
            prefix
            + f"outcome: {context.hooks.markdown_literal(outcome) or 'Outcome not recorded'}; "
            + f"completed: {context.hooks.markdown_literal(completed)}"
        )
    action = context.hooks.projection_value(record, record.get("next_action"), context.mode)
    label = "next action" if status == "open" else "proposed action"
    recorded = _display_timestamp(_record_timestamp(record), "Date not recorded")
    return (
        prefix
        + f"{label}: {context.hooks.markdown_literal(action) or 'not recorded'}; "
        + f"recorded: {context.hooks.markdown_literal(recorded)}"
    )


def _commitment_section(
    context: _ProjectionContext,
    records: Iterable[ProjectionRecord],
) -> str:
    return _grouped_summary(
        context,
        records,
        lambda record: _commitment_line(context, record),
        _commitment_bucket,
        (
            ("open", "Open"),
            ("checking", "Proposed or needs checking"),
            ("resolved", "Resolved"),
        ),
    )


def _legacy_entity_claim_lines(
    context: _ProjectionContext,
    claims: Iterable[ProjectionRecord],
) -> list[str]:
    hooks = context.hooks
    lines: list[str] = []
    for claim in claims:
        label = hooks.projection_value(
            claim, f"{claim.get('family')}.{claim.get('key')}", context.mode
        )
        lines.append(
            f"{hooks.markdown_literal(label)}: "
            f"{hooks.markdown_literal(hooks.projection_value(claim, claim.get('value'), context.mode))} "
            f"({claim.get('lifecycle')})"
        )
    return lines


def _render_entity_notes(context: _ProjectionContext) -> None:
    hooks = context.hooks
    relations_by_entity = _relations_by_entity(context)
    for entity in context.graph["entities"]:
        relation_lines = _entity_relation_lines(
            context, entity, relations_by_entity.get(entity["id"], [])
        )
        grouped = context.records_by_entity.get(entity["id"], {})
        claim_section = _entity_claim_section(context, grouped.get("claims", []))
        projected_aliases = (
            []
            if context.mode == "review_safe" and entity.get("sensitive")
            else [
                hooks.projection_value(entity, alias, context.mode)
                for alias in entity.get("aliases", [])
            ]
        )
        metadata = hooks.frontmatter(
            {
                **_display_properties(entity, hooks, context.mode),
                "kg_id": entity["id"],
                "kg_type": "entity",
                "namespace": hooks.projection_value(
                    entity, entity.get("namespace", "global"), context.mode
                ),
                "display_context": _entity_display_context(context, entity),
                "entity_type": hooks.projection_value(entity, entity.get("type"), context.mode),
                "status": hooks.projection_value(entity, entity.get("status"), context.mode),
                "lifecycle": hooks.projection_value(
                    entity, entity.get("lifecycle", "active"), context.mode
                ),
                "redirect_to": hooks.projection_value(
                    entity, entity.get("redirect_to"), context.mode
                ),
                "source_tier": hooks.projection_value(
                    entity, entity.get("source_tier"), context.mode
                ),
                "updated_at": entity.get("last_seen_at"),
                "aliases": projected_aliases,
            }
        )
        title = f"# {hooks.markdown_literal(hooks.projection_value(entity, entity.get('name'), context.mode))}"
        if not _eligible_for_navigation(entity, context.mode):
            context.notes[context.entity_paths[entity["id"]]] = "\n".join(
                [
                    metadata,
                    title,
                    "",
                    "## Claims",
                    hooks.bullet(_legacy_entity_claim_lines(context, grouped.get("claims", []))),
                    "",
                    "## Relations",
                    hooks.bullet(relation_lines),
                    "",
                    "## Evidence Refs",
                    hooks.bullet(hooks.projection_refs(entity, "evidence_refs", context.mode)),
                ]
            )
            continue
        context.notes[context.entity_paths[entity["id"]]] = "\n".join(
            [
                metadata,
                title,
                "",
                "## What Tedd remembers",
                claim_section,
                "",
                "## Relationships",
                _folded_bullets(
                    context,
                    relation_lines,
                    empty="None",
                    remainder_label="More relationships",
                ),
                "",
                "## Decisions",
                _entity_record_section(
                    context,
                    grouped.get("decisions", []),
                    _decision_bucket,
                    (
                        ("accepted", "Accepted"),
                        ("checking", "Proposed or needs checking"),
                        ("historical", "Historical"),
                    ),
                ),
                "",
                "## Open commitments",
                _commitment_section(context, grouped.get("open_loops", [])),
                "",
                "## Recorded observations",
                _entity_observation_section(context, grouped.get("observations", [])),
                "",
                "## Recent activity",
                _render_bounded_summary(
                    context,
                    _summary(
                        context,
                        [*grouped.get("events", []), *grouped.get("receipts", [])],
                        lambda record: (
                            f"{_link_to_record(context, record['id'])} — "
                            f"status: {_projected_literal(context, record, record.get('status') or record.get('lifecycle') or 'unknown')}; "
                            "recorded: "
                            + hooks.markdown_literal(
                                _display_timestamp(_record_timestamp(record), "Date not recorded")
                            )
                        ),
                    ),
                    empty="None",
                    remainder_label="More recent activity",
                ),
                "",
                "## Evidence used",
                hooks.bullet(_evidence_lines(context, entity)),
            ]
        )


def _claim_entity_lines(
    context: _ProjectionContext,
    claim: ProjectionRecord,
) -> list[str]:
    if not _eligible_for_navigation(claim, context.mode):
        return []
    return [
        context.hooks.wiki(
            context.entity_paths[ref],
            context.hooks.projection_value(
                context.entities[ref], context.entities[ref].get("name"), context.mode
            )
            or "Entity",
        )
        for ref in claim.get("entity_refs", [])
        if ref in context.record_paths and ref in context.entity_paths
    ]


def _claim_path(context: _ProjectionContext, claim: ProjectionRecord) -> str:
    path = context.record_paths.get(claim["id"])
    if path is not None:
        return path
    return context.hooks.note_path(
        "11 Claims",
        claim,
        context.hooks.projection_value(
            claim, f"{claim.get('family')} {claim.get('key')}", context.mode
        )
        or "Claim",
    )


def _render_claim_notes(context: _ProjectionContext) -> None:
    hooks = context.hooks
    for claim in context.graph["claims"]:
        path = _claim_path(context, claim)
        family_value = hooks.projection_value(claim, claim.get("family"), context.mode)
        key_value = hooks.projection_value(claim, claim.get("key"), context.mode)
        if not _eligible_for_navigation(claim, context.mode):
            context.notes[path] = "\n".join(
                [
                    hooks.frontmatter(
                        {
                            **_display_properties(claim, hooks, context.mode),
                            "kg_id": claim["id"],
                            "kg_type": "claim",
                            "surface_id": claim.get("surface_ref"),
                            "family": family_value,
                            "claim_key": key_value,
                            "lifecycle": hooks.projection_value(
                                claim, claim.get("lifecycle"), context.mode
                            ),
                            "active": claim.get("active"),
                            "valid_from": claim.get("valid_from"),
                            "valid_to": claim.get("valid_to"),
                            "source_tier": hooks.projection_value(
                                claim, claim.get("source_tier"), context.mode
                            ),
                            "updated_at": claim.get("ended_at")
                            or claim.get("asserted_at")
                            or claim.get("valid_from"),
                        }
                    ),
                    f"# {hooks.markdown_literal(_record_label(claim, hooks, context.mode))}",
                    "",
                    "## Value",
                    "- "
                    + hooks.markdown_literal(
                        hooks.projection_value(claim, claim.get("value"), context.mode)
                    ),
                    "",
                    "## Entities",
                    hooks.bullet(_claim_entity_lines(context, claim)),
                    "",
                    "## Lineage",
                    hooks.bullet(
                        [
                            *(
                                f"Observation: {ref}"
                                for ref in hooks.projection_refs(
                                    claim, "observation_refs", context.mode
                                )
                            ),
                            *(
                                f"Evidence: {ref}"
                                for ref in hooks.projection_refs(
                                    claim, "evidence_refs", context.mode
                                )
                            ),
                            "Superseded by: "
                            + (
                                str(claim.get("superseded_by"))
                                if context.mode == "operator_full"
                                else "withheld"
                                if claim.get("superseded_by")
                                else "none"
                            ),
                        ]
                    ),
                ]
            )
            continue
        observation_links, had_observations = _reference_links(context, claim, "observation_refs")
        successor = claim.get("superseded_by")
        successor_link = (
            _link_to_record(context, successor)
            if isinstance(successor, str) and successor
            else None
        )
        lineage = [f"Based on observation: {link}" for link in observation_links]
        if had_observations and not observation_links:
            lineage.append("Observations are not available in this view")
        lineage.extend(f"Evidence used: {link}" for link in _evidence_lines(context, claim))
        if successor_link is not None:
            lineage.append(f"Replaced by: {successor_link}")
        elif successor:
            lineage.append("Replacement is not available in this view")
        else:
            lineage.append("Replaced by: none")
        context.notes[path] = "\n".join(
            [
                hooks.frontmatter(
                    {
                        **_display_properties(claim, hooks, context.mode),
                        "kg_id": claim["id"],
                        "kg_type": "claim",
                        "surface_id": claim.get("surface_ref"),
                        "family": family_value,
                        "claim_key": key_value,
                        "lifecycle": hooks.projection_value(
                            claim, claim.get("lifecycle"), context.mode
                        ),
                        "active": claim.get("active"),
                        "valid_from": claim.get("valid_from"),
                        "valid_to": claim.get("valid_to"),
                        "source_tier": hooks.projection_value(
                            claim, claim.get("source_tier"), context.mode
                        ),
                        "about": _claim_entity_lines(context, claim),
                        "updated_at": claim.get("ended_at")
                        or claim.get("asserted_at")
                        or claim.get("valid_from"),
                    }
                ),
                f"# {hooks.markdown_literal(_record_label(claim, hooks, context.mode))}",
                "",
                "## Value",
                f"- {hooks.markdown_literal(hooks.projection_value(claim, claim.get('value'), context.mode))}",
                "",
                "## About",
                hooks.bullet(_claim_entity_lines(context, claim)),
                "",
                "## Lineage",
                hooks.bullet(lineage),
            ]
        )


def _connected_record_note(
    context: _ProjectionContext,
    record: ProjectionRecord,
    kind: str,
    timestamp_key: str,
) -> str:
    hooks = context.hooks
    if not _eligible_for_navigation(record, context.mode):
        return hooks.render_record_note(
            record,
            kind,
            context.entity_paths,
            context.entities,
            context.mode,
            timestamp_key,
        )
    content = hooks.render_record_note(
        record,
        kind,
        context.navigable_entity_paths,
        context.entities,
        context.mode,
        timestamp_key,
    )
    old = "## Evidence Refs\n" + hooks.bullet(
        hooks.projection_refs(record, "evidence_refs", context.mode)
    )
    replacement = "## Evidence used\n" + hooks.bullet(_evidence_lines(context, record))
    if old not in content:
        raise ValueError("Record renderer did not provide its evidence section")
    return content.replace(old, replacement)


def _render_observation_notes(context: _ProjectionContext) -> None:
    hooks = context.hooks
    for observation in sorted(context.graph["observations"], key=_record_sort_key):
        if not _eligible_for_navigation(observation, context.mode):
            continue
        path = context.record_paths[observation["id"]]
        evidence, had_evidence = _reference_links(context, observation, "source_ref", scalar=True)
        evidence_lines = evidence or [
            "Evidence is not available in this view" if had_evidence else "No evidence linked"
        ]
        about = _about_links(context, observation)
        family = hooks.projection_value(observation, observation.get("family"), context.mode)
        key = hooks.projection_value(observation, observation.get("key"), context.mode)
        context.notes[path] = "\n".join(
            [
                hooks.frontmatter(
                    {
                        **_display_properties(observation, hooks, context.mode),
                        "kg_id": observation["id"],
                        "kg_type": "observation",
                        "family": family,
                        "observation_key": key,
                        "observed_at": observation.get("observed_at"),
                        "source_tier": hooks.projection_value(
                            observation, observation.get("source_tier"), context.mode
                        ),
                        "about": about,
                    }
                ),
                f"# {hooks.markdown_literal(_record_label(observation, hooks, context.mode))}",
                "",
                "## Recorded observation",
                hooks.markdown_literal(
                    hooks.projection_value(observation, observation.get("value"), context.mode)
                )
                or "No value stored.",
                "",
                "## Details",
                hooks.bullet(
                    [
                        "Observed: "
                        + hooks.markdown_literal(
                            _display_timestamp(observation.get("observed_at"), "Date not recorded")
                        ),
                        "Source: "
                        + hooks.markdown_literal(
                            _source_tier_label(observation, hooks, context.mode)
                        ),
                        "An observation is not automatically an accepted current memory.",
                    ]
                ),
                "",
                "## About",
                hooks.bullet(about),
                "",
                "## Evidence used",
                hooks.bullet(evidence_lines),
            ]
        )


def _render_timeline_notes(context: _ProjectionContext) -> None:
    hooks = context.hooks
    graph = context.graph
    for event in graph["events"]:
        folder = (
            "15 Releases"
            if event.get("kind") in {"release", "deployment"}
            else "12 Incidents and Events"
        )
        event_name = (
            f"{event.get('occurred_at', '')[:10]} "
            f"{hooks.projection_value(event, event.get('title'), context.mode)}"
        )
        path = hooks.note_path(folder, event, event_name)
        path = context.record_paths.get(event["id"], path)
        context.notes[path] = _connected_record_note(context, event, "event", "occurred_at")
    for decision in graph["decisions"]:
        decision_name = (
            f"{decision.get('decided_at', '')[:10]} "
            f"{hooks.projection_value(decision, decision.get('title'), context.mode)}"
        )
        path = hooks.note_path("14 Decisions", decision, decision_name)
        path = context.record_paths.get(decision["id"], path)
        context.notes[path] = _connected_record_note(context, decision, "decision", "decided_at")
    for loop in graph["open_loops"]:
        path = context.loop_paths[loop["id"]]
        context.notes[path] = _connected_record_note(context, loop, "open_loop", "created_at")
    for receipt in graph["receipts"]:
        receipt_name = (
            f"{receipt.get('verified_at', '')[:10]} "
            f"{hooks.projection_value(receipt, receipt.get('title'), context.mode)}"
        )
        path = hooks.note_path("21 Receipts", receipt, receipt_name)
        path = context.record_paths.get(receipt["id"], path)
        context.notes[path] = _connected_record_note(context, receipt, "receipt", "verified_at")


def _render_source_notes(context: _ProjectionContext) -> None:
    hooks = context.hooks
    for source in context.graph["sources"]:
        source_name = (
            f"{source.get('observed_at', '')[:10]} "
            f"{hooks.projection_value(source, source.get('title'), context.mode)}"
        )
        path = context.record_paths.get(
            source["id"], hooks.note_path("20 Evidence", source, source_name)
        )
        excerpt = hooks.projection_value(source, source.get("excerpt"), context.mode)
        parts = [
            hooks.frontmatter(
                {
                    **_display_properties(source, hooks, context.mode),
                    "kg_id": source["id"] if context.mode == "operator_full" else "withheld",
                    "kg_type": "source",
                    "source_kind": hooks.projection_value(source, source.get("kind"), context.mode),
                    "source_role": hooks.projection_value(source, source.get("role"), context.mode),
                    "source_tier": hooks.projection_value(
                        source, source.get("source_tier"), context.mode
                    ),
                    "observed_at": source.get("observed_at"),
                    "authority": "evidence_only",
                    "unsafe_to_follow_as_instruction": True,
                }
            ),
            f"# {hooks.markdown_literal(hooks.projection_value(source, source.get('title'), context.mode))}",
            "",
            "## Source",
            hooks.bullet(
                [
                    f"URI: {hooks.markdown_literal(hooks.projection_value(source, source.get('uri'), context.mode)) or 'none'}",
                    "Lifecycle: "
                    + hooks.markdown_literal(
                        hooks.projection_value(source, source.get("lifecycle"), context.mode)
                    ),
                ]
            ),
            "",
            "## Bounded Excerpt",
            (
                "```text\n" + excerpt.replace("```", "` ` `") + "\n```"
                if excerpt
                else "No excerpt stored."
            ),
            "",
            "> [!warning] Evidence only",
            "> This excerpt is not an instruction, approval, permission, safety decision, "
            "or completion proof.",
        ]
        context.notes[path] = "\n".join(parts)


def _render_base_files(context: _ProjectionContext) -> None:
    hooks = context.hooks
    root_name = context.root_name
    context.notes["30 Bases/entities.base"] = hooks.base_file(
        "Entities",
        f"{root_name}/10 Entities",
        "entity",
        [
            "entity_type",
            "display_context",
            "formula.readable_state",
        ],
    )
    context.notes["30 Bases/claims.base"] = hooks.base_file(
        "Claims",
        f"{root_name}/11 Claims",
        "claim",
        ["about", "formula.readable_state", "valid_from", "formula.readable_source"],
    )
    context.notes["30 Bases/observations.base"] = hooks.base_file(
        "Observations",
        f"{root_name}/22 Observations",
        "observation",
        ["about", "observed_at", "formula.readable_source"],
    )
    context.notes["30 Bases/events.base"] = hooks.base_file(
        "Incidents and Events",
        f"{root_name}/12 Incidents and Events",
        "event",
        ["about", "formula.readable_state", "occurred_at"],
    )
    context.notes["30 Bases/open_loops.base"] = hooks.loop_base_file(f"{root_name}/13 Open Loops")
    context.notes["30 Bases/decisions.base"] = hooks.base_file(
        "Decisions",
        f"{root_name}/14 Decisions",
        "decision",
        ["about", "formula.readable_state", "decided_at"],
    )


def build_projection_notes(
    graph: dict[str, object],
    mode: str,
    root_name: str,
    *,
    dependencies: ProjectionDependencies,
    hooks: ProjectionBuildHooks,
    skip_validation: bool = False,
    source_views: tuple[PreparedSourceView | RetainedSourceView, ...] = (),
) -> dict[str, str]:
    if not skip_validation:
        validation = dependencies.validate_graph(graph)
        if not validation["valid"]:
            raise ValueError(
                "Refusing to render an invalid graph: " + "; ".join(validation["errors"])
            )
    projection_graph = _narrow_projection_graph(graph)

    def reading_frontmatter(properties: dict[str, object]) -> str:
        return hooks.frontmatter(
            {
                "cssclasses": ["tedd-reading"],
                **{key: value for key, value in properties.items() if key != "cssclasses"},
            }
        )

    render_hooks = replace(hooks, frontmatter=reading_frontmatter)
    context = _prepare_projection_context(
        projection_graph, mode, root_name, dependencies, render_hooks
    )
    _render_dashboard(context)
    _render_entity_notes(context)
    _render_observation_notes(context)
    _render_claim_notes(context)
    _render_timeline_notes(context)
    _render_source_notes(context)
    _render_base_files(context)
    _render_source_views(context, source_views)
    class_frontmatter = context.hooks.frontmatter({})
    class_lines = class_frontmatter.splitlines()[1:-1]
    for path, note in tuple(context.notes.items()):
        if not path.endswith(".md"):
            continue
        if not note.startswith("---\n"):
            context.notes[path] = class_frontmatter + note
            continue
        frontmatter_end = note.find("\n---\n", 4)
        if frontmatter_end < 0:
            raise ValueError("Generated Markdown note has incomplete frontmatter")
        existing_frontmatter = note[4:frontmatter_end]
        if re.search(r"(?m)^cssclasses:\s*$", existing_frontmatter) is None:
            context.notes[path] = (
                "---\n" + "\n".join(class_lines) + "\n" + note.removeprefix("---\n")
            )
    context.hooks.assert_unique_projection_paths(context.notes)
    return context.notes


def _render_source_views(
    context: _ProjectionContext, views: tuple[PreparedSourceView | RetainedSourceView, ...]
) -> None:
    """Apply trusted prepared descriptions; rendering cannot grant source access."""
    if type(views) is not tuple or len(views) > MAX_SOURCE_VIEWS:
        raise ValueError("Source presentation exceeds the view limit")
    claims = {claim["id"]: claim for claim in context.graph["claims"]}
    links: dict[str, list[str]] = {}
    for view in views:
        if not isinstance(view, (PreparedSourceView, RetainedSourceView)):
            raise ValueError("Source presentation requires a validated description")
        path, note = render_source_view(
            view, escape=context.hooks.markdown_literal, root_name=context.root_name
        )
        if path in context.notes:
            raise ValueError("Duplicate source presentation path")
        for memory_id in view.memory_ids:
            claim = claims.get(memory_id)
            if (
                claim is None
                or claim.get("sensitive") is not False
                or claim.get("lifecycle") in ("redacted", "redirected")
            ):
                raise ValueError("Source presentation target is unavailable")
            link = context.hooks.wiki(f"{context.root_name}/{path}", view.title)
            links.setdefault(_claim_path(context, claim), []).append(link)
        context.notes[path] = note
    for path, references in links.items():
        context.notes[path] += (
            f"\n\n{PRESENTATION_MARKER}\n\n## Saved sources\n\n"
            + context.hooks.bullet(references)
            + "\n"
        )


def _record_entity_lines(
    record: ProjectionRecord,
    entity_paths: dict[str, str],
    entities: dict[str, ProjectionRecord],
    mode: str,
    hooks: ProjectionTextHooks,
) -> list[str]:
    return [
        hooks.wiki(
            entity_paths[ref],
            hooks.projection_value(entities[ref], entities[ref].get("name"), mode) or "Entity",
        )
        for ref in record.get("entity_refs", [])
        if ref in entity_paths
    ]


def _record_owner_lines(
    record: ProjectionRecord,
    kind: str,
    entity_paths: dict[str, str],
    entities: dict[str, ProjectionRecord],
    mode: str,
    hooks: ProjectionTextHooks,
) -> list[str]:
    if kind != "open_loop":
        return []
    if mode == "review_safe" and record.get("sensitive") and record.get("owner_refs"):
        return ["Redacted in review_safe mode"]
    return [
        hooks.wiki(
            entity_paths[ref],
            hooks.projection_value(entities[ref], entities[ref].get("name"), mode) or "Owner",
        )
        for ref in record.get("owner_refs", [])
        if ref in entity_paths
    ]


def _record_details(
    record: ProjectionRecord,
    kind: str,
    mode: str,
    timestamp_key: str,
    hooks: ProjectionTextHooks,
) -> list[str]:
    details = [
        f"Status: {hooks.markdown_literal(hooks.projection_value(record, record.get('status'), mode))}",
        f"Authority state: {hooks.markdown_literal(hooks.projection_value(record, record.get('authority_state') or 'accepted', mode))}",
        f"Source tier: {hooks.markdown_literal(hooks.projection_value(record, record.get('source_tier'), mode))}",
        f"{hooks.markdown_literal(timestamp_key)}: {hooks.markdown_literal(record.get(timestamp_key))}",
    ]
    if record.get("rationale"):
        details.append(
            f"Rationale: {hooks.markdown_literal(hooks.projection_value(record, record.get('rationale'), mode))}"
        )
    if record.get("uri"):
        details.append(
            f"URI: {hooks.markdown_literal(hooks.projection_value(record, record.get('uri'), mode))}"
        )
    if kind == "receipt":
        details.append(f"Verified: {hooks.markdown_literal(record.get('verified'))}")
    if kind == "open_loop":
        action_label = (
            "Historical next action" if record.get("status") == "resolved" else "Next action"
        )
        details.extend(
            [
                f"Priority: {hooks.markdown_literal(record.get('priority'))}",
                f"Due: {hooks.markdown_literal(record.get('due_at') or 'none')}",
                f"{action_label}: {hooks.markdown_literal(hooks.projection_value(record, record.get('next_action'), mode)) or 'none'}",
                f"Checkpoint: {hooks.markdown_literal(record.get('checkpoint_at') or 'none')}",
                f"Terminal condition: {hooks.markdown_literal(hooks.projection_value(record, record.get('terminal_condition'), mode)) or 'none'}",
                f"Resolution: {hooks.markdown_literal(hooks.projection_value(record, record.get('resolution'), mode)) or 'none'}",
            ]
        )
    return details


def _record_metadata(
    record: ProjectionRecord,
    kind: str,
    mode: str,
    timestamp_key: str,
    owner_lines: list[str],
    hooks: ProjectionTextHooks,
) -> dict[str, object]:
    metadata = {
        "kg_id": record["id"],
        "kg_type": kind,
        "kind": hooks.projection_value(record, record.get("kind"), mode),
        "status": hooks.projection_value(record, record.get("status"), mode),
        "authority_state": hooks.projection_value(
            record, record.get("authority_state", "accepted"), mode
        ),
        "source_tier": hooks.projection_value(record, record.get("source_tier"), mode),
        timestamp_key: record.get(timestamp_key),
        "updated_at": _latest_record_timestamp(record, timestamp_key),
    }
    if kind == "open_loop":
        metadata.update(
            {
                "priority": record.get("priority"),
                "due_at": record.get("due_at"),
                "owners": owner_lines,
                "next_action": hooks.projection_value(record, record.get("next_action"), mode),
                "checkpoint_at": record.get("checkpoint_at"),
                "terminal_condition": hooks.projection_value(
                    record, record.get("terminal_condition"), mode
                ),
                "resolved_at": record.get("resolved_at"),
                "resolution": hooks.projection_value(record, record.get("resolution"), mode),
            }
        )
    return metadata


def _latest_record_timestamp(record: ProjectionRecord, timestamp_key: str) -> str | None:
    """Return the most recent canonical timestamp without rewriting its representation."""
    candidates = (
        record.get("resolved_at"),
        record.get("last_observed_at"),
        record.get(timestamp_key),
    )
    parsed: list[tuple[datetime, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        normalized = f"{candidate[:-1]}+00:00" if candidate.endswith("Z") else candidate
        try:
            instant = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        parsed.append((instant.astimezone(timezone.utc), candidate))
    if parsed:
        return max(parsed, key=lambda item: item[0])[1]
    return next(
        (candidate for candidate in candidates if isinstance(candidate, str) and candidate),
        None,
    )


def render_record_note(
    record: ProjectionRecord,
    kind: str,
    entity_paths: dict[str, str],
    entities: dict[str, ProjectionRecord],
    mode: str,
    timestamp_key: str,
    *,
    hooks: ProjectionTextHooks,
) -> str:
    entity_lines = _record_entity_lines(record, entity_paths, entities, mode, hooks)
    owner_lines = _record_owner_lines(record, kind, entity_paths, entities, mode, hooks)
    details = _record_details(record, kind, mode, timestamp_key, hooks)
    metadata = _record_metadata(record, kind, mode, timestamp_key, owner_lines, hooks)
    metadata.update(_display_properties(record, hooks, mode), about=entity_lines)
    authority = hooks.projection_value(record, record.get("authority_state") or "accepted", mode)
    lifecycle = hooks.projection_value(
        record, record.get("lifecycle") or record.get("status") or "unknown", mode
    )
    if lifecycle == "conflicting":
        state = (
            "Proposed conflict — needs checking"
            if authority == "candidate"
            else "Conflict — needs checking"
        )
    elif lifecycle == "quarantined":
        state = "Proposed — needs checking" if authority == "candidate" else "Needs checking"
    elif authority == "candidate":
        state = "Proposed or needs checking"
    else:
        state = lifecycle
    lead: list[str] = [
        "## Status",
        hooks.bullet([f"State: {hooks.markdown_literal(state)}"]),
        "",
    ]
    if kind == "open_loop":
        if record.get("status") == "resolved":
            outcome = hooks.projection_value(record, record.get("resolution"), mode)
            completed = _display_timestamp(
                record.get("resolved_at"), "Completion date not recorded"
            )
            lead.extend(
                [
                    "## Outcome",
                    hooks.bullet(
                        [
                            hooks.markdown_literal(outcome) or "Outcome not recorded",
                            f"Completed: {hooks.markdown_literal(completed)}",
                        ]
                    ),
                    "",
                ]
            )
        else:
            action = hooks.projection_value(record, record.get("next_action"), mode)
            heading = "Proposed step" if authority == "candidate" else "Next step"
            lead.extend(
                [
                    f"## {heading}",
                    hooks.markdown_literal(action) or "No next step recorded.",
                    "",
                ]
            )
    return "\n".join(
        [
            hooks.frontmatter(metadata),
            f"# {hooks.markdown_literal(_record_label(record, hooks, mode))}",
            "",
            "## Summary",
            hooks.markdown_literal(hooks.projection_value(record, record.get("summary"), mode))
            or "No summary stored.",
            "",
            *lead,
            *(["## Owners", hooks.bullet(owner_lines), ""] if kind == "open_loop" else []),
            "## Entities",
            hooks.bullet(entity_lines),
            "",
            "## Evidence Refs",
            hooks.bullet(hooks.projection_refs(record, "evidence_refs", mode)),
            "",
            "## Details",
            hooks.bullet(details),
        ]
    )


def _base_property_metadata_reference(property_name: str) -> str:
    if property_name.startswith(("formula.", "note.")):
        return property_name
    return f"note.{property_name}"


def _base_property_order_reference(property_name: str) -> str:
    return property_name.removeprefix("note.")


def _base_folder_filter_scalar(folder: str) -> str:
    expression = f"file.inFolder({json.dumps(folder)})"
    if re.fullmatch(r"[\w ./-]+", folder):
        return expression
    return yaml_scalar(expression)


def base_file(name: str, folder: str, record_type: str, properties: list[str]) -> str:
    """Render an Obsidian Bases file in Obsidian's stable serialization form."""
    property_config = [
        "properties:",
        "  formula.readable_note:",
        f"    displayName: {_BASE_TITLE_LABELS.get(record_type, 'Note')}",
    ]
    for property_name in properties:
        property_reference = _base_property_metadata_reference(property_name)
        display_name = _BASE_COLUMN_LABELS.get(
            property_name, property_name.replace("_", " ").capitalize()
        )
        property_config.extend([f"  {property_reference}:", f"    displayName: {display_name}"])
    formulas = ["formulas:", f"  readable_note: {_READABLE_NOTE_EXPRESSION}"]
    if "formula.readable_state" in properties:
        formulas.append(f"  readable_state: {_READABLE_STATE_EXPRESSION}")
    if "formula.readable_source" in properties:
        formulas.append(f"  readable_source: {_READABLE_SOURCE_EXPRESSION}")
    return (
        "\n".join(
            [
                "filters:",
                "  and:",
                f"    - {_base_folder_filter_scalar(folder)}",
                f"    - note.kg_type == {json.dumps(record_type)}",
                *formulas,
                *property_config,
                "views:",
                "  - type: table",
                f"    name: {name}",
                "    order:",
                "      - formula.readable_note",
                *(f"      - {_base_property_order_reference(item)}" for item in properties),
            ]
        )
        + "\n"
    )


def loop_base_file(folder: str) -> str:
    """Render lifecycle-specific views over canonical open-loop episodes."""
    record_filter = 'note.kg_type == "open_loop"'
    properties = [
        ("formula.active_title", "Commitment"),
        ("formula.candidate_title", "Proposal"),
        ("formula.resolved_title", "Completed work"),
        ("owners", "Owner"),
        ("next_action", "Next step"),
        ("formula.candidate_next_action", "Proposed step"),
        ("checkpoint_at", "Check-in"),
        ("due_at", "Due"),
        ("resolution", "Outcome"),
        ("resolved_at", "Completed"),
    ]
    lines = [
        "filters:",
        "  and:",
        f"    - {_base_folder_filter_scalar(folder)}",
        f"    - {record_filter}",
        "formulas:",
        f"  readable_note: {_READABLE_NOTE_EXPRESSION}",
        f"  active_title: {_READABLE_NOTE_EXPRESSION}",
        f"  candidate_title: {_READABLE_NOTE_EXPRESSION}",
        "  candidate_next_action: note.next_action",
        f"  resolved_title: {_READABLE_NOTE_EXPRESSION}",
        "properties:",
    ]
    for property_name, display_name in properties:
        property_reference = _base_property_metadata_reference(property_name)
        lines.extend([f"  {property_reference}:", f"    displayName: {display_name}"])
    views = [
        (
            "Active",
            'note.status == "open"',
            [
                "formula.active_title",
                "owners",
                "next_action",
                "checkpoint_at",
                "due_at",
            ],
        ),
        (
            "Candidates",
            'note.status == "candidate"',
            [
                "formula.candidate_title",
                "owners",
                "formula.candidate_next_action",
                "checkpoint_at",
            ],
        ),
        (
            "Resolved",
            'note.status == "resolved"',
            [
                "formula.resolved_title",
                "owners",
                "resolution",
                "resolved_at",
            ],
        ),
    ]
    lines.append("views:")
    for name, filter_expression, order in views:
        lines.extend(
            [
                "  - type: table",
                f"    name: {name}",
                "    filters:",
                "      and:",
                f"        - {filter_expression}",
                "    order:",
                *(
                    f"      - {_base_property_order_reference(property_name)}"
                    for property_name in order
                ),
            ]
        )
    return "\n".join(lines) + "\n"
