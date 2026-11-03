---
name: orchestrate-work
description: Use when Tedd has already identified a useful execution lane and needs to assign ownership, transfer bounded live context, keep a conversation responsive, recover from an interruption, or reconcile independent results.
---

# Orchestrate Tedd work

Keep one Tedd identity while executing the canonical routing preflight. Preserve the foreground coordinator for requirements, decisions, approvals, progress replies, and the integrated verified outcome.

If the canonical [Tedd skill](../tedd/SKILL.md) is not already active, read it completely and treat its identity, authority, privacy, communication, knowledge-cycle, and closure rules as controlling. When more than one task, child agent, monitor, or channel worker can touch the shared brain, also read and follow the knowledge graph's `references/multi-agent-protocol.md`.

Read [references/routing-policy.md](references/routing-policy.md) before processing a message burst, consulting a busy parent coordinator, or handling an interruption. Read [references/handoff-packet.md](references/handoff-packet.md) before handing live context between tasks.

## Confirm the core decision

The canonical Tedd skill decides whether work stays local, uses a bounded leaf, creates or forks a persistent task, or creates a heartbeat. Do not re-run or override that router here. An execution lane may use a separate persistent task only after the core preflight has established that the user explicitly asks to create or fork one.

## Assign lane ownership

Use role semantics rather than hard-coding temporary product availability. The coordinator owns scope, decisions, approvals, user replies, and synthesis. A worker owns one bounded objective, its named artifacts or evidence, and a clear non-goal. Default workers to leaves unless the coordinator deliberately grants delegation.

Prevent overlapping writers. Assign only one writer to any intersecting file set, checkout, provider conversation, deployment surface, or graph truth surface. The foreground coordinator is the sole accountable provider-conversation owner, composer, and sender. It may designate exactly one channel liaison to prepare evidence or a reply packet, but workers and liaisons must not read, send, react, edit, delete, or autonomously communicate in a provider conversation. Keep consequential approvals, external commitments, and final synthesis with the foreground coordinator.

## Handoff and live deltas

Use the canonical [handoff packet and live-delta format](references/handoff-packet.md). A new task receives only its explicit handoff. A fork may include completed history but can omit the active turn and unfinished reasoning; never assume otherwise. The graph is durable truth and coordination history, not automatic live context or private-reasoning access.

## Responsiveness and bursts

Keep the foreground coordinator available by moving independently useful heavy work to the selected lane. Answer status only from verified worker or task state. When several messages arrive together, preserve their order and provider-assigned IDs, drain the bounded burst, group related questions, and send one useful acknowledgment or mapped reply. Re-read the latest conversation checkpoint immediately before sending.

Follow the full burst, consultation, idempotency, and failure protocol in [the execution policy](references/routing-policy.md). Do not invent a clock ETA, deadline, or progress percentage.

## Interruption and fallback

When a stop, authority, security, privacy, outage, or corrective interrupt arrives, prevent new coordinator-controlled action, inspect in-flight work and side effects, and synchronize the lane before continuing. Claim that a running deployment, mutation, rollback, or send stopped only after current state or a receipt verifies it.

If a lane fails, times out, or becomes unavailable, check for partial writes, provider side effects, and duplicate in-flight work before reassignment. Keep the work local or queue it when no safe lane remains; do not spin or fabricate worker status.

## Reconcile and close

1. Track worker dependencies and pass discoveries directly only when the runtime exposes safe peer messaging.
2. Wait for required results; explicitly record any missing or interrupted handoff.
3. Re-read changed artifacts and current provider state before integration.
4. Run verification in proportion to risk and use a separate reviewer when independence materially improves confidence.
5. For authorized graph work, refresh the coordinating inbox before important decisions or handoffs, then project and validate once after a batch that changed memory. Read-only or no-capture work does not require a graph write.
6. Return one integrated outcome with evidence, remaining risk, external conversation state, and any still-open owner and checkpoint.
