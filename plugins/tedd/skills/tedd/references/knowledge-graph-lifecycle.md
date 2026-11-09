# Task-scoped knowledge-graph lifecycle

The graph supplies durable context and an audit trail, not live authority.

Use this lifecycle for relevant prior knowledge, requested memory work, or an
authorized durable milestone. Self-contained answers need no setup or capture.
Preserve read-only/no-capture boundaries and all mutation gates.

## Intake

1. Read the [graph skill's essential invariants](../../obsidian-knowledge-graph/SKILL.md) and select only the operating reference needed for the task.
2. Run its `doctor` command.
3. For shared-brain work, inspect and resume the exact existing task/run consumer and subscriptions. Register a new consumer only when required and authorized, using verified identity and scope. Use the configured default store; never create a per-agent production store.
4. With that scope available, pull the consumer inbox to a complete authenticated boundary, then request a bounded context pack. Do not load unrelated clients, raw evidence bodies, or global history into the task merely because they exist.
5. Recall specific names for the client, site, repository, environment, provider, incident, stakeholders, and task when the exact scope needs expansion.
6. Review current claims, decisions, receipts, active loops, candidate loops, resolved history, identity ambiguity, conflicts, and evidence separately. Check overdue items for relevance using the intake rule below.
7. Follow no more than one relationship hop unless the task requires broader research.
8. Evaluate the conditional Graph Steward triggers in `../../obsidian-knowledge-graph/references/graph-steward.md` and run the bounded read-only review when one is present.
9. Reverify current state when it may have changed.

If recall includes non-empty `overdue.items`, report one intake line, such as
"3 overdue items for this project". For a truncated list, say "at least 3";
do not imply an exact total. Continue unrelated work without a disposition
question. For an item relevant to the task, present the recorded status and
evidence, then ask the user to choose resolved, rescheduled, handed off,
withdrawn, or blocked. Propose a choice when evidence supports it, but do not
apply one automatically. Candidates remain proposals. An empty omitted or
truncated section means information is missing, not that nothing is overdue;
use the [bounded review command](../../obsidian-knowledge-graph/references/recall.md#review-overdue-items)
within the same scope when that information matters.

An empty recall is valid. Never invent history to fill it. Reuse verified intake
until a relevant change or decision boundary requires refresh.

## Optional scoped MCP reads

Use `brain_status` for health or an exact consumer. The connected `tedd-graph`
tools `recall_context`, `pull_changes` and `search_memory` require the exact
existing subscription, consumer, agent and task. They read only: no registration,
acknowledgement, migration, capture or projection. They infer neither scope nor
graph from the directory. Use the CLI for `doctor`, broader recall and unavailable
MCP routes. Keep work local unless another lane materially helps.

Use `search_memory` to supplement recent context when an older decision or
detail matters. Reconcile the inbox independently. Follow the
[scoped search procedure](../../obsidian-knowledge-graph/references/recall.md#on-demand-mcp-search)
for literal queries, labeled partial matches, one deliberate prefix broadening
when endings explain a miss, bounds, empty results and authority interpretation.
Treat a partial match as a lead: it does not establish the unmatched words.

## During work

Before finishing work that used memory, select only durable outcomes: a decision
marked accepted or proposed, attributed stakeholder approval, verified outcome, reusable fix, concrete
obligation with a known date or owner, or corrected fact. Exclude chat, routine
edits, repeated tests, and no-save, read-only or use-once content.

Batch compatible outcomes into the fewest milestone packets preserving scope,
authority ceilings and size limits. Under standing authorization, review and apply
without asking again. Preserve uncertain identity/evidence in existing candidate
lanes only where the capture contract permits it; never bypass a refusal.

For an event with `kind: reusable_fix`, use five nonempty fields in order:
`Symptom: … Cause: … Fix: … Verified by: … Applies when: …`.

After a pending-handoff reminder, handle the new request first. Recheck changed
scope, honor new no-save/read-only boundaries, and never capture just to clear
the reminder. Fresh fork/clear identities cannot reuse a parent's consumer.

Use concise graph excerpts or stable references. Never put raw chat transcripts,
browser state, credentials, private keys, tokens, cookies, authorization headers,
or unbounded logs in the graph. The opt-in exception for preserving direct workflow
sources belongs only in the separate private archive described in
[source-backed memory](source-backed-memory.md). Follow that reference for a
disputed memory; checking the answer must not silently change durable records.

Before an important decision, current-claim replacement, identity resolution, loop resolution, production action based on memory, or stakeholder handoff, pull the scoped inbox and refresh the context plan again. Apply high-impact captures with the reviewed fingerprint and per-record revision/read-set plan. If a relevant revision changed, reconcile and rerun the dry capture; never blindly retry. An unrelated client's watermark advance is not a conflict.

Concurrent mutations include agent, task, concrete run, discovery kind, actual based-on watermark and any parent task. These identify provenance; the verified source and caller-controlled ceiling establish evidence authority.

## Loop rules

- Create an active loop only from validated evidence or explicit user instruction.
- Keep assistant-derived suggestions in the candidate lane.
- Store known owners, next actions, checkpoints, and terminal conditions in `owner_refs`, `next_action`, `checkpoint_at`, and `terminal_condition`. Use ISO 8601 for an evidenced checkpoint. Preserve genuinely unknown values without inventing an owner or date; ask only when the requested follow-up depends on the missing information. An evidenced obligation with missing ownership remains distinct from an assistant-proposed candidate. Keep known operating state in structured fields rather than burying it in the summary.
- At recall, follow the overdue intake rule above. A reviewed user disposition
  governs resolution, rescheduling, withdrawal, or handoff; evidence alone does
  not authorize those changes. Never silently roll a checkpoint forward.
- Resolve the exact loop only when a receipt, verified provider action, tested deployment, or explicit outcome proves completion.
- Write the resolution as what completed and how it was verified.
- Never reopen a resolved loop. Create a new episode if the same concern recurs.
- Do not fabricate resolved loops for completed historical events that were never tracked as obligations. Keep those as events and receipts.

## Closure

Run capture, loop updates, and projection only for the applicable authorized
memory work. A read-only recall does not require a closing write. Validate after
mutations; project after successful capture or correction when a vault is configured.

Follow bounded capture recovery. If it fails, keep private bounded evidence and
report pending memory separately from verified deliverables. Do not hold a safe
handoff hostage to graph debugging or claim pending work is complete. Missing
knowledge still blocks actions that need it for safety.

1. Map material outcomes to canonical records or explicit omission reasons. Substantial analyses and proposal reviews require the packet's v1 coverage ledger and verified returned digest/IDs, including identity/status, state, conclusions/risks, decisions, obligations and artifacts.
2. Treat an artifact receipt only as proof that the artifact exists. Do not let it stand in for the artifact's durable conclusions, relationship status, risks, decisions, or obligations.
3. Use `--handoff` with the current hook prompt ID; check journal proof. Repair with `handoff --check --reconstruct`, not recapture. `handoff --nothing --reason` is an agent assertion, not proof. Read-only permits asserted receipts; no-files forbids receipts/tally. Without a receipt, report it pending. `TEDD_NO_LOCAL_WRITES=1` also silences pre-agent hooks. Follow the [receipt procedure](../../obsidian-knowledge-graph/references/capture-and-correction.md#optional-prompt-handoff-receipts).
4. Resolve completed loops and leave remaining loops accurate.
5. Query the committed mutation or exact record IDs, then rerun the semantic coverage ledger against what actually reached the graph. Repair any unexplained material omission before handoff.
6. Pull the scoped inbox once more before the final handoff and acknowledge only a complete delivery that was actually reconciled.
7. For parallel work, have the coordinating task project once with `review_safe`; child agents do not each rebuild the vault.
8. Run fast `validate` after mutations and `doctor` when health or path state needs rechecking; run full validation after migration, bulk work, restore, identity maintenance, or graph-architecture changes.
9. Complete any Graph Steward review triggered by a bulk backfill or graph-architecture change and capture only the verified audit receipt.
10. End with `Memory: saved N (ids), candidates M (ids), not saved: <reason or none>`. Count verified outcomes, not source/auxiliary IDs; distinguish candidates. Nothing durable is a valid reason. A receipt proves journal membership, not semantic coverage; pending memory must stay explicit.
