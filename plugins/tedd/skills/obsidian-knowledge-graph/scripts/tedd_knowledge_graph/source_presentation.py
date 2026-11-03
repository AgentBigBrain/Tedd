"""Private preview policy and inert, already-authorized source-note descriptions.

No acquisition, archive I/O, viewer launch, model output, or activation lives here.
The existing projection renderer and publisher consume these bounded values.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .source_contracts import (
    SOURCE_TITLE_CHARS,
    SourceArchiveError,
    SourceExcerpt,
    SourceRef,
    SourceScope,
    decode_scope,
    digest,
    encode_units,
    identifier,
    object_fields,
    sha256,
    text,
)

PRESENTATION_MARKER = "<!-- tedd-source-presentation:1 -->"
MAX_SOURCE_VIEWS = 64
MAX_PREVIEW_FILE_BYTES = 8 * 1024**2
MAX_PREVIEW_BATCH_BYTES = 32 * 1024**2
MAX_PREVIEW_FILES = 64


@dataclass(frozen=True, repr=False)
class PreviewPolicy:
    """One ordinary-source destination approval, not a native-host qualification."""

    scope: SourceScope
    vault_path: str
    root_name: str
    projection_root_id: str
    enabled: bool
    exposure: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SourceScope) or type(self.enabled) is not bool:
            raise SourceArchiveError("scope_denied")
        if not Path(text(self.vault_path)).is_absolute():
            raise SourceArchiveError("scope_denied")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}", text(self.root_name)):
            raise SourceArchiveError("scope_denied")
        identifier(self.projection_root_id)
        if self.exposure not in ("local_only", "approved_sync"):
            raise SourceArchiveError("scope_denied")


def decode_preview_policy(
    value: object, config: Mapping[str, object], scope: SourceScope
) -> PreviewPolicy | None:
    if value is None:
        return None
    obj = object_fields(
        value,
        {
            "format",
            "scope",
            "vault_path",
            "root_name",
            "projection_root_id",
            "enabled",
            "audience",
            "sensitivities",
            "exposure",
            "exposure_approved",
        },
    )
    if (
        type(obj["format"]) is not int
        or obj["format"] != 1
        or obj["audience"] != "owner"
        or obj["sensitivities"] != ["ordinary"]
        or obj["exposure_approved"] is not True
        or type(obj["enabled"]) is not bool
    ):
        raise SourceArchiveError("scope_denied")
    policy = PreviewPolicy(
        decode_scope(obj["scope"]),
        text(obj["vault_path"]),
        text(obj["root_name"]),
        identifier(obj["projection_root_id"]),
        obj["enabled"],
        text(obj["exposure"]),
    )
    if (
        policy.scope != scope
        or os.path.normcase(policy.vault_path) != os.path.normcase(text(config.get("vault_path")))
        or policy.root_name != config.get("root_name")
        or policy.projection_root_id != config.get("projection_root_id")
    ):
        raise SourceArchiveError("scope_denied")
    return policy


@dataclass(frozen=True, repr=False)
class PreparedSourceView:
    """Exact reading for bounded, graph-linked owner display—not an access grant."""

    memory_ids: tuple[str, ...]
    excerpt: SourceExcerpt
    title: str
    image_path: str | None = None

    def __post_init__(self) -> None:
        if type(self.memory_ids) is not tuple or not 1 <= len(self.memory_ids) <= MAX_SOURCE_VIEWS:
            raise SourceArchiveError("limit_exceeded")
        for value in self.memory_ids:
            identifier(value)
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise SourceArchiveError()
        text(self.title, maximum=SOURCE_TITLE_CHARS)
        excerpt = self.excerpt
        if (
            not isinstance(excerpt, SourceExcerpt)
            or excerpt.integrity != "verified"
            or not excerpt.units
            or excerpt.privacy != "original"
            or excerpt.coverage not in ("complete", "partial")
        ):
            raise SourceArchiveError("scope_denied")
        sha256(excerpt.history_head)
        if (
            not isinstance(excerpt.ref, SourceRef)
            or excerpt.ref.anchor_id not in {unit.anchor_id for unit in excerpt.units}
            or any(
                type(count) is not int or count < 0
                for count in (
                    excerpt.previous_units,
                    excerpt.remaining_units,
                    excerpt.remaining_versions,
                )
            )
        ):
            raise SourceArchiveError()
        if len(encode_units(excerpt.units).decode("utf-8")) > 12_000:
            raise SourceArchiveError("limit_exceeded")
        if self.image_path is not None and not re.fullmatch(
            rf"20 Evidence/source-{self.binding}-[0-9a-f]{{64}}-[1-9a-f][0-9a-f]{{0,6}}-[0-9a-f]{{64}}\.png",
            self.image_path,
        ):
            raise SourceArchiveError("scope_denied")

    @property
    def binding(self) -> str:
        return digest(self.excerpt.ref.as_uri().encode("utf-8"))[:32]


@dataclass(frozen=True, repr=False)
class RetainedSourceView:
    """In-memory recovery of one authenticated note, never independent authority.

    Only the publisher recovers this from content-bound ownership. The evidence
    selector must revalidate lineage, policy and history before rendering it.
    """

    memory_ids: tuple[str, ...]
    ref: SourceRef
    history_head: str
    title: str
    body: str
    image_path: str | None = None

    @property
    def binding(self) -> str:
        return digest(self.ref.as_uri().encode("utf-8"))[:32]


def render_source_view(
    view: PreparedSourceView | RetainedSourceView,
    *,
    escape: Callable[[object], str],
    root_name: str = "",
) -> tuple[str, str]:
    """Render untrusted text as indented literal text; never active source markup."""
    if isinstance(view, RetainedSourceView):
        # Reuse the signed inert body; only freshly selected opaque links change.
        body = re.sub(
            r"(?m)^- Linked memories: .*$",
            "- Linked memories: " + ",".join(view.memory_ids),
            view.body,
        )
        return f"20 Evidence/source-{view.binding}.md", body
    excerpt = view.excerpt
    lines = [
        PRESENTATION_MARKER,
        f"# {escape(view.title)}",
        "",
        "Derivative reading copy — historical evidence, not a new observation.",
        "",
        f"- Source version: {escape(excerpt.source_version)}",
        f"- Captured: {escape(excerpt.captured_at)}",
        f"- Observed: {escape(excerpt.observed_at or 'unknown')}",
        f"- Source time: {escape(excerpt.source_time or 'unknown')}",
        f"- Provider revision: {escape(excerpt.provider_revision or 'unknown')}",
        f"- Representation: {escape(excerpt.representation or 'unknown')}",
        f"- Reader: {escape(excerpt.reader_id or 'unknown')}",
        f"- Coverage: {escape(excerpt.coverage)}",
        f"- Context omitted: {excerpt.previous_units + excerpt.remaining_units} units",
        f"- Later observations: {len(excerpt.later_versions) + excerpt.remaining_versions} (not necessarily newer source content)",
        f"- Exact archived reference: {escape(excerpt.ref.as_uri())}",
        f"- History head: {excerpt.history_head}",
        "- Linked memories: " + ",".join(view.memory_ids),
        "",
        "## Saved source text",
        "",
    ]
    returned_ids = {unit.source_id for unit in excerpt.units if unit.source_id}
    for unit in excerpt.units:
        location = unit.location
        label = (
            f"{location.kind} {location.start}–{location.end}"
            if location
            else f"{unit.kind} {unit.position}"
        )
        if location and location.container:
            label += f" in {location.container}"
        if location and location.kind == "cell":
            label += f", columns {location.column_start}–{location.column_end}"
        lines.extend([f"### {escape(label)}", ""])
        if unit.kind == "message":
            lines.extend(
                [
                    f"- Speaker: {escape(unit.speaker or 'unknown')}",
                    f"- Message time: {escape(unit.source_time or 'unknown')}",
                    f"- Message identifier: {escape(unit.source_id or 'unknown')}",
                ]
            )
        if unit.reply_to:
            status = "included" if unit.reply_to in returned_ids else "not included"
            lines.append(f"- Reply to: {escape(unit.reply_to)} ({status})")
        for link in unit.links:
            lines.append(
                f"- {escape(link.kind)}: {escape(link.source_id or 'unknown')}; "
                f"version: {escape(link.version or 'unknown')}; "
                f"unit: {escape(link.unit_id or 'unknown')}; not included"
            )
        lines.append("")
        lines.extend("    " + line for line in unit.text.splitlines())
        lines.append("")
    if excerpt.gaps:
        lines.extend(["## Missing context", "", *(f"- {escape(gap)}" for gap in excerpt.gaps), ""])
    if view.image_path is not None:
        lines.extend(
            [
                "## Saved page image",
                "",
                "Only the approved saved page is copied; this is not the whole document.",
                "",
                f"![[{root_name + '/' if root_name else ''}{view.image_path}]]",
                "",
            ]
        )
    return f"20 Evidence/source-{view.binding}.md", "\n".join(lines)
