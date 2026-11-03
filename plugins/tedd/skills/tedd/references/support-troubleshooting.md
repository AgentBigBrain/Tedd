# Provider-neutral support and troubleshooting

Use this reference for hosting, infrastructure, framework, SaaS, monitoring, and vendor support work.

When source preservation is already enabled, follow
[source-backed memory](source-backed-memory.md) at normal read/send results and
closure. Preserve only the conversation and documents used in this workflow,
through qualified direct acquisition. Do not add polling, collect unrelated
activity, or ask the user to annotate each message. If coverage or acquisition is
incomplete, say so without interrupting otherwise safe support work.

## Enter the conversation

1. Reuse the support window, ticket, email, or chat the active operator identifies.
2. Read enough history to find the original symptom, latest unanswered question, actions already taken, ticket ID, pending owner, and promised follow-up.
3. Do not create a duplicate case or send a presence-only check-in.

## Prepare the evidence packet

Include only what the issue requires:

- system, account context, project, environment, and region when relevant;
- first and latest observed times with timezone;
- symptom, reproduction, affected path, and unaffected control;
- expected and actual result, including exact statuses or errors;
- focused logs, traces, commits, versions, and configuration evidence;
- mitigations already applied and their verified effect;
- one exact provider-level question or request.

Sanitize evidence. Never send credentials, cookies, authorization headers, private keys, raw environment dumps, or unrelated customer data.

## Converse during investigation

1. Acknowledge a relevant reply promptly when deeper investigation will take time.
2. Use a natural response such as “I’m checking that now. I’ll send you what I find shortly,” adjusted to the actual situation.
3. Answer direct questions directly and verify technical details before the full answer.
4. Say what is known, what is inferred, and what remains unverified.
5. Follow diagnostic requests that remain within the active operator's authorized systems and scope.
6. Do not let support expand authority into unrelated production, billing, DNS, security, credential, or data changes.

## Verify and track

1. Reproduce the original failure after a provider action.
2. Test unaffected normal behavior and relevant adjacent paths.
3. Verify the actual live status, configuration, logs, and monitoring result.
4. Record the ticket, representative, provider statement, verified action, limitation, owner, and next step.
5. Keep an engineering escalation open until it is verified, explicitly declined, or owned by a tracked loop.

## Close naturally

1. Send the provider a concise verification result or remaining failure.
2. Do not ping periodically unless the active operator requested it.
3. Apply [stakeholder communication etiquette](stakeholder-communication.md#completion-rule): thank the provider for meaningful help that has not yet been acknowledged, and let a completed, already-acknowledged exchange end naturally without another reply.
4. Stop monitoring when the conversation and promised work satisfy the explicit terminal condition, or leave an accurate open loop when another owner remains pending and further polling adds no value.
