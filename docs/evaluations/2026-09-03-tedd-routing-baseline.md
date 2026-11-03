# Tedd routing baseline

- Date: 2026-09-03
- Plugin version: 1.0.0
- Evaluator schema: `tedd-routing-observations-v1`

## Authorized observation

The authorized evaluation exercised `parallel_read_only_investigations` with a
mid-run `long_work_with_status_question`. The selected canonical route was
`bounded_subagent` (a bounded read-only investigation). The coordinator answered
the status question from verified worker state before work began. The first
status was observed within the available 30-second measurement window; exact
timestamps were unavailable. Total observed completion time was 212,021 ms.

No contradiction or unverifiable completion claim was observed. The coordinator
reported no side effects, but no independent storage digest was captured, so
that statement is not storage-level proof. This was one existing-task
observation rather than a fresh-task installation measurement; it does not
establish comparative latency or routing superiority.

## Future scoring

Use only sanitized scenario IDs, routes, and boolean safety results in an
observation file. Capability-aware scenarios record bounded-lane availability;
the fresh-context, peer-dependency, and interrupted-worker scenarios also
record their required completion evidence. Peer-dependency observations record
whether peer messaging was available and whether delivery was direct; otherwise
the coordinator carries the update. Do not include prompts, conversation text,
graph data, identifiers, secrets, or local paths.

```bash
python3 scripts/tedd_routing_eval.py catalog --pretty
python3 scripts/tedd_routing_eval.py score --observations observations.json --pretty
```

## Packaging qualification (disposable state only)

The copied-installed-tree qualification initialized one local stdio server,
listed tools, and called `brain_status`, `recall_context`, and `pull_changes`
against a synthetic v4 graph with one synthetic change and one exact synthetic
consumer/subscription. Tool-call count was three. The subprocess completed
within the test's 10-second bound; exact per-tool timings were not recorded, so
no latency comparison or improvement is claimed. The five observed response
lines (initialize, tools/list, then the three tools) were 261, 1,825, 1,005,
1,401, and 2,805 bytes: 7,297 newline-delimited bytes in aggregate. Results
remain bounded by the protocol response limit.

The before/after disposable-store digest plus logical graph state, consumer
state, and subscription identity matched. This proves no durable graph/config/
WAL/logical-state write for that fixture; it does not promise that SQLite cannot
update content or metadata in an already-existing `-shm` coordination file
while file set, size, and mode remain stable. Removing the copied MCP config
still left CLI `doctor` and `recall` runnable; that is CLI-only evidence, not
host discovery evidence. The packaging test uses `python3` on this macOS/Linux
target; native Windows uses the CLI fallback pending a native launcher
qualification. Fallback frequency was zero in the normal copied-MCP run and
one expected CLI fallback in the isolated missing-`python3` launcher case.

The cached 1.0.0 Tedd plugin visible to this running task is not this reviewed
worktree. No explicitly authorized fresh task was available to prove new host
MCP or skill discovery, so **new task required** is recorded for that platform
qualification. The existing routing observation above remains the only
authorized task-level route observation. No prompts, task URIs, client graph
content, identifiers, secrets, or local paths were recorded.
