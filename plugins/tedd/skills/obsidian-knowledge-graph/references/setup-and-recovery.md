# Configure and recover the graph

Read this for setup, launcher/configuration changes, backup, migration, index repair, or full validation. Routine reads use [recall](recall.md).

Read the [graph skill](../SKILL.md) for the shared invariants. Run command examples from the `obsidian-knowledge-graph` skill directory, using its configured store and a supported interpreter; `scripts/` paths are relative to that directory. On Windows use the qualified `py -3` or `python` launcher and native paths.

## Paths and configuration

### Guided existing-brain setup

The implementation candidate includes `tedd_setup.py` in the sibling Tedd skill.
Run it with a qualified Python interpreter. It previews one host, an existing
private profile/brain and exact approved project selectors. It does not initialize
a brain, collect sources, change credentials, project notes or rename a vault root.

From the `tedd` skill directory, preview your selected configuration and IDs:

```bash
python3 scripts/tedd_setup.py --host codex --graph-config "/private/graph/config.json" --project "/work/example-project" --entity-id "entity_example_project"
```

Review the directory, graph identity, selectors and host command. Re-run the same
command with `--apply --fingerprint PREVIEW_FINGERPRINT` only when those changes
are approved. `--include-descendants` expands coverage only when explicitly
previewed and approved. The default profile and binding file are
`~/.codex/tedd/profile.json` and `~/.codex/tedd/project-bindings.json`.
Use `--operator-id` if the existing profile has more than one operator.

Codex writes its own configuration in a private temporary copy before apply.
The final replacement preserves other settings and server arguments, but Codex
can normalize TOML formatting and comments. Private rollback copies precede any
replacement. A partial failure reports possibly changed components and that backup;
inspect current settings before recovery, rather than restoring over unrelated edits.
Configuration drift requires a fresh preview. Repeating unchanged setup writes no
configuration or binding bytes. The packaged duplicate must remain disabled when
an explicit Codex endpoint is active; inspect the host's plugin settings separately.

For Claude, `--host claude` saves only the approved project binding. The candidate
plugin's `userConfig` owns the interpreter and graph-config file selections; setup
does not write Claude settings. Neither route reports connected from a configuration
check. `host_discovery` and `direct_protocol` remain `not_run` until separately
verified. Host discovery must include a scoped native read and another after restart.

Windows Desktop's tested Claude Code 2.1.288 loader uses `tedd@inline` for the
same plugin whose installer stores options as `tedd@tedd`. If normal CLI health
connects but loading the cached directory fails with a missing `python` option,
review a backup of the user's `~/.claude/settings.json`. A compatibility entry
under `pluginConfigs["tedd@inline"].options` can use exactly the existing reviewed
`python` and `graph_config` paths. Preserve the original entry, project enablement
and all other settings; do not replace conflicting values or add another server.
Project settings do not supply these options. This is a host-specific path lookup
repair, not a setup-helper write or proof of native scoped reads. Reverify the
fresh Desktop session, keep both path entries consistent on later changes, and
remove only the added alias for rollback. Do not generalize this check to untested
hosts or enable sources or broader memory scope. Tool loading alone does not
establish scoped reads, lifecycle behavior or interactive permission handling;
verify each required path in the actual host.

The read-only session check uses the same entry point:

```bash
python3 scripts/tedd_setup.py --host claude --graph-config "/private/graph/config.json" --project "/work/example-project" --session-id "CURRENT_NATIVE_ID" --run-id "CURRENT_RUN_ID"
```

Use the freshly resolved ID from Claude skill invocation, not the literal example
or a parent's rendered ID. The check returns the profile's agent ID, task consumer,
approved selectors and current subscription IDs, without registering, subscribing,
acknowledging or saving. If registration/subscription is required, follow the existing
reviewed CLI lifecycle using those exact identities and selectors. A wrong owner,
different scope, no unique active subscription or changed brain identity requires explicit
review or a new session; no broader fallback is permitted.

For a fresh session, register and subscribe using the returned approved inputs,
then repeat the read-only check before recall. An already registered consumer
with no active subscription cannot establish its prior project scope. It refuses
automatic reuse, including after deactivation; review that coordination explicitly
or start a new native session. Do not attach a different client simply because its
directory also has an approval.

New projection configuration defaults to `Tedd Knowledge`. Existing explicit,
custom and legacy implicit `Codex Knowledge` roots remain unchanged. A live rename
is a separately reviewed projection operation, never an installer/setup side effect.

Use `scripts/knowledge_graph.py` for deterministic operations.

The command examples use `python3` and POSIX paths on macOS and Linux. On native Windows, use `py -3` or the configured Python 3.10+ `python`, Windows absolute paths, and PowerShell-native temporary-directory commands. Do not translate `chmod` literally; the runtime applies and validates the platform's private-file controls.

Defaults:

- Config: `~/.codex/knowledge-graph/config.json`
- Canonical store: `~/.codex/knowledge-graph/graph.sqlite3`
- Private capture-plan key: `~/.codex/knowledge-graph/graph.sqlite3.plan.key`
- Migration-generated backup artifacts: `<canonical-store parent>/backups/`; manual `backup` requires an explicit new `--output` path, preferably in that private directory
- Projection root: `<vault>/<configured root name>`

Keep the config and canonical store outside the Obsidian vault. The CLI rejects vault-backed state so projection rebuilds, synchronization, and human note edits cannot mutate canonical knowledge.

Run `doctor` first. On a fresh installation with no canonical store, the operator must explicitly run `init-v4`; it creates and configures a private empty SQLite-WAL store and plan key without creating sample records, revisions, mutations, consumers, or subscriptions. Never use it on an existing v3 graph: use the reviewed `upgrade-v4` workflow instead. If no vault is configured and projection is needed, ask the user for the Obsidian vault path. Canonical capture and recall can work before a vault is configured.
The vault must be owned by the current user and neither it nor its non-sticky ancestry may be group/world writable.

### Optional global preferences

Global preferences need a dedicated scope for the selected profile operator.
Each project must explicitly approve that extra subject. Setup does not create
the scope or subscribe every task to it.

1. Reuse the setup preview above with **all** of the project's approved
   `--entity-id` and `--record-id` selectors, adding `--include-preferences`.
   Setup builds a complete proposed binding from these arguments; omitting an
   existing selector can remove it from the proposal. Review the full selection.
2. If `preference_scope.status` is `capture_required`, its `entity` is a proposed
   record. Save it through the [reviewed capture workflow](capture-and-correction.md#capture-workflow)
   using the actual owner-approved source and date. Setup apply refuses until
   this record exists; it never creates graph records itself.
3. Rerun the preview after capture. A `ready` result means the expected scope
   identity and references were checked. Review the new fingerprint, then use
   the normal approved apply step. It does not prove a host connection.
4. For a fresh task, complete the existing registration/subscription procedure
   and read-only session check using the approved selection. Do not silently
   broaden an already-running task.

`--include-preferences` belongs to setup preview/apply, not the read-only
`--session-id` check. Without it, setup retains its ordinary selector behavior.
The opaque scope ID is stable for the private profile operator and contains no
name or email; it is not the person's entity. Use the preview's exact ID, never
an example or another operator's ID. The [preference schema](capture-schema.md#preferences)
reserves that scope for stated global preferences; client and project knowledge
keep their own subjects.

## Optional MCP and CLI fallback

Codex may discover the packaged local stdio `tedd-graph` MCP server when its
`python3` launcher is available. It exposes four read-only tools:

- `brain_status` returns graph health and, when requested, the state of one exact named consumer; it never returns records.
- `recall_context` returns the bounded `context_pack` for one existing exact subscription.
- `search_memory` finds matching current memories within that subscription without replacing recent context.
- `pull_changes` returns the bounded inbox for one existing exact subscription and never acknowledges delivery.

Check the connected server's tool list before using `search_memory`; the RC6
package does not include it. Its absence does not authorize installation or a
configuration change. See the
[search reference](recall.md#on-demand-mcp-search) for arguments and result limits.

`recall_context`, `search_memory` and `pull_changes` require the exact active
subscription and matching consumer, agent and task identifiers.
They cannot request a global CLI read and never infer a
consumer, project, client, graph, or working-directory scope. MCP is local
stdio only, contains no telemetry or network listener, and never registers a
consumer, creates a subscription, initializes/migrates a graph, projects, or
captures automatically. Read-only means no durable graph, config, WAL, or
logical-state writes. An existing SQLite `-shm` coordination file can change in
contents or metadata while its file set, size, and mode remain stable; do not
promise an absolute absence of filesystem writes.

The deterministic CLI remains authoritative when MCP is unavailable; use a
working supported interpreter if `python3` is unavailable. To disable the
bundled server while preserving installed skills, use the user's Codex plugin
MCP settings, not edits to the immutable installed package. In the user's
`~/.codex/config.toml` (or `$CODEX_HOME/config.toml` for a custom Codex home),
merge this setting into any existing matching table; do not duplicate the table
or replace unrelated configuration:

```toml
[plugins."tedd@tedd".mcp_servers.tedd-graph]
enabled = false
```

This affects only the bundled server. A separately configured
`[mcp_servers.tedd-graph]` entry is independent; inspect both before claiming
all Tedd MCP access is disabled. Apply configuration changes only when authorized,
then start a new task and verify the intended server list. See the official
[Codex MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Renaming `.mcp.json` demonstrates CLI independence only, not host discovery or
deactivation.
Native Windows uses `py -3` (or the configured Python) with the CLI until a
native MCP launcher is qualified. MCP and adaptive routing are optional; neither
makes subagents mandatory.

```bash
python3 scripts/knowledge_graph.py doctor
python3 scripts/knowledge_graph.py init-v4
python3 scripts/knowledge_graph.py validate --full
python3 scripts/knowledge_graph.py configure --vault "/absolute/path/to/vault" --root-name "Tedd Knowledge"
```

Skip the initialization line when `doctor` reports an existing v4 store. `init-v4` is idempotent for an already configured v4 store and can recover only an unbound, fully empty v4 store left by an interrupted first run; it refuses to adopt unbound canonical or coordination data.

Every configuration mutation uses the same local config lock. An operation that must also lock canonical state always acquires locks in `config -> store` order; do not add a path that reverses that order or writes configuration outside the shared lock.

Production Tedd agents always use the configured default store. For an isolated test or migration rehearsal only, put the global `--config` option before the subcommand and bind that sandbox configuration to its own store. Once v4 is active, commands reject ad hoc `--store` overrides so every cooperating process has one unambiguous brain:

```bash
KG_SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/codex-kg.XXXXXX")"
chmod 700 "$KG_SANDBOX"
mkdir "$KG_SANDBOX/vault"
python3 scripts/knowledge_graph.py --config "$KG_SANDBOX/config.json" configure --vault "$KG_SANDBOX/vault" --store "$KG_SANDBOX/graph.sqlite3"
python3 scripts/knowledge_graph.py --config "$KG_SANDBOX/config.json" init-v4
python3 scripts/knowledge_graph.py --config "$KG_SANDBOX/config.json" doctor
python3 scripts/knowledge_graph.py --config "$KG_SANDBOX/config.json" validate --full
```

Never guess a destructive target. Use a new or empty projection root for the first build. The projector only removes files proven to belong to the configured root by its authenticated manifest and root-bound per-file ownership marker. It rejects symlinks, unowned files in managed directories, and non-generated paths, while preserving `40 Review Actions/`. Reconfiguring to another vault or root rotates the projection identity and manifest key.

## Validation and recovery

Run `validate` after mutations and `doctor` when paths or projection behavior are unclear.

```bash
python3 scripts/knowledge_graph.py validate
python3 scripts/knowledge_graph.py validate --full
python3 scripts/knowledge_graph.py doctor
python3 scripts/knowledge_graph.py index-status
python3 scripts/knowledge_graph.py reindex
python3 scripts/knowledge_graph.py reindex --force
python3 scripts/knowledge_graph.py backup --output /absolute/private/path/graph-YYYYMMDD.sqlite3
```

Run the private, deterministic recall quality suite after changing FTS, recall
ranking, scoping, review-safe sanitization, or the canonical read boundary. It
uses the exact configured production CLI and never mutates the graph. Read and
follow [references/memorybench.md](memorybench.md) before curating or
changing cases; do not auto-accept the current ranked output as ground truth.

```bash
python3 scripts/memorybench_knowledge_graph.py \
  --config ~/.codex/knowledge-graph/config.json \
  --suite ~/.codex/knowledge-graph/memorybench/cases.json \
  --repeat 2 \
  --output ~/.codex/knowledge-graph/memorybench/reports/production-recall.json
```

Fast validation checks SQLite integrity, foreign keys, current-revision pointers, typed identity/surface registries, current FTS membership, and the journal head. `validate --full` streams every revision payload hash, mutation-chain link, semantic registry, and current-state root; use it after migration, architecture changes, bulk work, restore, or on a maintenance cadence rather than inside every packet commit. FTS and projection are rebuildable from canonical revisions. Neither is a backup.

`backup` uses SQLite's online backup API, validates one coherent standalone snapshot, and stages both output files before publishing the database followed by its digest/watermark/journal-head manifest. The matching manifest is the completion marker: choose a new destination for every backup, and treat a database without its valid matching manifest as an incomplete orphan. Portable Python cannot atomically force-replace those two pathnames, so the CLI does not offer backup overwrite. Never copy only the main database file while WAL is active. Keep pre-migration plus recent daily and weekly verified snapshots under a byte-budgeted retention policy, and periodically restore one to a new sandbox path before trusting it. A redaction report must account for older backups that may still contain the redacted revision payload.

Schema migration is explicit, side-by-side, digest-gated, and equivalence-checked. Review the deterministic v3-to-v4 plan, then apply only against the exact canonical bytes and transform that were reviewed:

```bash
python3 scripts/knowledge_graph.py upgrade-v4 --dry-run
python3 scripts/knowledge_graph.py upgrade-v4 --apply \
  --expect-source-sha256 "<source_sha256 from dry run>" \
  --expect-plan-fingerprint "<plan_fingerprint from dry run>" \
  --producer-agent-id '<configured-agent-id>' --task-id task_123 --producer-run-id run_456 \
  --based-on-watermark 42
```

The apply step briefly quiesces the v3 writer, refuses a changed digest or transform, preserves the exact v3 bytes, builds and validates a temporary standalone database, proves current-state equivalence, runs backup/restore verification, atomically installs the database, and only then switches configuration. The former v3 path becomes a refusal tombstone so a stale process cannot silently fork the brain. Before any v4 mutation, rollback may restore the exact v3 generation; after v4 writes, rollback requires a validated current-state export or an explicitly accepted loss boundary.

SQLite WAL is the supported same-machine growth architecture for hundreds of thousands to low millions of records when transactions remain packet-sized. Keep global watermarks for total audit order, per-record revisions for concurrency, exact historical payloads for feeds, and hash/root-bound audit checkpoints for verification. Do not compact merely because a count threshold was reached. Introduce immutable archive segments only after measured history size makes backup or full verification miss its service objective, and preserve chain boundaries plus transparent lookup. The moment concurrent writers move to another machine, replace direct file access with one authenticated service (normally PostgreSQL); never add a distributed lock around SQLite.
