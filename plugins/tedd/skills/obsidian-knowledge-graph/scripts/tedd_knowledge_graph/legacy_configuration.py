"""Configuration and projection-target policy for the legacy facade."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .source_contracts import decode_scope
from .source_presentation import PreviewPolicy, decode_preview_policy

JsonObject = dict[str, object]
LEGACY_IMPLICIT_ROOT_NAME = "Codex Knowledge"


@dataclass(frozen=True)
class OwnerPreviewTransition:
    """Non-persisted result of applying preview approval lifecycle rules."""

    policy: Literal["unchanged", "invalidated"]
    old_target_copies_may_remain: bool
    old_vault_path: str | None = None
    old_root_name: str | None = None

    def __post_init__(self) -> None:
        residual_target = self.old_vault_path is not None or self.old_root_name is not None
        if self.policy == "invalidated":
            if (
                not self.old_target_copies_may_remain
                or self.old_vault_path is None
                or self.old_root_name is None
            ):
                raise ValueError("Invalidated preview approval requires the exact old target")
        elif self.old_target_copies_may_remain or residual_target:
            raise ValueError("Unchanged preview approval cannot report residual target copies")

    def public_mapping(self) -> JsonObject:
        result: JsonObject = {
            "policy": self.policy,
            "old_target_copies_may_remain": self.old_target_copies_may_remain,
        }
        if self.old_target_copies_may_remain:
            result.update(
                {
                    "old_vault_path": self.old_vault_path,
                    "old_root_name": self.old_root_name,
                }
            )
        return result


class ConfigurationOutcome(dict[str, object]):
    """Configured mapping plus a transient transition, compatible with legacy callers.

    Subclassing ``dict`` preserves the facade's historical mapping behavior while
    keeping diagnostics out of both the persisted configuration and its JSON shape.
    """

    __slots__ = ("owner_preview",)

    def __init__(
        self,
        config: Mapping[str, object],
        owner_preview: OwnerPreviewTransition,
    ) -> None:
        super().__init__(config)
        self.owner_preview = owner_preview


class AtomicWrite(Protocol):
    def __call__(self, path: Path, content: str, mode: int = 0o600) -> None: ...


@dataclass(frozen=True)
class ConfigurationDependencies:
    """Live facade hooks and policy values used by configuration operations."""

    config_schema_version: str
    default_root_name: str
    default_store_path: Path
    default_v4_store_path: Path
    is_link_or_reparse_point: Callable[[Path], bool]
    ensure_projection_target: Callable[[Path, str], Path]
    load_config: Callable[[Path], JsonObject]
    secure_state_path: Callable[[object, str], Path]
    state_privacy_warnings: Callable[[Path, Path, JsonObject], list[str]]
    normalize_space: Callable[[object], str]
    token_hex: Callable[[int], str]
    utc_now: Callable[[], str]
    atomic_write: AtomicWrite


def default_unconfigured_store_path(dependencies: ConfigurationDependencies) -> Path:
    """Prefer v4 only when no legacy default path exists at all."""

    legacy_path = dependencies.default_store_path
    if legacy_path.exists() or dependencies.is_link_or_reparse_point(legacy_path):
        return legacy_path
    return dependencies.default_v4_store_path


def resolve_store(
    args: argparse.Namespace,
    config: JsonObject,
    dependencies: ConfigurationDependencies,
) -> Path:
    raw = getattr(args, "store", None) or config.get("store_path")
    if not raw:
        raw = str(default_unconfigured_store_path(dependencies))
    return dependencies.secure_state_path(raw, "Canonical store")


def configure_locked(
    args: argparse.Namespace,
    config_path: Path,
    dependencies: ConfigurationDependencies,
) -> ConfigurationOutcome:
    """Apply a configuration change while the facade holds the config lock."""

    vault = Path(args.vault).expanduser().resolve()
    config = dependencies.load_config(config_path)
    existing_root = dependencies.normalize_space(config.get("root_name")) or (
        LEGACY_IMPLICIT_ROOT_NAME if config.get("vault_path") else dependencies.default_root_name
    )
    root_name = args.root_name or existing_root
    existing_preview: PreviewPolicy | None = None
    preview_value = config.get("source_preview_policy")
    if preview_value is not None:
        embedded_scope = preview_value.get("scope") if isinstance(preview_value, Mapping) else None
        existing_preview = decode_preview_policy(
            preview_value,
            config,
            decode_scope(embedded_scope),
        )
    dependencies.ensure_projection_target(vault, root_name)
    store_path = dependencies.secure_state_path(
        args.store
        or config.get("store_path")
        or str(default_unconfigured_store_path(dependencies)),
        "Canonical store",
    )
    proposed_config: JsonObject = {
        **config,
        "vault_path": str(vault),
        "root_name": root_name,
        "store_path": str(store_path),
    }
    privacy_warnings = dependencies.state_privacy_warnings(
        config_path,
        store_path,
        proposed_config,
    )
    if privacy_warnings:
        raise ValueError(
            "Refusing to place canonical graph state in the synced Obsidian vault: "
            + "; ".join(privacy_warnings)
        )
    target_changed = bool(config) and (
        dependencies.normalize_space(config.get("vault_path")) != str(vault)
        or existing_root != root_name
    )
    store_changed = bool(config) and (
        dependencies.normalize_space(config.get("store_path")) != str(store_path)
    )
    manifest_key = (
        dependencies.token_hex(32)
        if target_changed
        else config.get("projection_manifest_key") or dependencies.token_hex(32)
    )
    projection_root_id = (
        dependencies.token_hex(16)
        if target_changed
        else config.get("projection_root_id") or dependencies.token_hex(16)
    )
    preview_invalidated = existing_preview is not None and (target_changed or store_changed)
    if target_changed or store_changed:
        config.pop("source_preview_policy", None)
    if store_changed:
        config.pop("source_owner_access_policy", None)
        config.pop("source_owner_view_access_policy", None)
    config.update(
        {
            "schema_version": dependencies.config_schema_version,
            "vault_path": str(vault),
            "root_name": root_name,
            "store_path": str(store_path),
            "projection_manifest_key": manifest_key,
            "projection_root_id": projection_root_id,
            "updated_at": dependencies.utc_now(),
        }
    )
    dependencies.atomic_write(config_path, json.dumps(config, indent=2, sort_keys=True) + "\n")
    owner_preview = (
        OwnerPreviewTransition(
            "invalidated",
            True,
            old_vault_path=existing_preview.vault_path,
            old_root_name=existing_preview.root_name,
        )
        if preview_invalidated and existing_preview is not None
        else OwnerPreviewTransition("unchanged", False)
    )
    return ConfigurationOutcome(config, owner_preview)


def resolve_projection(
    args: argparse.Namespace,
    config: JsonObject,
    dependencies: ConfigurationDependencies,
) -> tuple[Path, str]:
    configured_vault = dependencies.normalize_space(config.get("vault_path"))
    configured_root = (
        dependencies.normalize_space(config.get("root_name")) or LEGACY_IMPLICIT_ROOT_NAME
    )
    if not configured_vault:
        raise ValueError(
            "No Obsidian vault configured. Run configure --vault /absolute/path first."
        )
    vault = Path(os.path.expanduser(configured_vault)).resolve()
    requested_vault = getattr(args, "vault", None)
    requested_root = getattr(args, "root_name", None)
    if requested_vault and Path(requested_vault).expanduser().resolve() != vault:
        raise ValueError(
            "Projection target differs from the configured vault; "
            "run configure to rotate projection ownership"
        )
    if requested_root and requested_root != configured_root:
        raise ValueError(
            "Projection root differs from the configured root; "
            "run configure to rotate projection ownership"
        )
    return vault, configured_root


__all__ = [
    "ConfigurationOutcome",
    "ConfigurationDependencies",
    "OwnerPreviewTransition",
    "configure_locked",
    "default_unconfigured_store_path",
    "resolve_projection",
    "resolve_store",
]
