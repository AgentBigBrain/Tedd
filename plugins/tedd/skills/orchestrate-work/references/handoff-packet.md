# Canonical handoff packet and live deltas

Use this template when the core routing preflight has selected a bounded worker or an explicitly user-authorized persistent task. It transfers only task-relevant facts and authority; it is not a transcript, graph export, or access grant to private reasoning.

## Handoff packet template

- objective:
- verified state:
- hypotheses:
- permissions:
- owned artifacts:
- relevant user instructions or messages:
- task-specific constraints:
- required durable records:
- current watermark and scope identifiers:
- output contract:
- stop condition:
- leaf boundary:

State only bounded, review-safe values. Label hypotheses separately from verified state. Provide a bounded summary or authorized task-relevant text inline. A reference is allowed only to an artifact or resource the packet explicitly grants the receiver access to; a bare message reference to an origin task is not usable. Never copy a transcript. The coordinator retains approvals, external commitments, and final synthesis unless the packet explicitly grants a narrower action.

## Live-delta format

Send a delta when verified state, scope, authority, ownership, watermark, or stop condition changes after the handoff.

```text
delta kind: verified_state | scope | authority | ownership | watermark | stop_condition
current watermark and scope identifiers: bounded references
verified change: concise evidence-backed fact
hypothesis update: labeled or none
status: investigating | implementing | verifying | blocked | complete
changed artifacts: bounded names or none
blockers or needs: bounded dependency or none
next action: bounded next step or none
```

Do not send raw transcripts, credentials, private reasoning, or unrelated durable graph content. If the needed context is missing, ask the owner a focused question and mark the uncertainty.
