# Tedd Project Recall Lab Protocol

Status: current

Protocol ID: `tedd-project-recall-protocol-v1`

Report schema: `tedd-project-recall-report-v1`

Scenario catalog: `tedd-project-recall-scenarios-v1`

Evaluator tokenizer: `tedd-memory-eval-tokenizer-v1`

## Question

Given a synthetic project that holds far more records than one context can
return, what does each of Tedd's reads put in front of an agent, and how large
is it? The lab measures delivery. It does not measure whether a model answers
correctly, and a better delivery score is not evidence of a better answer.

## Relation to the memory evaluation lab

This is a separate instrument. It runs on the
[memory evaluation lab](../memory-eval-lab/PROTOCOL-V2.md)'s sandbox, providers,
scoring, tokenizer and report privacy check without changing them. That lab's
protocol, catalog, command and report are not altered by this protocol.

The memory evaluation lab asks whether the right records reach a small packet.
Eleven of its twelve scenarios hold fewer records than its standard budget of
12, so it cannot show what a read leaves out when a project outgrows the
budget. This lab holds the budgets fixed and makes the projects large.

## Isolation

Every run follows the isolation and non-interference rules of the
[memory evaluation lab protocol](../memory-eval-lab/PROTOCOL.md#isolation-and-non-interference):
one private disposable synthetic graph, review-safe reads only, no
caller-selected path, and a report without payloads, paths or identifiers. The
command accepts only a timeout and a pretty-print option.

Reads go through two public routes. The first is the public CLI, limited to
the memory evaluation lab's frozen read commands. The second is the packaged
read-only MCP server, started with the same synthetic configuration in the
same isolated environment. The lab calls only its `recall_context` and
`search_memory` tools, with the subscription and consumer that the sandbox
registered for the project being read.

## Projects

The catalog holds seven independent synthetic projects. Each is generated from
fixed text tables and SHA-256 choices, so it is identical on every supported
interpreter and platform. The sandbox writes one mutation per record, oldest
first, so a record's position is its recency.

Each project repeats one cycle of twenty routine records: nine notes, five
verification receipts, three decisions, two events and one resolved loop. One
routine decision in four is an undecided proposal. A routine note holds about
690 characters of text and any other routine record about 400, which gives a
mean record payload of 914 characters. Dates follow position across a fixed
120-day span. A few authored records replace routine ones at fixed positions;
the question in each case is about those.

| Case | Records | Placement | Partition | Required records |
| --- | ---: | --- | --- | --- |
| `old_decision` | 300 | `outside_recall` | calibration | One accepted decision, 8% into the project |
| `decisions_over_time` | 650 | `outside_recall` | confirmation | Three accepted decisions, 6%, 34% and 63% in |
| `replaced_decision` | 300 | `outside_recall` | calibration | The current decision, 42% in; the decision it replaced is labeled stale |
| `open_commitment` | 250 | `outside_recall` | calibration | One overdue open loop, 22% in, beside two other overdue loops |
| `old_note` | 250 | `outside_recall` | confirmation | One note, 15% in, that no claim cites |
| `recent_work` | 300 | `recent_work` | calibration | A note and a decision among the newest four records |
| `small_project` | 30 | `fits_one_recall` | confirmation | One accepted decision, 10% in |

Placement is checked when the catalog is built:

- `outside_recall`: every required record has more than 100 records, or more
  than 50,000 payload characters, at least as recent as itself. The records
  list cannot hold it at either budget.
- `recent_work`: every required record is within the newest six records and
  6,000 payload characters. This is a control that recency order should pass
  at both budgets.
- `fits_one_recall`: the whole project fits the `default` budget, and the
  required record is outside the `standard` item limit. This is a control that
  recency order should pass at `default` only.

`old_decision` and `open_commitment` are critical cases. The two partitions
are visible, not blinded; the confirmation partition guards against tuning a
change to the calibration cases.

The projects hold no sensitive, untrusted or canary records; those detectors
remain the memory evaluation lab's job. They hold no claims and no stated
preferences, so the `current_state` and `preferences` sections of a context
response are empty in every case.

The catalog is frozen by a canonical SHA-256 digest. Changing any project,
question or label requires a new catalog ID.

## Arms

1. `no_memory_v1` returns nothing. It is the lower bound.
2. `context_response_v1` is one CLI `context` call for the project's
   subscription. The lab reads the whole response: the records list and every
   reserved section.
3. `recall_fts_v1` is one CLI `recall` call with the case's question, filtered
   to the project. The command ranks matches across the whole graph, keeps as
   many as the item limit, and only then filters to the project, so the other
   six projects' records compete for the limit. This arm is diagnostic. It is
   not the search an agent uses.
4. `mcp_search_v1` is one MCP `search_memory` call with the case's question,
   scoped to the project's subscription, with the budget as its result and
   character limits. This is the search an agent uses.
5. `mcp_context_search_v1` is the route an agent takes when it reads its
   context and then searches: one MCP `recall_context` call at the budget,
   then one MCP `search_memory` call with the case's question at that tool's
   default limits. The lab reads both responses, including the reserved
   sections of the first.
6. `oracle_v1` fetches exactly the required records by ID. It shows that each
   case is answerable inside the budget. It assumes the IDs are already known.

The first, third and sixth arms are the memory evaluation lab's providers,
unchanged. The second uses that lab's `context_recency_v1` provider for the
records list and adds the reserved sections. The two MCP arms replay each
records list they receive through that same provider, so every records list
in this lab is measured by the same frozen rules.

Both search arms search with the case's question as written. The lab does not
measure whether an agent would search, or which words it would use.

## Budgets and repetitions

| Budget | Maximum items | Maximum characters | Purpose |
| --- | ---: | ---: | --- |
| `standard` | 12 | 12,000 | The memory evaluation lab's standard budget |
| `default` | 100 | 50,000 | The shipped `recall_context` default |

`mcp_context_search_v1` has a larger allowance at each budget: the budget for
its context, plus 10 results and 12,000 characters for its search, which are
the shipped `search_memory` defaults. Its sizes are the sum of both responses,
so a record that both steps return is counted twice. Each of its two calls is
still held to that call's own limits, so room left by one step cannot hide an
overrun in the other.

Each arm, budget and case receives one unmeasured warm-up and three measured
repetitions. Record order, section contents and every score must be identical
across the measured repetitions.

## Measures

The records list is scored by the memory evaluation lab's rules, including its
budget, scope, namespace, reference and payload assertion checks. This lab adds
the following.

A reserved section is any top-level member of a context response that is an
object with an `items` array. The rule names no section, so a section added by
a later Tedd change is read without changing this instrument. An item names a
record when it has a text `id`.

- **Listed recall**: the share of required records named anywhere in the
  response, in the records list or in a reserved section.
- **Delivered recall**: the share of required records delivered in full. A
  record is delivered when it is in the records list, or when a reserved
  section item with its ID carries every needed field with the authored value.
- **Limit violations**: calls that returned more than the limits they were
  given. A call's records must not outnumber its item limit, and the part of
  its response that its character limit covers must fit that limit: the whole
  response for a context, the results list for a search. Both are measured in
  canonical JSON, which is never longer than the JSON Tedd budgets. A read by
  exact ID has no limit.
- **Sufficiency**: every required record is delivered, the records list has no
  hard violation, no call has a limit violation, and no reserved section names
  a record outside the project.
- **Stale named**: stale records named anywhere in the response.
- **Withheld sections**: reserved sections that Tedd marks `omitted` because
  their items did not fit the budget. An empty section that is not marked had
  nothing to show and is not counted.
- **Size**: items, section items, payload characters, and the characters and
  evaluator tokens of the complete response in canonical JSON. Items and
  payload characters are also reported as a share of the project.

The needed fields are fixed per record kind:

| Record kind | Needed fields |
| --- | --- |
| Decision | `summary`, `status` |
| Open loop | `next_action`, `status`, `checkpoint_at` |
| Note | `value` |

A section that lists a decision by title therefore earns listed recall and not
delivered recall. Utility and safety are reported separately; there is no
composite score. Latency is observational.

The `overdue` section reports days overdue relative to the run date. The
response size of a case that returns it can therefore differ by a character
between run dates, and a run that crosses a day boundary can fail the
repetition gate and should be rerun. No record-level measure depends on the
run date.

## Full-context reference

For each project the report gives the record count, payload characters and
evaluator tokens of handing over every authored record. This is a reference
size, not an arm: by construction it holds every required record and every
stale one. It is computed from the authored payloads. Each run checks that
every payload any read returns equals its authored payload, so the reference
equals what full review-safe reads would return. The project's own entity
record is not counted.

## Lab qualification gates

The lab is qualified for a run only when all of the following hold:

- the exact-ID arm is sufficient in every case at both budgets and names no
  stale record;
- the no-memory arm delivers nothing and is never sufficient;
- every returned payload equals its authored payload;
- measured repetitions deliver exactly the same thing: the same records in the
  same order, the same content in every reserved section, and the same scores
  and sizes;
- the complete arm, budget, case and repetition matrix uses one warm-up and
  three measured repetitions;
- the synthetic graph's canonical state is unchanged by the reads; and
- the report contains no payloads, paths or identifiers.

A poor result for a read is a finding, not a qualification failure.

## Scoring a leaner record format

A later change may return records in a leaner form than the full payload.
These rules for scoring that form were fixed before any such change was built.

- A leaner form is measured by its own arm, added by a protocol revision with
  a new protocol ID and report schema. The six arms above, the catalog and the
  budgets stay as they are. The existing arms keep the gate that every
  returned payload equals its authored payload.
- A leaner record names its record by ID and counts as listed. It counts as
  delivered only when it carries every needed field of that record with the
  authored value, exactly as a reserved section item does. A field that is
  shortened, reworded or missing is not carried.
- The memory evaluation lab's payload checks apply to full payloads only. A
  leaner record must still belong to the project; one that names a record
  outside it is a scope exposure.
- Size is compared between the leaner arm and `context_response_v1` on the
  same build, at each budget and case by case, in response characters and
  evaluator tokens. A size gain counts only where delivered recall is no
  lower.
- The leaner arm is held to the same guardrails as any candidate.

## Comparing a candidate change

These rules were fixed before any candidate was built. A candidate is a Tedd
change evaluated by running this unchanged protocol, catalog and command on
the build with the change and on the build without it.

- **Primary outcome**: sufficiency of `context_response_v1` at the `default`
  budget across the five `outside_recall` cases.
- **Secondary outcomes**: delivered recall and listed recall for the same arm,
  budget and cases, reported separately. A gain in listed recall is reported
  as listed and never as delivered.
- **Search routes**: the same outcomes for `mcp_search_v1` and
  `mcp_context_search_v1`, reported beside the primary outcome. A gain for
  `context_response_v1` that leaves `mcp_context_search_v1` unchanged helps
  only an agent that does not search, and is reported that way.
- **Size outcome**, for a change meant to return less: mean response
  characters and evaluator tokens for `context_response_v1` at each budget,
  with delivered recall no lower in any case. A leaner record format is
  scored by the rules in the section above.
- **Guardrails**: every case that is sufficient without the change stays
  sufficient with it, at both budgets, which includes both controls; no case
  gains a hard violation, a limit violation, a scope exposure, a withheld
  section, or a newly named stale record; no response exceeds its character
  allowance; and the run qualifies.
- **Confirmation**: a movement counts only if the confirmation partition moves
  in the same direction as the calibration partition.

A candidate that fails a guardrail is not an improvement under this protocol,
whatever its primary outcome.

## What this lab cannot establish

- whether a live agent uses a delivered or listed record correctly;
- answer quality, action quality or user benefit;
- model-token or cost savings, since evaluator tokens are comparison units;
- whether an agent searches after reading its context, and with which words;
  or
- results on real projects, whose wording, mix and history differ.

Those claims need the agent study described in the
[memory research report](../../docs/MEMORY_RESEARCH.md).

## Reproduction

From the repository root, with the pinned development environment available:

```text
python scripts/project_recall_lab.py --pretty
```

The command writes one sanitized JSON report to standard output and no
persistent artifact. It seeds about 2,100 synthetic records and makes about
300 reads, which takes several minutes. The recorded result for Tedd
1.2.0-rc.13 is in [BASELINE.md](BASELINE.md).
