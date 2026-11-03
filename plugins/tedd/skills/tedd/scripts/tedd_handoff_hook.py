#!/usr/bin/env python3
"""Bounded host handoff reminders. Never read transcripts or capture knowledge."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import tedd_setup as setup

# Narrow compatibility adapter to the CLI's existing descriptor/ACL/publication
# boundary. Do not create a second, weaker private-file implementation for hooks.
from knowledge_graph_v4_cli import (
    _plan_parent_guard,
    _read_handoff_file,
    _safe_output_path,
    _write_handoff_file,
)
from platform_support import mode_has_any, owner_is_current_user
from tedd_knowledge_graph import handoff_receipts as receipts
from tedd_knowledge_graph.source_contracts import check_json_bounds, unique_object

PENDING = "A memory handoff is pending from your previous turn."
FINISH = (
    "Finish with Memory: saved N (ids), candidates M (ids), not saved: <reason or none>. "
    "Use actual counts; honor no-save/no-files. Report a missing receipt pending, even with no saves."
)
# A numeric token must remain distinct from absent optional identity/flag fields.
_UNUSED_NUMBER = object()


def _ignored_number(value: str) -> object:
    """Native telemetry is unused; never materialize unbounded numeric values."""
    if value in {"NaN", "Infinity", "-Infinity"}:
        raise ValueError("hook input contains a non-JSON number")
    return _UNUSED_NUMBER


@dataclass(frozen=True)
class HookRequest:
    host: str
    name: str
    session: str
    directory: Path
    prompt: str | None
    source: str | None
    continued: bool

    @classmethod
    def decode(cls, host: str, event: dict[str, object]) -> HookRequest:
        name = event.get("hook_event_name")
        directory = event.get("cwd")
        source = event.get("source")
        continued = event.get("stop_hook_active", False)
        if (
            host not in {"claude", "codex"}
            or not isinstance(name, str)
            or name not in {"SessionStart", "UserPromptSubmit", "Stop"}
        ):
            raise ValueError("unsupported handoff event")
        if (
            not isinstance(directory, str)
            or len(directory) > 4096
            or not Path(directory).is_absolute()
        ):
            raise ValueError("hook requires the current absolute project directory")
        if type(continued) is not bool or (source is not None and not isinstance(source, str)):
            raise ValueError("invalid native hook flags")
        prompt = event.get("prompt_id" if host == "claude" else "turn_id")
        return cls(
            host,
            str(name),
            receipts.validate_prompt_id(event.get("session_id")),
            Path(directory),
            None if prompt is None else receipts.validate_prompt_id(prompt),
            source,
            continued,
        )


def session_path(host: str, session_id: str) -> Path:
    key = hashlib.sha256(json.dumps([host, session_id]).encode()).hexdigest()
    return receipts.handoff_directory() / "sessions" / (key + ".json")


def session_context(
    event: HookRequest, config: Path | None
) -> tuple[Path, dict[str, object]] | None:
    """Require the approved directory, operator, graph and exact native consumer."""
    config = config or Path(
        os.environ.get("TEDD_GRAPH_CONFIG", str(setup.graph.DEFAULT_CONFIG_PATH))
    )
    profile = setup.tedd_profile.DEFAULT_PROFILE.expanduser().absolute()
    project = setup.real_directory(event.directory)
    bindings = setup.decode_bindings(setup.private_object(profile.parent / "project-bindings.json"))
    if not any(
        project == Path(str(binding["directory"]))
        or (binding["include_descendants"] and Path(str(binding["directory"])) in project.parents)
        for binding in bindings
    ):
        return None
    request = setup.SetupRequest(
        host=event.host,
        graph_config=config,
        project=project,
        profile=profile,
        host_home=Path.home() / ".codex",
        python=Path(sys.executable),
    )
    context = setup.session_binding(request, event.session, "run_handoff_check")
    return setup.existing_store_path(config, setup.private_object(config)), context


def _state(value: object) -> dict[str, object]:
    if value is None:
        return {}
    fields = {
        "version",
        "host",
        "session_id",
        "store_id",
        "generation",
        "prompt_id",
        "blocked_prompt",
        "reminded_prompt",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("invalid private handoff session state")
    if type(value["generation"]) is not int or value["generation"] < 1:
        raise ValueError("invalid handoff generation")
    for key in fields - {"version", "generation"}:
        if not isinstance(value[key], str) or len(value[key]) > 200:
            raise ValueError("invalid handoff session field")
    return value


def handle_event(
    host: str, event: dict[str, object], config: Path | None = None
) -> dict[str, object]:
    """Return native hook JSON. State contains only identities, never prompt prose."""
    if os.environ.get("TEDD_NO_LOCAL_WRITES") == "1":
        return {}
    return _handle_request(HookRequest.decode(host, event), config)


def _handle_request(event: HookRequest, config: Path | None) -> dict[str, object]:
    host, name, session = event.host, event.name, event.session
    if name == "Stop" and event.continued:
        return {}
    if name == "SessionStart" and event.source not in {"clear", "fork"}:
        return {}
    bound = session_context(event, config)
    if bound is None:
        return {}
    store_path, context = bound
    if name == "Stop" and context["coordination"] != "ready":
        return {}
    # parents=True hardens only the leaf on POSIX. Create the receipt root first
    # so a later capture never inherits a public intermediate handoffs folder.
    setup.graph.ensure_private_directory(receipts.handoff_directory())
    if not owner_is_current_user(receipts.handoff_directory()) or mode_has_any(
        receipts.handoff_directory(), 0o077
    ):
        raise ValueError("Handoff directory must be private and owned by the current user")
    path = _safe_output_path(str(session_path(host, session)), "Handoff session", force=True)
    guard = _plan_parent_guard(path)
    with setup.graph.store_lock(path), setup.storage.open_store(store_path, readonly=True) as store:
        guard()
        graph_state = store.state()
        previous = _state(_read_handoff_file(path))
        fresh = {
            "version": 1,
            "host": host,
            "session_id": session,
            "store_id": graph_state["store_id"],
            "generation": graph_state["generation"],
            "prompt_id": "",
            "blocked_prompt": "",
            "reminded_prompt": "",
        }
        if previous and any(
            previous[key] != fresh[key] for key in ("host", "session_id", "store_id", "generation")
        ):
            raise ValueError("handoff session identity changed")
        if name == "SessionStart":
            _write_handoff_file(path, fresh)
            return {}
        if name == "Stop" and not previous.get("prompt_id"):
            return {}
        raw_prompt = event.prompt
        if name == "UserPromptSubmit":
            # Older Codex releases lack turn_id. Generate one timestamp per input,
            # persisted here; Stop uses that exact token rather than a new clock.
            prompt = receipts.validate_prompt_id(
                raw_prompt or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            )
        else:
            prompt = receipts.validate_prompt_id(raw_prompt or previous.get("prompt_id"))
        state = previous or fresh
        pending = False
        prior_prompt = state["prompt_id"]
        if context["coordination"] == "ready" and prior_prompt:
            identity = receipts.HandoffIdentity(
                str(context["consumer_id"]), str(context["agent_id"]), session, str(prior_prompt)
            )
            expected = receipts.reconstruct_receipt(store, identity)
            document = _read_handoff_file(receipts.receipt_path(expected, identity))
            pending = not (
                receipts.verify_receipt(store, identity, document)
                or receipts.accepts_nothing(store, identity, document)
            )
        if name == "UserPromptSubmit":
            remind = pending and prior_prompt != prompt and state["reminded_prompt"] != prior_prompt
            if remind:
                state["reminded_prompt"] = prior_prompt
            state["prompt_id"] = prompt
            if prompt != prior_prompt:
                state["blocked_prompt"] = ""
            _write_handoff_file(path, state)
            return (
                {"hookSpecificOutput": {"hookEventName": name, "additionalContext": PENDING}}
                if remind
                else {}
            )
        if prior_prompt != prompt or not pending or state["blocked_prompt"] == prompt:
            return {}
        state["blocked_prompt"] = prompt
        _write_handoff_file(path, state)
        if host == "codex":
            # Codex block creates another user prompt. A warning preserves the
            # original receipt identity and never creates a continuation loop.
            return {"systemMessage": "Memory handoff pending. " + FINISH}
        return {"decision": "block", "reason": FINISH}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "claude"), required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--current-prompt", metavar="SESSION_ID")
    args = parser.parse_args(argv)
    if os.environ.get("TEDD_NO_LOCAL_WRITES") == "1":
        return 0
    try:
        if args.current_prompt:
            session = receipts.validate_prompt_id(args.current_prompt)
            state = _state(_read_handoff_file(session_path(args.host, session)))
            result: dict[str, object] = {
                "prompt_id": state.get("prompt_id"),
                "status": "recorded" if state else "pending",
            }
        else:
            payload = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            check_json_bounds(payload, 2 * 1024 * 1024)
            event: object = json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=unique_object,
                parse_int=_ignored_number,
                parse_float=_ignored_number,
                parse_constant=_ignored_number,
            )
            if not isinstance(event, dict):
                raise ValueError("hook event must be a JSON object")
            result = handle_event(args.host, event, args.config)
        if result:
            print(json.dumps(result))
        return 0
    except FileNotFoundError:
        return 0
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        setup.storage.V4Error,
        setup.tedd_profile.ProfileError,
    ):
        # Missing setup is a silent opt-out; malformed configured state is not
        # success. This bounded warning never echoes paths, prompt or secrets.
        print(
            json.dumps(
                {
                    "systemMessage": "Tedd handoff check unavailable; follow the memory skill and report pending memory."
                }
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
