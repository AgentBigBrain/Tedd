# Knowledge graph scale and limits

These results show what one synthetic benchmark completed on one Windows host, not a universal limit. Record count, revision history, mutation count, data shape, storage, hardware, and the number of writers can each change performance.

For the broader retrieval and agent-evaluation lineage and dated aggregate results, see [Memory research](MEMORY_RESEARCH.md).

## Tested and untested sizes

The archived study tested 366,000 synthetic current records plus 36,600 earlier revisions to see
how the local graph behaved under a heavy workload. This is not bundled Tedd data, a recommended
graph size, or a promise about another machine. The benchmark code also offers an optional
one-million-record run, but no public result currently qualifies that size.

## Verified and available tiers

| Tier | Purpose | Evidence |
| --- | --- | --- |
| 10,000 and 100,000 records | Intermediate synthetic tiers | Passed in the 2026-08-06 repeatable benchmark on one Windows host |
| 366,000 records / 36,600 revisions | Largest reported synthetic tier | Passed in the 2026-08-06 benchmark on one Windows host |
| 1,000,000 records | Optional unreported test | Available in the benchmark tool; no public pass is claimed |

The reported pass rows come from one archived synthetic benchmark with one execution and one
writer per tier.
Earlier one-shot qualification attempts did not produce usable tier results and are excluded from
the table. This repository contains only synthetic benchmark code and non-sensitive aggregate
evidence; it does not contain a private reference graph or its records.

## Archived study and current benchmark

The published 10k, 100k, and 366k measurements came from the archived repeatable scale study at
commit `da53a1b1eab11c74e65e6ac057fd927ebbf75a43`. Its archived protocol and results, retained in the maintainers' private research
archive, describe the exact workload and evidence. Reproducing that study requires
access to that archive and its `scripts/memory_scale_bench.py` runner.

The command below runs the current benchmark tool. It is useful for measuring the current machine,
but its code, test data, and procedure differ from the archived study. It will not reproduce the
archived measurements exactly.

## When the local database is no longer enough

SQLite's write-ahead logging mode (WAL) supports several Tedd processes on one computer. Keep write
transactions small, and measure read and write latency, backup and restore time, full validation,
database size, and Obsidian-view generation on the actual machine. The 95th-percentile latency
(`p95`) is the time that 95% of measured operations meet or beat; `p99` is the same measure for 99%.

Move the database behind one authenticated service or API that owns all transactions when either
condition becomes true:

- writers must operate from another machine; or
- measured latency, recovery, validation, storage, or retention no longer meets the project's
  recorded limits after profiling.

Never put the SQLite database or private profile on a network share or synchronized folder, and never solve the boundary with a distributed file lock.

## Running the synthetic benchmark

From the installed `skills/obsidian-knowledge-graph` directory, or from `plugins/tedd/skills/obsidian-knowledge-graph` in a repository checkout, the default command uses a disposable temporary store and does not touch the configured graph:

```bash
python3 scripts/benchmark_knowledge_graph_v4.py
```

On Windows, use `py -3 scripts\benchmark_knowledge_graph_v4.py`; the benchmark applies the same protected local file permissions as the runtime while keeping all data disposable.

Larger targets and multi-writer runs are optional:

```bash
python3 scripts/benchmark_knowledge_graph_v4.py --full-targets --writers 8,16,32
```

Record the machine, Python and SQLite versions, target population, revisions, writer count, database size, and latency percentiles with any result. A pass on one corpus and machine is evidence for that run, not a blanket production guarantee.
