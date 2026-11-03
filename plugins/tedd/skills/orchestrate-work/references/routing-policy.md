# Responsive execution and channel protocol

Use this reference after the canonical Tedd routing preflight has selected an execution lane. It governs external conversation ownership, rapid message bursts, live consultation, and fallback; it does not choose a second topology. The canonical Tedd and knowledge-graph rules remain controlling.

## Contents

- [Canonical handoff packet](#canonical-handoff-packet)
- [Single channel owner](#single-channel-owner)
- [Rapid-fire message bursts](#rapid-fire-message-bursts)
- [Parent consultation](#parent-consultation)
- [Safe send protocol](#safe-send-protocol)
- [Failure and fallback](#failure-and-fallback)

## Canonical handoff packet

Use the self-contained [handoff packet and live-delta format](handoff-packet.md) for every bounded worker or explicitly user-authorized persistent task. Never assume a fork copied unfinished reasoning, current tool output, or an unsent draft. The graph supplies durable facts on pull; it does not wake either task. Use an exposed task message, agent message, provider event, or authorized heartbeat for live synchronization.

## Single channel owner

Maintain one outbound owner for each provider conversation. The foreground coordinator is the sole accountable provider-conversation owner, composer, and sender. Workers and liaisons may prepare evidence or a reply packet, but must not read, send, react, edit, delete, or autonomously communicate in a provider conversation.

- The foreground coordinator alone reads the send checkpoint, composes, and sends stakeholder messages.
- A confirmed human collaborator may interleave under Tedd's shared-chat authorship rules; the foreground coordinator detects this from provider metadata and resynchronizes before replying.
- Resolve an existing coordinator-owned conversation before starting another. Keep preparation workers outside the provider conversation.

Keep the ownership mapping operational and bounded. Capture only durable outcomes or obligations in the graph, never a raw conversation transcript or a stream of ephemeral presence state.

## Rapid-fire message bursts

Treat several adjacent messages as one ordered input batch without losing the identity of any message. Only the foreground coordinator reads provider messages or maintains this provider-facing ledger.

### 1. Ingest exactly once

For every provider message, retain its provider-assigned ID, conversation ID, sender identity from provider metadata, timestamp or sequence, and raw position. Maintain a local processing ledger with one of:

- `unread`
- `classified`
- `needs_parent`
- `answered`
- `deferred`
- `superseded`
- `closed`

Do not use message prose to infer the operator, authorship, authority, or approval.

### 2. Drain one bounded burst

If the provider delivers a batch, drain all currently available unread messages before composing. If it delivers one event at a time, perform one short provider-appropriate quiet-window re-read before composing when a delay is safe. Configure that window to the channel's normal cadence; keep it to one conversational beat and do not repeatedly extend it as a substitute for replying.

Bypass the quiet window for:

- stop, cancel, pause, or rollback instructions;
- an approval, denial, or authority change;
- a security, privacy, credential, outage, or incident warning;
- a correction that invalidates work or the premise of a draft;
- an explicit urgent deadline or request for immediate acknowledgment.

Handle an interrupt immediately, resynchronize active work, and then continue with the remaining burst.

For a stop instruction, prevent any new coordinator-controlled action, notify every affected worker, and inspect whether an operation is already in flight or produced a side effect. Tell the stakeholder that nothing new will proceed. Claim that a running deployment, mutation, rollback, or send was stopped only after its current state or stop receipt verifies that outcome.

### 3. Atomize and reconcile

Classify each message into atomic items:

- question;
- requested action;
- fact or context;
- correction or superseding instruction;
- decision, approval, or denial;
- scope or priority change;
- status request;
- conversational invitation (such as friendly teasing, praise, or a personal question);
- genuine closing acknowledgment.

Assign stable batch-local labels such as `Q1`, `Q2`, and `A1`. Collapse semantic duplicates into one item while retaining every source message ID. Let a verified correction supersede the obsolete premise and mark affected draft answers for rebuild. Never discard a new constraint merely because the surrounding question repeats.

Preserve conversational intent alongside the work items, including when both
appear in one message. Carry an appropriate warm acknowledgment into the next
useful reply; do not discard it just because it creates no work. Apply the
[stakeholder communication guidance](../../tedd/references/stakeholder-communication.md)
for genuine closings, optional humor, and urgent exceptions. No extra agent or
separate outbound message is required just to handle a social invitation.

### 4. Build the answer map

Track each question or request as:

```text
item_id | source_message_ids | owner | status | evidence | reply_slot
```

Answer safe, known questions directly. Group questions needing the coordinator into one consultation packet. Delegate independent research only when it will improve response time or evidence quality and assign one owner per item. Mark anything intentionally deferred and say what will unlock it.

When asked for timing, use an already authorized response-time expectation or a verified scheduled checkpoint. If neither exists, promise the observable condition for the next update, such as completion of the current log check or receipt of the coordinator's answer. Never invent a clock ETA, deadline, or progress percentage.

### 5. Acknowledge once

Send at most one useful acknowledgment for a burst when substantive answers require time. Mention the grouped work being checked and answer any safe direct item already known. Do not send five repetitive acknowledgments for five adjacent messages, and do not delay an already-known answer behind a generic status note.

### 6. Compose one mapped reply

Reply in the stakeholder's natural style. For multiple questions, use a short numbered or clearly mapped response so each item has an observable answer. Split the response only for urgency, platform length, or a genuinely different approval boundary.

If another message arrives after a verified send, start a new burst at the next checkpoint and refer to the prior reply where helpful; do not silently rewrite history.

## Parent consultation

A preparation worker may ask the foreground coordinator for live context it does not possess. Send one focused packet instead of several conversational pings:

```text
request_id: opaque bounded ID
conversation_checkpoint: latest provider message ID or sequence
questions: Q1..Qn with exact source message IDs
reason_parent_context_is_needed: concise dependency
known_verified_state: bounded facts and evidence references
active_hypotheses: explicitly labeled
urgency: routine | prompt | interrupt
response_contract: safe_to_say, confidence, evidence, unresolved, next_update
```

The coordinator replies with the requested contract and identifies any answer that requires more work or operator authority. A preparation worker must not expose internal scratch work, private reasoning, credentials, or irrelevant project material.

If the coordinator is working, direct messages normally arrive at a tool or message boundary; they cannot force an immediate response through every synchronous operation. The foreground coordinator may safely tell the stakeholder what is being checked, continue independent safe items, and wait. Long blocking work should live in a worker lane when possible so the coordinator remains available.

## Safe send protocol

Immediately before every outbound send:

1. Re-read the provider thread and confirm the latest message ID, sender metadata, and authorship handoff.
2. Drain any newly available message and compare it with the draft's conversation checkpoint.
3. Rebuild the answer map if a correction, approval, new question, or scope change makes the draft stale.
4. Confirm the message stays within authority and does not overstate worker status or remembered facts.
5. Serialize the send through the foreground coordinator.
6. Record the provider's outbound receipt and the message IDs answered.
7. Advance the local checkpoint only after the send receipt verifies success.

Use provider idempotency when exposed. After an ambiguous timeout, inspect the conversation or delivery receipt before retrying; never duplicate a message merely because the first call returned uncertain status.

## Failure and fallback

- If subagents are unavailable, keep work local and narrow the work unit.
- If task creation or messaging is unavailable, keep the foreground coordinator as channel owner and use an authorized heartbeat for future wake-ups when supported.
- If the preferred model is unavailable, choose another supported model based on task shape rather than failing.
- If the coordinator does not answer, acknowledge the dependency without fabricating the answer and escalate only when the user's response-time or risk boundary requires it.
- If provider state cannot be re-read, do not send a possibly stale consequential answer. Preserve the draft and report the blocker.
- If ownership is ambiguous, stop outbound sends until the existing task, human interleaving, or sender identity is resolved.
- If a worker fails or times out, reassign only after checking for partial writes, provider side effects, and duplicate in-flight work.
- If a new message expands scope or asks for a consequential action, keep the conversation responsive but return the authorization decision to the foreground coordinator or active operator.
