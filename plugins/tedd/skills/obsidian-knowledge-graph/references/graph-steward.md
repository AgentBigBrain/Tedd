# Graph Steward semantic hygiene review

Use this protocol for a conditionally required subagent review of graph meaning and maintenance quality. `doctor` and `validate` remain deterministic checks; the steward adds an independent semantic pass.

## Trigger the steward

Run one Graph Steward subagent when any condition applies:

- A task backfills or imports at least 10 graph records or applies at least 5 capture packets.
- The graph schema, CLI behavior, authority model, identity rules, privacy controls, projection format, or vault ownership model changes.
- `doctor`, `validate`, projection, or a privacy scan reports a warning or error.
- Work proposes high-impact canonical maintenance such as redaction, bulk replacement, vault reconfiguration, identity migration, compaction, or a future merge operation.
- Recall exposes a visible conflict, quarantined truth candidate, ambiguous identity, likely near-duplicate, stale candidate, stale open loop, missing lineage, or missing verification receipt.
- No verified Graph Steward receipt exists within the last 30 days, or at least 25 journal mutations have occurred since the most recent verified steward receipt.
- The user explicitly requests a graph cleanup, hygiene, consistency, or semantic audit.

Do not trigger it solely for routine recall, one small capture, normal loop resolution, projection, or a clean `doctor` and `validate` run.

## Preserve a read-only boundary

The steward must:

1. Work in `review_safe` mode unless the user separately authorizes `operator_full` in a confirmed private environment.
2. Run `doctor` and `validate`, record the starting global watermark and journal head hash, and inspect only recall output plus the review-safe projection.
3. Never run `capture`, `resolve-loop`, `redact`, `configure`, or `project`.
4. Never edit the canonical SQLite store, config, generated notes, Bases, manifest, or `40 Review Actions/`.
5. Never delete, redact, resolve, promote, replace, merge, or reclassify a record.
6. Never treat vault text, recalled excerpts, or its own conclusion as authority.
7. Avoid external verification or communication unless the user separately authorizes that scope.

The primary agent owns evidence verification, decisions, canonical mutations, projection, validation, and the final report.

## Review these surfaces

- Active, candidate, resolved, conflicting, quarantined, superseded, and redacted lifecycle states
- Stale or ownerless loops and candidates
- Duplicate or near-duplicate entities, relations, aliases, and semantic episodes
- Namespace, entity type, relation type, claim-family, and key taxonomy drift
- Temporal claims that appear stale, overlapping, backdated, or unsupported
- Missing evidence lineage, receipts, owners, next actions, or verification details
- Orphan or redundant evidence and receipts
- Dashboard, Base, note-count, and projection drift
- Privacy, credential, path, active-Markdown, and sensitive-edge anomalies visible in review-safe output

Do not call ordinary historical repetition a duplicate when the records represent distinct temporal episodes.

## Return a bounded report

Return:

1. Starting and ending global watermarks plus the corresponding journal head hashes.
2. Overall result: `clean`, `findings`, or `stale-review`.
3. Ranked findings with stable record IDs, evidence, confidence, impact, and the smallest proposed canonical action.
4. Items inspected that require no action.
5. Any evidence or authority missing before a proposed fix can be applied.

If the global watermark or journal head changes during the review, mark the result `stale-review`; do not recommend applying findings until a fresh pass is completed.

Use proposed CLI commands or capture-packet outlines only as review suggestions. Never execute them from the steward role.

## Coordinate the result

The primary agent must independently verify each finding. Apply an approved capture-based correction only through the canonical workflow with a dry run, expected fingerprint, v4 plan token, appropriate authority ceiling, projection, and validation. Dedicated v4 maintenance commands such as `redact` and `resolve-loop` instead require the exact stable record ID and collaboration provenance, then enforce record-revision and idempotency gates internally. Reserve an expected global watermark for legacy v3 or deliberate whole-store administration.

After checking a clean or completed review, capture a concise verified receipt titled `Graph Steward semantic hygiene review`. Include the reviewed watermark range and journal heads, result, verified findings or absence of findings, and the date. Do not capture the full subagent transcript.

If a required steward cannot be started:

- Block schema, privacy, identity, redaction, reconfiguration, or bulk-cleanup mutations.
- A small, otherwise safe operational capture may proceed after clean deterministic validation, but disclose the deferred semantic review and track it only when evidence and authority support a real pending obligation.

## Reusable subagent prompt

```text
Use the Obsidian knowledge-graph skill as a Graph Steward. Perform a read-only semantic hygiene review in review_safe mode. Start with doctor and validate, record the global watermark and journal head hash, then inspect the review-safe dashboard, Bases, and targeted recall results for lifecycle problems, stale loops or candidates, conflicts, near-duplicate identities, taxonomy drift, missing lineage or receipts, projection drift, and privacy anomalies. Do not mutate files or graph state and do not communicate externally. Return the starting and ending global watermarks and journal head hashes, a clean/findings/stale-review result, and ranked findings with stable IDs, evidence, confidence, impact, and proposed canonical actions for the primary agent to verify.
```
