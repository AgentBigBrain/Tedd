# Project recall baseline: Tedd 1.2.0-rc.13

This is one run of the [project recall protocol](PROTOCOL.md) on 2026-11-10. It
records what Tedd's reads deliver from large synthetic projects before any
change that the protocol's comparison rules are meant to judge.

## What was run

| Field | Value |
| --- | --- |
| Protocol | `tedd-project-recall-protocol-v1` |
| Scenario catalog | `tedd-project-recall-scenarios-v1` |
| Catalog SHA-256 | `f38329b084ad0d284d3c7050b775f68c2efb7268d2e28f7dd320bf334680f276` |
| Evaluator SHA-256 | `c746bdfc4042eff4057a507d7af7baced0313735bb5b0bd254008d8e55113128` |
| Memory evaluation lab evaluator SHA-256 | `fe160c35164bd523d0fcc506116de0247e03fc7e8d328d4f18cf911bfe262eb1` |
| Source | Commit `da7b201a0ed00a2231c286a0e65190802ada740a`, Tedd 1.2.0-rc.13, with this lab's files not yet committed |
| Host | Windows 11 on AMD64, CPython 3.14.0, SQLite 3.50.4 with FTS5 |
| Synthetic graph | 2,087 records at watermark 2,081 |
| Lab qualification | Passed all nine checks |

The two evaluator digests are computed from file contents, so they identify
the exact instrument and graph runtime whatever the Git state.

## Results by arm

"Oversized cases" are the five whose required records lie outside what the
default budget can hold. "Controls" are the two that recency order should pass.

| Arm | Budget | Oversized cases sufficient | Controls sufficient | Mean delivered recall | Mean response characters | Mean response evaluator tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `no_memory_v1` | `standard` | 0 of 5 | 0 of 2 | 0.00 | 0 | 0 |
| `no_memory_v1` | `default` | 0 of 5 | 0 of 2 | 0.00 | 0 | 0 |
| `context_response_v1` | `standard` | 0 of 5 | 1 of 2 | 0.14 | 10,994 | 2,795 |
| `context_response_v1` | `default` | 1 of 5 | 2 of 2 | 0.43 | 45,875 | 11,353 |
| `recall_fts_v1` | `standard` | 2 of 5 | 1 of 2 | 0.60 | 1,768 | 465 |
| `recall_fts_v1` | `default` | 4 of 5 | 1 of 2 | 0.88 | 16,529 | 4,048 |
| `mcp_search_v1` | `standard` | 1 of 5 | 1 of 2 | 0.40 | 10,035 | 2,536 |
| `mcp_search_v1` | `default` | 3 of 5 | 1 of 2 | 0.74 | 35,074 | 8,772 |
| `mcp_context_search_v1` | `standard` | 1 of 5 | 2 of 2 | 0.48 | 20,995 | 5,316 |
| `mcp_context_search_v1` | `default` | 2 of 5 | 2 of 2 | 0.62 | 55,876 | 13,874 |
| `oracle_v1` | `standard` | 5 of 5 | 2 of 2 | 1.00 | 1,111 | 309 |
| `oracle_v1` | `default` | 5 of 5 | 2 of 2 | 1.00 | 1,111 | 309 |

No arm returned a hard violation or a record outside its project. Every arm
delivered exactly the same thing across its three measured repetitions, and no
response exceeded its character allowance. One read named a stale record:
`mcp_search_v1` at `default` returned the replaced decision in
`replaced_decision` together with the current one.

## What a context returned

`context_response_v1` is one CLI `context` call, read in full. "Delivered" is
the share of required records that arrived with their full payload or with
every needed field.

| Case | Records in project | Budget | Records returned | Section items | Sections withheld | Delivered | Sufficient |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| `old_decision` | 300 | `standard` | 10 | 0 | 0 | 0.00 | No |
| `decisions_over_time` | 650 | `standard` | 9 | 0 | 0 | 0.00 | No |
| `replaced_decision` | 300 | `standard` | 9 | 0 | 0 | 0.00 | No |
| `open_commitment` | 250 | `standard` | 10 | 0 | 1 | 0.00 | No |
| `old_note` | 250 | `standard` | 10 | 0 | 0 | 0.00 | No |
| `recent_work` | 300 | `standard` | 10 | 0 | 0 | 1.00 | Yes |
| `small_project` | 30 | `standard` | 9 | 0 | 0 | 0.00 | No |
| `old_decision` | 300 | `default` | 44 | 0 | 0 | 0.00 | No |
| `decisions_over_time` | 650 | `default` | 43 | 0 | 0 | 0.00 | No |
| `replaced_decision` | 300 | `default` | 44 | 0 | 0 | 0.00 | No |
| `open_commitment` | 250 | `default` | 44 | 1 | 0 | 1.00 | Yes |
| `old_note` | 250 | `default` | 45 | 0 | 0 | 0.00 | No |
| `recent_work` | 300 | `default` | 45 | 0 | 0 | 1.00 | Yes |
| `small_project` | 30 | `default` | 31 | 0 | 0 | 1.00 | Yes |

The record counts include the project's own entity record.

## What the search routes returned

Each cell gives delivered recall and whether the read was sufficient. All
three arms search with the case's question as written.

| Case | Budget | `recall_fts_v1` | `mcp_search_v1` | `mcp_context_search_v1` |
| --- | --- | --- | --- | --- |
| `old_decision` | `standard` | 1.00, yes | 1.00, yes | 1.00, yes |
| `decisions_over_time` | `standard` | 0.67, no | 0.33, no | 0.33, no |
| `replaced_decision` | `standard` | 0.00, no | 0.00, no | 0.00, no |
| `open_commitment` | `standard` | 1.00, yes | 0.00, no | 0.00, no |
| `old_note` | `standard` | 0.00, no | 0.00, no | 0.00, no |
| `recent_work` | `standard` | 0.50, no | 0.50, no | 1.00, yes |
| `small_project` | `standard` | 1.00, yes | 1.00, yes | 1.00, yes |
| `old_decision` | `default` | 1.00, yes | 1.00, yes | 1.00, yes |
| `decisions_over_time` | `default` | 0.67, no | 0.67, no | 0.33, no |
| `replaced_decision` | `default` | 1.00, yes | 1.00, yes | 0.00, no |
| `open_commitment` | `default` | 1.00, yes | 0.00, no | 1.00, yes |
| `old_note` | `default` | 1.00, yes | 1.00, yes | 0.00, no |
| `recent_work` | `default` | 0.50, no | 0.50, no | 1.00, yes |
| `small_project` | `default` | 1.00, yes | 1.00, yes | 1.00, yes |

`mcp_search_v1` searches with the budget as its limit, 12 or 100 results.
`mcp_context_search_v1` searches with the search tool's default of 10 results
at both budgets, after a context at the budget.

## Size against the whole project

For `context_response_v1` at the `default` budget:

| Case | Records | Whole-project payload characters | Whole-project evaluator tokens | Returned payload characters | Share returned |
| --- | ---: | ---: | ---: | ---: | ---: |
| `old_decision` | 300 | 272,767 | 65,025 | 39,236 | 14.4% |
| `decisions_over_time` | 650 | 602,185 | 141,133 | 39,460 | 6.6% |
| `replaced_decision` | 300 | 274,040 | 64,616 | 40,121 | 14.6% |
| `open_commitment` | 250 | 227,511 | 53,894 | 39,666 | 17.4% |
| `old_note` | 250 | 225,770 | 54,218 | 39,725 | 17.6% |
| `recent_work` | 300 | 271,914 | 65,012 | 39,929 | 14.7% |
| `small_project` | 30 | 27,212 | 6,441 | 27,537 | 101.2% |

`small_project` exceeds 100% because the returned records include the
project's entity record, which the whole-project reference does not count.

## What the run shows

- **A default context covers a small part of a large project.** It returned 43
  to 45 records from projects of 250 to 650, which is 6.6% to 17.6% of their
  payload characters.
- **A context alone delivered the required records in one of five oversized
  cases.** No decision or note older than the returned records was delivered
  or named, at either budget. `old_decision` is a critical case and was not
  delivered.
- **That one delivery came from a reserved section.** The overdue loop in
  `open_commitment` was not in the records list. At `default` the `overdue`
  section carried it, while naming one of that project's three overdue loops.
  At `standard` Tedd withheld the section's items for lack of budget.
- **A search after the context answered one more case.** A context followed by
  a search with the question, at the search tool's default of 10 results, was
  sufficient in two of five oversized cases at `default` and one at
  `standard`. The search found `old_decision`. It did not return the current
  decision in `replaced_decision`, the note in `old_note`, or two of the three
  decisions in `decisions_over_time`.
- **A larger search limit found more, and a stale record with it.** Scoped
  search alone with 100 results was sufficient in three of five oversized
  cases. It returned the current decision in `replaced_decision` together
  with the decision it replaced.
- **Scoped search did not return the overdue commitment at either budget.**
  The route with a context delivered it at `default` only through the
  `overdue` section.
- **Both controls behaved as designed.** A context delivered recent work at
  both budgets. It delivered the small project at `default`, where the project
  fits whole, and not at `standard`.
- **Every case is answerable in a small space.** The exact-ID arm was
  sufficient everywhere with a mean response of 1,111 characters.

Scoped search orders partial matches by how many distinct question words they
contain and then by recency. These generated projects repeat a small
vocabulary, so an old record often ties with many newer ones. That is the
likely reason old records fell outside the shorter limits. The lab did not
test that explanation separately.

## Limits

- This is one run on one Windows host. Latency is not reported here because
  process start-up dominates it.
- The projects and their text are synthetic and generated from a small
  vocabulary. Search on real projects, whose wording is more varied, may do
  better or worse.
- The search arms search with the case's question as written. Whether an agent
  searches after reading its context, and with which words, was not measured.
- The catalog holds no claims and no stated preferences, so the run says
  nothing about the `current_state` and `preferences` sections.
- The run measures what a read delivers. It does not show whether an agent
  would answer correctly with it.
