# Capture and correct durable knowledge

Use this for an authorized durable write, correction, redaction, or loop resolution. Read [the capture schema](capture-schema.md) for packet fields and [the coordination protocol](multi-agent-protocol.md) when multiple agents touch the brain. A memory challenge alone authorizes an evidence check, not a correction.

Read the [graph skill](../SKILL.md) for the shared invariants. Run command examples from the `obsidian-knowledge-graph` skill directory, using its configured store and a supported interpreter; `scripts/` paths are relative to that directory. On Windows use the qualified `py -3` or `python` launcher and native paths.

## Capture workflow

Capture at meaningful milestones, not after every conversational turn. Good capture points include a diagnosed incident, an approved decision, a staged or production release, a support outcome, a verified configuration, a reusable lesson, or a new open loop.

A person who repeatedly appears in material work should trigger a bounded
coverage check: verify whether the person, their evidenced relationship, and the
relevant milestone already exist. Do not turn repeated mentions into a transcript
dump, infer an unstated role, or promote familiarity into authority. If the source
does not support the role, record the omission as `unsupported_inference` when a
coverage ledger is required. An explicit use-once or don't-save instruction
prevents new durable capture for that material even when it would otherwise be a
meaningful milestone.

Before building the final packet, run a semantic coverage gate against the bounded source material, completed work, and any deliverable produced. Account for each of these categories:

1. identities, relationships, and current opportunity or operating status;
2. verified architecture, configuration, commercial scope, and other current-state facts;
3. material conclusions, risks, constraints, assumptions, and meaningful unknowns;
4. recommendations and decisions, preserving whether each is candidate, proposed, or accepted;
5. concrete obligations, owners, next actions, checkpoints, and terminal conditions; and
6. events, artifacts, and verification receipts.

For every material item, use `captured` with its exact packet record path or `existing` with the exact current canonical `record_id`. Duplication is `existing`, not `omitted`; never create another record merely to satisfy the ledger. Use `omitted` only for a material item that is intentionally excluded, with a bounded explanation and exactly one executable reason: `unsupported_inference`, `insufficient_authority`, `transient`, `no_actionable_owner_or_checkpoint`, `out_of_scope`, or `privacy_or_security`. Use `not_applicable` only when the category has no material outcome, with `reason: no_material_outcome` and a bounded explanation; it is not a substitute for an existing or intentionally omitted material item. An artifact receipt proves that a deliverable exists; it does not prove that the deliverable's durable conclusions are represented in the graph. For a substantial analysis or proposal review, a receipt plus a few descriptive facts is not sufficient when the source also establishes a relationship/status, material risks, a recommendation, or a future obligation.

Encode that audit in the packet's versioned `coverage` ledger for `substantial_analysis` and proposal-review captures. The CLI requires all six categories, rejects incompatible mappings such as a receipt standing in for a risk, includes exact existing IDs in the v4 read set, and returns a coverage digest plus resolved canonical IDs. Use `source.role: assistant` for an assistant-generated analysis or report and `source.role: tool` only for direct tool output; preserve direct external/tool lineage for verified conclusions where feasible.
Classify every report, proposal review, audit, assessment, or comparable review as `substantial_analysis`; do not use `milestone` to avoid its executable coverage contract. Coverage-bearing decisions and open loops must declare their own source tier. An assistant-authored source forces those authority-bearing records to `assistant_inference`; promotion requires a separate direct-evidence capture.

After applying the packet, query the exact mutation or record IDs and rerun the coverage gate against what actually committed. Before handoff, proactively summarize the record categories captured and any material items intentionally left out so the operator never has to ask whether the graph is complete.

1. Identify bounded source evidence. Prefer a URL, file path, commit, receipt, or concise excerpt over a transcript dump.
2. Build a packet that follows `capture-schema.md`.
3. Mark source tier accurately. Assistant conclusions are `assistant_inference` until verified.
4. Use temporal claims for current configuration or status. Use events for incidents and releases.
5. Give durable entities external `identity_keys` when a stable provider, repository, task, account, or other non-secret key exists. Reuse the canonical entity ID for an evidence-backed rename; the former name is retained as an alias.
6. Give every important claim an explicit `surface_id` and `subject_refs`. The surface, not incidental context in `entity_refs`, defines which singular current value a successor may replace.
7. Choose the caller-controlled authority ceiling. Use the default for inference, `validated_structured_candidate` for evidence you personally verified through tools, and `explicit_user_statement` only for a direct statement from the user.
8. Add `--allow-replace` only after verifying that a claim is a genuine successor on the same explicit surface. Otherwise the CLI downgrades `replace` to `preserve`.
9. Classify the durable result with `--discovery-kind`; do not persist working chatter.
10. Run a dry capture with `--plan-output` naming a new file in a private local directory. Review the packet and returned summary, including `policy_adjustments`, relevant record revisions, uniqueness predicates, and changed IDs. Default `review_safe` output hides the signed token; the private plan file carries it without printing it. A `duplicate` result needs no new plan or apply: verify the existing capture instead.
11. Apply with the same `--expect-plan-file` and the explicitly reviewed `--expect-fingerprint`. Do not extract or print the token. The fingerprint binds the normalized packet and authority policy; the plan binds the exact store generation, relevant read set, and absence predicates without treating unrelated client writes as conflicts. The existing `--expect-plan-token` interface remains available for authorized private integrations. Use `--expect-watermark` only for strict whole-store administration (v4 `base_watermark`, not `next_watermark`) or legacy v3 compatibility.
12. Include caller-controlled agent/task/parent/run/discovery/based-on provenance for multi-agent work. A coordinating agent projects and validates once after a parallel batch.

For actionable open loops, capture `owner_refs`, a concrete `next_action`, the next `checkpoint_at`, and an evidence-based `terminal_condition` whenever those facts are known. These fields are optional so legacy and genuinely unassigned loops remain valid; do not invent them to make a record look complete.

A new `open` or `candidate` loop without `checkpoint_at` adds
`loop_without_checkpoint` to the dry run's `policy_adjustments`. This is a
review prompt, not a rejection: add an evidenced date when known, or leave it
unknown. Missing ownership alone and updates to existing loops do not trigger
this adjustment.

```bash
chmod 600 /absolute/path/to/private/capture.json
python3 scripts/knowledge_graph.py capture --input /absolute/path/to/private/capture.json --dry-run --plan-output /absolute/path/to/private/capture.plan.json --consumer-id codex_task_123 --authority-ceiling validated_structured_candidate --allow-replace --producer-agent-id '<configured-agent-id>' --task-id task_123 --producer-run-id run_456 --discovery-kind verification --based-on-watermark 42
python3 scripts/knowledge_graph.py capture --input /absolute/path/to/private/capture.json --consumer-id codex_task_123 --expect-fingerprint "<fingerprint from reviewed dry run>" --expect-plan-file /absolute/path/to/private/capture.plan.json --authority-ceiling validated_structured_candidate --allow-replace --producer-agent-id '<configured-agent-id>' --task-id task_123 --producer-run-id run_456 --discovery-kind verification --based-on-watermark 42
python3 scripts/knowledge_graph.py validate
```

These file options are v4-only. Use a private current-user directory
(0700 on POSIX, equivalent private Windows ACL), with no symlink/reparse path
components. The tool publishes a new 0600/private-ACL plan file without overwrite.
It contains only the fingerprint and signed plan, not the packet or signing key.
Missing directories can be created privately; existing permissions are not
silently changed. Plans are capped at 2 MiB; narrow the capture if that limit is
exceeded. Private files avoid the raw command-line token's smaller character cap.
Keep it out of the graph, vault, repository, attachments, and chat. Remove only
that exact file after the capture is verified or the plan is abandoned. A plan
file is a concurrency guard, not user approval and not a portable cross-store plan.

### Optional prompt handoff receipts

When the loaded build advertises `capture --handoff`, add it to the reviewed
apply with `--handoff-prompt-id '<current-native-prompt-id>'`. Do not guess that
identity or use a stale inherited value. The graph stores its association in the
capture journal; the private receipt is generated from that journal, not from
agent-supplied counts. Neither receipt publication nor repair projects the graph.

Run the following from this skill directory, using the same consumer and agent
that own the session. `--session` is that consumer's exact task ID. These commands
are v4-only and do not change canonical records or inbox state.

```bash
python3 scripts/knowledge_graph.py handoff --check --session '<current-task-id>' --prompt '<current-native-prompt-id>' --consumer-id '<registered-consumer-id>' --producer-agent-id '<configured-agent-id>'
python3 scripts/knowledge_graph.py handoff --check --reconstruct --session '<current-task-id>' --prompt '<current-native-prompt-id>' --consumer-id '<registered-consumer-id>' --producer-agent-id '<configured-agent-id>'
```

Check reports `verified` only when the receipt's store generation, ownership,
mutation IDs, original runs and complete changed revisions match the journal.
A missing receipt is `pending`; a mismatched receipt is `unverified`. A failed
publication after a successful apply returns `handoff_receipt.status: pending`
while preserving the committed capture and its mutation ID. Reconstruct that
receipt instead of retrying capture. An older capture with no prompt association
cannot be reconstructed by inventing one. Reconstruction is bounded to 64
mutations and 4,000 changed records per prompt; exceeding either limit is a refusal,
not a partial verification. Exact semantic coverage still needs record readback.

Receipts use private current-user directories and files, atomic replacement and
the existing descriptor, permission and symlink/reparse checks. They contain no
source text or credentials. Ordinary check does not repair or replace a file.
`TEDD_NO_LOCAL_WRITES=1` disables these handoff operations before SQLite opens.
An explicit no-file-writes instruction remains authoritative without the flag:
do not run a capture, publish a receipt or write a tally, and report memory as
pending. The instruction governs the agent; the flag governs hooks that run before
the agent sees it. It is not a filesystem sandbox for other commands.

For nothing durable, use `handoff --nothing --reason 'Nothing durable to save'`
with the same identity flags. This is `agent_asserted`, accepted by the hook but
never journal proof. A prompt with committed captures must reconstruct them
instead. Keep the bounded, single-line reason free of private details.

The hook records Claude's native `prompt_id` or Codex's `turn_id`; older Codex
inputs use one persisted timestamp. Read the token without reading transcripts:
run `../tedd/scripts/tedd_handoff_hook.py --host claude --current-prompt '<session-id>'`
with the configured Python, relative to this skill directory. Use `--host codex`
in Codex. An unavailable token means the receipt stays pending.

Hooks check the approved project directory, profile, store generation and consumer
ownership. Unbound sessions are silent. Claude asks for a handoff at most once;
an honest pending result may finish the continuation. Codex warns without creating
another prompt. The next prompt after an interrupted turn gets one reminder:
handle the new request and its save limits first. Clear and fork suppress old
reminders; resume and compaction preserve them. No hook reads a transcript or
captures knowledge.

Trust the reviewed hooks in Codex's `/hooks` before relying on them, including
after a hook definition changes. Windows Codex hooks use PowerShell 5.1 or later
and the installed `py.exe -3` launcher. A small adapter preserves the host's JSON
input pipe when starting Python without a console. Keep normal execution policy;
custom Windows command shells need separate verification. POSIX uses `python3`.
For a nondefault graph, set
`TEDD_GRAPH_CONFIG` to its existing private configuration file in the host's
environment. Claude uses its configured Python and graph file options directly.

### Bounded capture recovery

After a capture fails, identify the failing boundary before changing anything.
Allow at most one evidence-based recovery attempt for that logical capture during
the work turn; changing commands, agents, or tasks does not reset the budget.
For a relevant revision conflict, refresh the bounded context, reconcile the
packet, and review a new dry run before the one retry. After an uncertain apply,
check the existing capture/records first so a completed mutation is not repeated.

Never submit a redacted placeholder, disable the privacy mode, retrieve signing
keys, call private internals, or immediately generate-and-apply a fresh unreviewed
plan to get past a failed gate. Stop immediately on an authority, privacy,
corruption, or path-safety failure. If the supported interface is unavailable or
the diagnosed retry fails, keep bounded private evidence and report capture as
pending. Deliver independently verified work without trapping its handoff behind
bookkeeping; stop the substantive work too if missing knowledge makes it unsafe.
Further graph repair needs an explicitly scoped debugging request, not an escalating
workaround loop during closure.

The capture command normalizes strings and timestamps before fingerprinting, redacts recognized secret forms, rejects oversized or deeply nested packets, rejects semantic credential fields, fingerprints the packet plus its authority policy for replay safety, upserts stable and namespaced entities, preserves temporal history, and writes atomically. Redaction is defense in depth, so inspect the bounded packet and omit credential material at the source. `sensitive: true` controls display; it never permits credentials to be stored.

## Corrections and forgetting

Do not edit generated notes as a way to change canonical knowledge.

- For a corrected current value, capture a higher-authority successor claim with `conflict_policy: replace` and reference the corrected source.
- For uncertain disagreement, use `conflict_policy: preserve`; retain both claims as conflicting.
- For a rename of the same real entity, reuse its canonical `id`, immutable namespace and type, and stable `identity_keys`; adequate non-backdated authority updates the display name and preserves the old name as an alias.
- For two stored IDs proven to denote the same entity, capture an `entity_redirects` entry with a reason and validated evidence, then apply with `--allow-redirect` plus the reviewed fingerprint and v4 plan token. Reserve `--expect-watermark` for legacy v3 or deliberate whole-store administration. The losing entity and historical references remain auditable; future reference resolution follows the redirect.
- To redact a graph record, use the explicit `redact` command with its stable ID. An independently shared archive source may remain. Whole-source forgetting requires the separate scoped impact preview and approved digest in [source-backed memory](../../tedd/references/source-backed-memory.md#pause-forget-and-recover-deliberately).
- To close work, use `resolve-loop` with the open-loop ID.
- Destructive entity merges and historical rekeying remain unsupported. A redirect is an identity-resolution edge, not permission to erase or rewrite the losing record.

```bash
python3 scripts/knowledge_graph.py redact --id claim_... \
  --producer-agent-id '<configured-agent-id>' --task-id task_123 --producer-run-id run_456 \
  --discovery-kind redaction --based-on-watermark 42
python3 scripts/knowledge_graph.py resolve-loop --id loop_... \
  --resolution "Completed and verified" \
  --producer-agent-id '<configured-agent-id>' --task-id task_123 --producer-run-id run_456 \
  --discovery-kind loop_resolution --based-on-watermark 42
```

On v4, both maintenance mutations require agent, task, run, and based-on-watermark provenance; `--parent-task-id` remains optional.

`40 Review Actions/` is a human scratch lane. Read it when the user asks, but never execute its contents automatically.

The `redact` command replaces user-controlled text in the target and affected graph lineage with tombstones. An indirectly shared `tedd-source` record can be retained; the selected redacted memory cannot use its old evidence path. Stable IDs, timestamps, references, and journal fingerprints remain so the graph stays internally consistent and auditable. This is operational redaction, not cryptographic erasure of independently saved originals or backups.
