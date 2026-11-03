"""Typed service surface for legacy graph mutation.

Implementation is split by responsibility: entity identity, record/claim
semantics, and packet capture coordination. The flat ``knowledge_graph``
module composes call-time dependencies so historical monkeypatch hooks remain
observable without importing the facade from these private services.
"""

from __future__ import annotations

from .legacy_capture import LegacyCaptureService
from .legacy_identity import LegacyIdentityService
from .legacy_mutation_contracts import (
    CaptureDependencies,
    IdentityDependencies,
    JsonObject,
    JsonValue,
    RecordDependencies,
    require_json_object,
)
from .legacy_records import LegacyRecordService

__all__ = [
    "CaptureDependencies",
    "IdentityDependencies",
    "JsonObject",
    "JsonValue",
    "LegacyCaptureService",
    "LegacyIdentityService",
    "LegacyRecordService",
    "RecordDependencies",
    "require_json_object",
]
