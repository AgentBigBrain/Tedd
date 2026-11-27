"""Inputs shared by command-line and hosted source discovery."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from .source_contracts import SourceArchiveError, SourceDiscovery
from .v4_application_contracts import GraphReadSession, JsonObject

if TYPE_CHECKING:
    from .source_archive import Archive


REQUIRED_TEXT = (
    "consumer_id",
    "producer_agent_id",
    "task_id",
    "producer_run_id",
    "subscription_id",
)
OPTIONAL_TEXT = ("source_id", "source_version", "query", "snapshot_sha256")
MAX_TEXT = 8192


def _string(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise SourceArchiveError("invalid_request")
    return value


@dataclass(frozen=True, kw_only=True)
class LookupRequest:
    command: ClassVar[Literal["lookup"]] = "lookup"
    config: Path
    session: GraphReadSession
    subscription_id: str
    subject_ids: tuple[str, ...]
    source_id: str | None
    source_version: str | None
    query: str | None
    offset: int
    snapshot_sha256: str | None

    @classmethod
    def from_cli(cls, args: Namespace) -> LookupRequest:
        return cls(
            config=Path(args.config),
            session=GraphReadSession(
                args.producer_agent_id, args.task_id, args.producer_run_id, args.consumer_id
            ),
            subscription_id=args.subscription_id,
            subject_ids=tuple(args.subject_id),
            source_id=args.source_id,
            source_version=args.source_version,
            query=args.query,
            offset=args.offset,
            snapshot_sha256=args.snapshot_sha256,
        )

    @classmethod
    def from_named(cls, config: Path, values: JsonObject) -> LookupRequest:
        required = {*REQUIRED_TEXT, "subject_id"}
        if not required <= values.keys() <= required | {*OPTIONAL_TEXT, "offset"}:
            raise SourceArchiveError("invalid_request")
        subjects = values["subject_id"]
        offset = values.get("offset", 0)
        if (
            not isinstance(subjects, list)
            or not 1 <= len(subjects) <= 64
            or type(offset) is not int
        ):
            raise SourceArchiveError("invalid_request")
        optional = {key: _string(values[key]) for key in OPTIONAL_TEXT if key in values}
        return cls(
            config=config,
            session=GraphReadSession(
                _string(values["producer_agent_id"]),
                _string(values["task_id"]),
                _string(values["producer_run_id"]),
                _string(values["consumer_id"]),
            ),
            subscription_id=_string(values["subscription_id"]),
            subject_ids=tuple(_string(item) for item in subjects),
            source_id=optional.get("source_id"),
            source_version=optional.get("source_version"),
            query=optional.get("query"),
            offset=offset,
            snapshot_sha256=optional.get("snapshot_sha256"),
        )

    def discover(self, archive: Archive) -> SourceDiscovery:
        return archive.discover(
            source_id=self.source_id,
            source_version=self.source_version,
            query=self.query,
            offset=self.offset,
            snapshot_sha256=self.snapshot_sha256,
        )
