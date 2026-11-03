# Performance and scale

## Measure before changing code

- Start with a reproducible workload, latency or memory objective, and representative data size.
- Profile the actual boundary. Do not infer a critical bottleneck from a local expression such as a comprehension, `dict.get`, `str.join`, or set lookup.
- Preserve correctness, readability, compatibility, and bounded resource use unless measurement justifies a tradeoff.
- Report environment, Python version, dataset, repetitions, variance, and before/after results.

## Bound concurrency and materialization

- A semaphore bounds concurrent bodies but does not bound the number of created coroutine objects or tasks.
- For large or attacker-controlled input, use a bounded queue and fixed worker pool, chunking, or backpressure at the producer.
- Track cancellation and retrieve task exceptions. Use structured concurrency APIs only when compatible with the declared Python floor or behind a compatibility boundary.
- Avoid retaining full result sets, histories, or projection payloads when a streaming or bounded hot-set design satisfies the contract.

## Choose data structures contextually

- Use sets for repeated membership checks when ordering, duplicates, hashability, and memory costs permit.
- Use generators when single-pass laziness helps; use lists when reuse, indexing, stable snapshots, or simpler failure behavior matters.
- Prefer comprehensions when they remain clear. A loop is often better when control flow, diagnostics, or side effects are important.
- Avoid replacing explicit code with a micro-optimization that has no measured effect at Tedd's scale.

## Scale SQLite deliberately

- Keep write transactions short and indexes aligned with measured query plans.
- Bound result pages and context payloads; avoid long-lived read snapshots that retain WAL growth.
- Benchmark backup, restore, validation, projection, and recall against service objectives, not record-count folklore.
- Move to a service database when writers cross machine boundaries; do not attempt to repair distributed access with a file lock.
