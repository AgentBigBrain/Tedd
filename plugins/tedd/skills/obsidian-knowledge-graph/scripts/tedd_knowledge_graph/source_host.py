"""Trusted in-process host capabilities; never deserialize these from source data.

The default CLI has no host. A qualified embedding supplies actual callbacks;
constructing this value is not proof of acquisition or private-viewer qualification.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from .source_authority import ProviderCheck
from .source_contracts import SourceExcerpt, SourceMetadata, SourceRef, SourceScope
from .source_reader_contracts import SourceReader
from .source_review import ReviewBook, ReviewRendering
from .v4_application_contracts import GraphReadSession


@dataclass(frozen=True, repr=False)
class SourceHost:
    # Optional qualified parser/renderer capability. Merely injecting it grants
    # no source access, retention, review acknowledgment, publication or delivery.
    reader: SourceReader | None = field(default=None, kw_only=True)
    # Bind a concrete local original to the actual host's user selection. This
    # callback grants no retention policy and must not read neighboring content.
    authorize_local: Callable[[Path], bool] | None = None
    # Host-owned export root and actual private owner-confirmation UI. Serialized
    # tool arguments describe proposals only; they cannot set either capability.
    export_root: Path | None = None
    confirm_exports: Callable[[str], bool] | None = None
    # A new binding of saved evidence is not a new acquisition. Apply current
    # negative retention controls without requiring the vanished original again.
    check_retention: Callable[[SourceMetadata], None] | None = None
    # Obtain provider-bound identity/version metadata before a new direct read.
    # This cannot widen the approved scope, source selection or sensitivity.
    prepare_google: Callable[[SourceMetadata], SourceMetadata] | None = None
    # Acquisition is limited to the approved workflow selection, exact principal
    # and revision. The host checks view/copy/download restrictions, never just
    # ownership or a model's assertion. Bytes must not be reconstructed by a model.
    acquire_google: Callable[[SourceMetadata, int], bytes] | None = None
    provider_check: ProviderCheck | None = None
    # A short local reservation of the host's actual workflow state. It refuses
    # paused, closed or excluded work and serializes a concurrent pause through
    # publication. It performs no network work and creates no second state store.
    collecting: Callable[[GraphReadSession, SourceScope], AbstractContextManager[None]] | None = (
        None
    )
    # Bind this operation's prepared result to a fresh LOCAL reservation held by
    # the actual transport write. Factories must not perform provider/network I/O.
    retain_delivery: Callable[[Callable[[], AbstractContextManager[None]]], None] | None = None
    # Deliver inert, selected evidence only to the qualified human display. Do
    # not echo its bytes into model-visible results, logs, or ordinary previews.
    # This is a bounded local handoff, not a wait for human input under locks.
    display_owner: Callable[[SourceExcerpt], None] | None = None
    display_owner_image: Callable[[SourceRef, bytes], None] | None = None
    display_owner_rendering: Callable[[ReviewRendering, bytes], None] | None = None
    # Turn-owned image delivery/review state; never populated from tool arguments.
    reviews: ReviewBook | None = None
    # Build a blank private viewer, release only inside the supplied fresh access
    # reservation, then await the owner's review outside all publication locks.
    review_owner_image: (
        Callable[[ReviewRendering, bytes, AbstractContextManager[None]], bool] | None
    ) = None
    # Enumerate one approved folder page; no source preservation or graph write.
    list_google: Callable[[SourceScope, str, str], dict[str, object]] | None = None
    # Actual host cancellation signal; never supplied in serialized tool input.
    cancelled: Event | None = None
