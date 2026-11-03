# AgentBigBrain adaptations

This skill independently reimplements a small subset of architectural ideas reviewed in AgentBigBrain at commit `265d4acffd1292b0bbe1bbb6f505c116852c09c7`.

## Current v4 architecture

- A private local SQLite database in WAL mode is the single canonical mutation authority.
- Canonical entities have immutable opaque IDs, governed identity keys, aliases, and evidence-backed redirects. Claim surfaces have their own immutable identities.
- Every record change creates an immutable revision and a hash-chained mutation with a global watermark and producer/task/run provenance.
- Per-record compare-and-swap protects relevant concurrent work without making unrelated client activity invalidate a reviewed plan.
- Database uniqueness constraints enforce one canonical owner for identity keys and claim-surface signatures.
- Versioned task consumers, exact-scope subscriptions, authenticated inbox cursors, acknowledgements, and bounded context packs provide multi-agent awareness without loading the whole brain into every task.
- Schema migrations, transactional backups, WAL checkpoints, hash/root-bound dry-run-gated audit checkpoints, store generations, and full validation provide explicit recovery and evolution boundaries.
- FTS5 recall is transactionally current inside the canonical database. Exact opaque-ID lookup, aliases, redirects, and bounded relationship expansion remain explainable retrieval paths.
- Obsidian is an incremental, authenticated, review-safe projection performed outside canonical write transactions. It is never a round-trip backup or a second truth store.
- Evidence is not instruction, permission, approval, safety authority, or completion proof.
- Opt-in source preservation keeps immutable workflow originals outside the graph.
  Exact scoped links support explicit bounded evidence checks; ordinary recall
  does not open the archive or receive extra source bodies. See
  [source-backed memory](../../tedd/references/source-backed-memory.md).

## Adapted patterns

- Three layers: bounded source evidence, canonical typed records, and derived review surfaces.
- Distinct observations, temporal claims, events, decisions, receipts, and open loops.
- Stable identities, source fingerprints, idempotent writes, temporal lifecycle state, and explicit observation lineage.
- Caller-controlled authority ceilings so captured text cannot promote itself or replace stronger truth.
- Deterministic, collision-checked Obsidian paths, authenticated manifests, per-file ownership markers, and conservative deletion rules.
- Review-safe output by default, including credential, private-path, URI, active-Markdown, sensitive-edge, and source-lineage sanitization.
- Explicit operational separation between canonical knowledge and consumer/subscription state.

## Deliberately excluded

- The AgentBigBrain Node runtime, package lock, native inference stack, federation, daemon, pulse, browser, and autonomy subsystems.
- Automatic capture of every turn, unrelated activity, or raw environment/browser/shell state. The narrow private-archive exception preserves only qualified direct sources used in an explicitly enabled workflow, not transcript dumps in the graph.
- Vector embeddings as a canonical identity or duplicate-prevention mechanism.
- Markdown or Obsidian as a writable source of truth.
- Automatic execution of Obsidian-authored review actions.
- Recursive deletion of projection directories.
- Distributed access to the SQLite file. Multi-machine writers require a reviewed authenticated transactional service rather than shared storage or distributed file locking.
- Automatic journal truncation. Audit checkpoints exist, but archival or pruning requires a separately designed retention protocol that preserves history, deduplication, and consumer boundaries.

## Growth posture

The dated v4 reference snapshot contained 328 current canonical records and 38 prior journal mutations. Because records and mutations are different units, the exact 1,000-times current-record planning target is 328,000 records. The synthetic suite uses 366,000 records plus a 10% revision population as a deliberately conservative qualification tier and 1,000,000 records as a stretch tier. A private migration qualification passed at 366,000 records, 36,600 revisions, and 32 concurrent writers; that result is evidence for its machine and corpus shape, not a universal capacity promise. The benchmark suite also exercises exact identity contention, unrelated and relevant concurrent writes, replay safety, history, backup, migration, projection, and recovery behavior.

SQLite WAL is intentionally the same-machine coordination boundary. Keep the database on a private local filesystem. Readers may run concurrently, while packet-sized write transactions serialize through SQLite. When measured p95/p99 write latency, database size, maintenance windows, or multi-machine requirements exceed that boundary, migrate behind a single authenticated service with equivalent idempotency, record-revision compare-and-swap, mutation ordering, subscriptions, backups, and audit retention. Do not solve that transition by sharing the WAL database.

Obsidian remains a bounded review cockpit at scale. Projection uses a fixed bounded current-state hot set rather than rendering every historical revision. Exact record history, exports, and audit feeds stay available through deterministic commands without overloading agent context or the vault.

## Upstream references

- Repository: https://github.com/AgentBigBrain/AgentBigBrain
- Graph contracts: `src/core/profileMemoryRuntime/profileMemoryGraphContracts.ts`
- Source Recall contract: `docs/SOURCE_RECALL.md`
- Projection boundary: `src/core/projections/README.md`
- Obsidian sink: `src/core/projections/targets/obsidianVaultSink.ts`
- License: Apache License 2.0 with NOTICE.

No upstream source file is copied into this skill. If future work copies code or documentation rather than reimplementing an idea, preserve the upstream license and NOTICE obligations and mark modifications.
