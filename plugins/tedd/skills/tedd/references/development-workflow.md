# Development and delivery workflow

Use this reference for code, configuration, infrastructure, and releases. Apply
the canonical [Tedd execution and precedence rules](../SKILL.md): an approved
implementation includes in-scope debugging and verification, not an automatic
handoff after each step. Use only the phases the user requested.

## Orient

1. Reuse the existing plan, acceptance checks, authorized workspace, and approvals. Separate code still to implement from tests awaiting a host capability.
2. Inspect the relevant local instructions, repository status, recent changes, executable configuration, and verification entry points. Preserve an already suitable branch or isolated checkout.
3. Record a production or infrastructure baseline and rollback point only when live changes are in scope.
4. Preserve every unrelated change.

## Diagnose

1. Reproduce the issue safely.
2. Compare the failure with an unaffected control.
3. Trace the boundaries implicated by the evidence; do not investigate unrelated layers merely to complete a checklist.
4. Prefer primary logs, provider status, HTTP evidence, commits, configuration, and deterministic tests over assumptions.
5. Separate the root cause from secondary load, noisy warnings, and monitoring artifacts.

## Change

1. Use the authorized task branch, preview, change set, or lower environment.
2. Make the smallest cohesive change that addresses the verified cause or requested behavior. Fix nearby duplication when it affects this workflow; defer unrelated redesign.
3. Keep generated artifacts synchronized only when the repository's normal workflow requires them.
4. Run focused checks while developing. Repair in-scope failures without another approval request. Use the required release and quality gates on the stable final candidate; rerun only checks invalidated by a later change. Preserve existing acceptance gates.
5. Review the exact change before commit or apply.
6. If a push or upload was authorized and performed, confirm the remote artifact matches the tested source. Local completion does not imply permission to publish.

For an already inspected checkout and authorized disposable test environment,
continue tests and repairs without repeating the trust preflight. Recheck when
executable setup, dependencies, target environment, or authority materially
changes. Missing software is a capability gap: use an approved suitable helper,
or request the specific installation decision while finishing independent work.

## Stage and approve

Apply this phase only when staging or release preparation is in scope.

1. Deploy the exact candidate to staging or preview.
2. Test the changed path, adjacent path, error handling, navigation, responsive behavior, logs, monitoring, and unaffected controls as applicable.
3. Record defects found during staging and the commits or configuration changes that fixed them.
4. Send exact review links and test evidence. Ask for approval only at a release boundary that requires a decision not already granted for this candidate and scope. An informational review update is not an automatic stop.
5. Do not interpret a casual acknowledgment as approval for an unsent or unverified deliverable.

## Promote and verify

Promotion requires the user's applicable release authority; neither a test pass
nor this reference supplies it.

1. Confirm the approved artifact is still the candidate tip.
2. Promote without rebuilding from a dirty or different source.
3. Verify live behavior independently with the same critical tests.
4. Confirm provider state, monitoring, logs, and rollback readiness.
5. Stop further promotion if production differs materially from staging. Use the authorized rollback, or hand off the specific recovery decision while preserving the rollback point.

## Record

Report the verified outcome and any exact remaining gap. Capture only applicable,
authorized durable milestones through the [knowledge lifecycle](knowledge-graph-lifecycle.md).
Do not invent staging, production, or memory activity to fill a report template.
