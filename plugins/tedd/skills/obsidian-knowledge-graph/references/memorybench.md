# Tedd MemoryBench

MemoryBench checks retrieval through Tedd's real read interfaces. Existing v1
suites measure CLI `recall`: relevance-ranked FTS followed by subject filtering.
Opt-in v2 suites can measure MCP `search_memory` or the overdue and preference
sections delivered by `recall_context`. MCP search applies scope before limits.
It returns records that contain every query term, with exact IDs first and then
recent changes. When none exists, it returns labeled partial matches ordered by
how many distinct query terms each record contains.
Scores for one path do not describe another. Keep their reports separate.

Run these commands from the installed `obsidian-knowledge-graph` skill directory
with Python 3.10–3.14. On Windows use the qualified `py -3 -B` or absolute Python
interpreter in place of `python3 -B`, native paths, and a single PowerShell line
instead of Bash continuations.

The runner creates no graph or reader, changes no subscription, acknowledges
nothing, and never writes canonical state.

Keep the production suite private at:

```text
~/.codex/knowledge-graph/memorybench/cases.json
```

The file must be owned by the current user and inaccessible to group and other
users. Store only short queries, canonical IDs, scalar assertions, and hard
negative IDs. Do not store transcripts, payload snapshots, credentials, raw
tool output, or secrets. Reports contain case IDs, ordered record IDs, metrics,
latencies, and gate messages only; returned payload bodies are discarded.

On POSIX, set mode `0600` as shown below. On Windows, do not translate
`chmod`; the runtime validates the protected user-scoped ACL instead.

Run an authorized suite against its selected graph configuration. The example
keeps detailed results in a new private report beside the suite; stdout contains
the aggregate summary:

```bash
chmod 600 ~/.codex/knowledge-graph/memorybench/cases.json
python3 -B scripts/memorybench_knowledge_graph.py \
  --config ~/.codex/knowledge-graph/config.json \
  --suite ~/.codex/knowledge-graph/memorybench/cases.json \
  --repeat 2 \
  --output ~/.codex/knowledge-graph/memorybench/reports/baseline.json
```

Exit code `0` means every hard gate and configured aggregate threshold passed.
Exit code `1` means a stable-snapshot retrieval, ranking, scope, or privacy gate
failed. Exit code `2` means the suite was invalid, a read failed, or the run
could not make a stable comparison. Changes to store identity, generation,
watermark, and, for v2, reader state or implementation make the result
inconclusive. Resolve the reported cause before rerunning.

## Suite contract

The bounded v1 format is:

```json
{
  "schema_version": "tedd-memorybench-v1",
  "suite_id": "tedd-production-recall-v1",
  "expected_store_id": "immutable-store-uuid",
  "expected_generation": 1,
  "defaults": {
    "mode": "review_safe",
    "limit": 5,
    "max_chars": 12000
  },
  "thresholds": {
    "min_macro_precision_at_k": 0.0,
    "min_macro_recall_at_k": 0.0,
    "min_macro_mrr": 0.0
  },
  "cases": [
    {
      "id": "current_deployment",
      "query": "verified deployment state",
      "subject_ids": ["entity_project"],
      "collections": ["claims", "claim_surfaces"],
      "relevant_record_ids": ["claim_current"],
      "relevant_surface_ids": ["surface_deployment_state"],
      "required": [
        {
          "record_id": "claim_current",
          "max_rank": 3,
          "collection": "claims",
          "payload_equals": {
            "lifecycle": "current",
            "active": true
          }
        }
      ],
      "ranking_pairs": [
        {
          "higher_record_id": "claim_current",
          "lower_record_id": "claim_superseded"
        }
      ],
      "forbidden_reference_ids": ["entity_other_client"],
      "forbidden_namespaces": ["other_client"]
    }
  ]
}
```

Every `required` item names exactly one `record_id` or `surface_id`; the latter
is satisfied by either the surface record or a claim whose `surface_ref`
matches. `payload_equals` accepts scalar review-safe fields only. Historical or
superseded results are not automatically failures: assert their lifecycle when
important, require current truth within a bounded rank, and use
`ranking_pairs` only where current truth must precede known history.

## MCP search suites

Use `schema_version: tedd-memorybench-v2` and an explicit `read_path`. A
`mcp_search_memory` case supplies the existing exact reader that the model would
use. It cannot add `subject_ids` or `collections` to change that subscription:

```json
{
  "schema_version": "tedd-memorybench-v2",
  "suite_id": "private-scoped-search",
  "read_path": "mcp_search_memory",
  "expected_store_id": "immutable-store-uuid",
  "expected_generation": 1,
  "defaults": {"limit": 5, "max_chars": 12000},
  "cases": [
    {
      "id": "approved_palette",
      "query": "palette approv*",
      "session": {
        "consumer_id": "reader_alpha",
        "agent_id": "agent_alpha",
        "task_id": "task_alpha",
        "subscription_id": "subscription_alpha"
      },
      "required": [{"record_id": "decision_palette", "max_rank": 3}],
      "forbidden_record_ids": ["decision_other_project"]
    }
  ]
}
```

The IDs above are placeholders. Replace the store identity and reader fields
with the verified existing graph, consumer, agent, task, and active subscription.
Use independently reviewed expected record IDs; the example does not register
or approve any reader.

A case can add `match` with `auto`, `all` or `any`. Without it, the request
omits the argument and the connected tool applies its own default, as it would
for the model. Set `match: all` for a case that depends on the every-term rule.
A build whose `search_memory` tool does not list `match` refuses the argument,
which makes the run invalid; omit it for that build.

Use literal search words appropriate to MCP. A natural-language question that
works with CLI recall's OR matching has no every-term match when one word is
missing. Under `match: all` it returns nothing; under the default it returns
partial matches. Scores for the two modes describe different behavior, so
record which one a suite used. Compare the same information need and
independently judged records, not unexamined query strings. Retain the query
and scope in the private suite; do not derive expected results from whichever
records the current implementation returns.

Run the same command shown above with the v2 suite path. The runner initializes
the actual stdio MCP endpoint, checks its tool list, and calls the selected tool.
A refused identity, missing tool, timeout or malformed protocol response makes
the run invalid, not a successful empty retrieval. A legitimate no-match case
can use `max_result_count: 0`; add `match: all` when its query has more than one
term, because the default can return partial matches. It tests retrieval, not
whether a model abstains.

## Delivered context suites

Use `read_path: mcp_recall_context` for D2 and D3. Each case supplies `session`
and `context_section: overdue` or `preferences`, plus its usual required targets
and hard negatives. There is no query. Set `max_items` to bound recent records
in the request and `max_chars` for the entire disclosed context. Omit `limit`
from the case and defaults; it would not control these sections.

A complete context suite can name a preference's stable surface. Replace the
example identities and expected surface with verified values. This suite has
no `limit` in either its defaults or case:

```json
{
  "schema_version": "tedd-memorybench-v2",
  "suite_id": "private-preference-context",
  "read_path": "mcp_recall_context",
  "expected_store_id": "immutable-store-uuid",
  "expected_generation": 1,
  "defaults": {"max_chars": 12000},
  "cases": [
    {
      "id": "current_reporting_preference",
      "session": {
        "consumer_id": "reader_alpha",
        "agent_id": "agent_alpha",
        "task_id": "task_alpha",
        "subscription_id": "subscription_alpha"
      },
      "context_section": "preferences",
      "max_items": 1,
      "required": [
        {
          "surface_id": "surface_reporting_style",
          "max_rank": 1,
          "payload_equals": {"effective_conflict": false}
        }
      ]
    }
  ]
}
```

Assertions inspect the compact fields actually delivered, not full payloads
reread from storage. Precision uses the section's fixed capacity: five overdue
items or twelve preferences, recorded as `precision_k`. MRR describes position
in that section, not a relevance score. The complete response is checked for
budget overflow, and hard negatives are checked across records and both
sections. Budget-omitted context fails a case rather than masquerading as an
empty memory. Reports retain omission/truncation flags but discard payloads.

## Stable comparisons

For before/after comparisons, use a dated private snapshot made through the
supported [backup workflow](setup-and-recovery.md#validation-and-recovery), not
a filesystem copy of an open SQLite-WAL database. Restore it to a separate
private local location. Never replace the normal graph configuration or turn
that copy into another production writer.

Prepare approved exact readers and subscriptions in the disposable copy before
measurement. The benchmark itself cannot register them. An already authorized
reader can also be used without registration, but a changing live graph is not
a fixed comparison dataset. Keep source versions, suite judgments, reader
scope, budgets and queries explicit.

V2 reports identify the read path, implementation digest, suite digest and
canonical identity/watermark. They also fingerprint reader state. A changed
reader, cursor, implementation or canonical checkpoint makes the run
inconclusive. Preparation writes are outside the measured read-only interval.
If reader status omits subscriptions because its list is too large, measurement
is invalid: an incomplete list cannot establish that every cursor stayed put.
V1 suites and report behavior remain available unchanged. This is separate from
the frozen public synthetic retrieval protocol; no provider was added to it.

## Interpreting scores

Precision@k divides labeled relevant results by the configured case limit, or
the fixed section capacity for delivered context.
Every `required` target is automatically included in the relevant target set;
additional manually curated record and surface targets may extend that set.
Recall and MRR use the resulting verified targets. Unjudged returned records
are reported explicitly, so precision is a conservative lower bound rather
than a claim that every unjudged result is irrelevant. Keep semantic
thresholds informational at first and raise them only after reviewing a stable
baseline. Hard scope, privacy, current-state, and required-result gates remain
strict from the first run.

An output report, when requested, must live under the suite's sibling
`reports/` directory. `--force` may replace an earlier report there, but it
cannot target the suite, graph configuration, canonical store, tool source, or
an arbitrary owned file. With `--output`, stdout contains only the aggregate
summary; case and record identifiers stay in the private `0600` report. Without
`--output`, the caller is explicitly choosing to receive the detailed report on
stdout and must protect that sink.

## Curation rules

- Bind a production suite to the immutable `expected_store_id`; optionally bind
  its generation. Never pin a global basis watermark in the suite.
- Curate expected IDs from verified exact records and graph relationships, not
  by automatically accepting the current ranked output.
- A `ranking_pairs` higher target must also be a required record, preventing a
  missing preferred result from passing the ordering gate vacuously.
- Prefer stable entity, claim-surface, decision, receipt, and loop IDs. Update a
  case deliberately when a legitimate successor claim changes the expected ID.
- Include exact identity, alias/name, current-versus-history, scoped ambiguous
  names, cross-client hard negatives, and review-safe privacy cases.
- Add a case after a real miss or regression. Do not grow the suite with
  redundant queries merely to increase its count.
- A benchmark failure is evidence for investigation, not mutation authority.
  Verify the cause before changing retrieval, identities, or canonical facts.

The private task tally measures observed work outcomes separately. Use consistent
project names, count completed meaningful work rather than monitor check-ins,
and record an evidenced hit or miss when one was assessed. If it was not
assessed, say so in the task note. An absent outcome entry establishes neither
success nor failure. Do not invent historical rows,
silently rewrite past entries, or infer effectiveness from sparse logging.
Repeated capture failures need their exact errors investigated; passing search
tests cannot repair knowledge that was never saved, and privacy checks remain
in force.

The v4 path has no scan-recall comparator; `--no-index` is a legacy flag and is
not a valid FTS parity test. Subject filtering currently happens after the
initial top-k FTS candidate cutoff in CLI recall; this does not describe MCP
search. Scoped misses should remain visible as
benchmark failures rather than being hidden by the evaluator.
