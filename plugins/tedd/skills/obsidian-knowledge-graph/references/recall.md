# Recall scoped graph context

Use this for routine reads and memory challenges. Load the coordination procedure below only when resuming or changing consumers/subscriptions or coordinating shared-brain work. A read does not require a new registration or a closing write.

Read the [graph skill](../SKILL.md) for the shared invariants. Run command examples from the `obsidian-knowledge-graph` skill directory, using its configured store and a supported interpreter; `scripts/` paths are relative to that directory. On Windows use the qualified `py -3` or `python` launcher and native paths.

## Recall workflow

When a user questions a remembered statement, read [source-backed memory](../../tedd/references/source-backed-memory.md) and check the specific memory and saved source. Use the exact existing subscription, start with the bounded `source-evidence` command, and inspect relevant context before answering. Source integrity is not a truth verdict. A challenge does not authorize a durable correction or a wider client search.

Use recall before working on an established client, site, repository, incident, release, or provider when prior decisions or unresolved work could affect the result.

1. Query with the most specific stable names and aliases available. When a name can repeat across clients, use `<namespace> <name>` or the stable entity ID.
2. Review current claims, decisions, open loops, receipts, historical records, and evidence separately.
3. Follow entity links for at most one additional hop unless the user asks for a broader investigation.
4. State conflicts or gaps. Do not force an answer from weak evidence.
5. Reverify current production state when it may have changed.

```bash
python3 scripts/knowledge_graph.py recall "Test Client Alpha hosting incident" --limit 10
python3 scripts/knowledge_graph.py changes --since-watermark 42 --limit 100
python3 scripts/knowledge_graph.py changes --since-watermark 42 --through-watermark 57 --cursor '<cursor-from-prior-page>' --limit 100
python3 scripts/knowledge_graph.py get --id claim_...
python3 scripts/knowledge_graph.py history --id claim_... --limit 25
```

`recall` uses transactionally current FTS candidate lookup, then returns review-safe canonical records with ranked reasons, ordered recall sections, and stable record IDs. `get` performs an exact current or requested-revision lookup; `history` returns immutable revisions for one exact record. Use returned IDs when explaining or correcting remembered knowledge. Use `--mode operator_full` only when the user explicitly requests it and the local environment is private.

For a lossless global audit catch-up, use `changes --since-watermark`. The first page returns `snapshot_watermark`, an authenticated continuation, `complete`, and `advance_to_watermark`; continue only with its cursor. Each change embeds the exact record revision written at that mutation, so paging remains historically stable while current records advance. Normal task awareness should use persistent exact-scope `inbox` subscriptions instead of repeatedly ingesting this global feed.

## Stated preferences

Subscription `recall_context` and CLI `context` put a `preferences` section first,
labeled "stated preferences, with dates". It contains currently effective
preference claims whose single subject is explicitly approved in the task's
subscription. Project
subjects come first, clients next, and the optional operator-global scope last;
each group is newest first, then stable ID. This order controls display and
truncation, not which preference takes precedence. Context references alone do
not make a subject eligible. Quarantined, ended, not-yet-effective, sensitive, redacted, and
redirected subjects or claims are excluded. Unresolved current conflicts retain
`lifecycle: conflicting` or `effective_conflict: true`; do not choose one as the
operator's settled default. The latter flag compares visible effective values
on the same claim surface before limits, so a shortened list cannot hide their
disagreement. It does not detect contradictory wording across different subjects.

Read the condition and reason along with `value`, `valid_from`, `valid_to`, `source_tier`,
`revision`, and the stable claim and subject IDs. A preference is evidence of
what someone stated, not authority over the current task. On a conflict, state it
in one sentence, follow the current request, and offer to update the default.
Only an owner-confirmed standing change goes through reviewed supersession;
neither a one-off exception nor observed behavior changes the saved preference.

Dates determine when a replacement takes effect. If a successor starts in the
future, the old value remains in this section until its `valid_to`, even though
its saved lifecycle already says `superseded`. At that boundary, the successor
appears. Historical values whose ending date has passed stay out of the summary.

The section holds at most 12 items and 2,000 characters after sanitization. It
uses spare space after recent records and the existing overdue summary, inside
the caller's complete `max_chars` budget. Read `truncated`, `omitted`, and
`reason: budget_exhausted` together; missing details are not evidence that no
preference exists. An optional context plan covers only disclosed preference
claims, alongside the other returned records. Reading never subscribes to global
preferences or changes any saved default. The
[setup option](setup-and-recovery.md#optional-global-preferences) adds that scope
only after approval; the [capture schema](capture-schema.md#preferences) describes
the record shape.

## Current claims

Subscription `recall_context` and CLI `context` return a `current_state`
section after `preferences`, labeled "current claims, with dates". It is the
task's fact sheet: each item is the current value on one claim surface, so the
task can read what is true now without sorting dated notes.

A claim appears when it is in the subscription's scope, active, and `current`
or `conflicting`. Preference claims have their own section and are left out.
Superseded, quarantined, sensitive and redacted claims, and claims on a
sensitive or inactive surface, are excluded before the item limit. Claims whose
surface subject the subscription names explicitly come first, then the most
recently changed, then stable ID. This order controls display and truncation,
not which claim is more reliable.

| Field | Meaning |
| --- | --- |
| `id`, `revision` | The claim record and its current revision |
| `surface_ref`, `subject_refs` | The claim surface and the entities that surface is about |
| `family`, `key`, `value` | The saved claim |
| `valid_from` | When the value took effect |
| `last_confirmed_at` | The latest observation time among the claim's evidence at `validated_structured_candidate` or above |
| `source_tier`, `lifecycle` | The claim's authority and lifecycle |
| `effective_conflict` | `true` when the claim is `conflicting`; do not pick one value as settled |
| `cited_observations` | How many observations the claim cites as [folded notes](#find-notes-to-fold) |

Restating the same current value from validated evidence moves
`last_confirmed_at`. An assistant inference does not. The field says when the
claim was last checked; it does not show that the claim is still true. Reverify
unstable or high-risk state before acting on it.

The section holds at most 12 items and 2,400 characters after sanitization,
and never more than a quarter of the caller's `max_chars`. That space is
reserved before recent records. Read `truncated`, `omitted` and
`reason: budget_exhausted` together: an omitted section is missing
information, not evidence that no claim exists. An optional context plan covers
the disclosed claims. A build that returns no `current_state` section predates
this feature.

## Waiting work

The same context returns a `waiting` section with exact counts for the
subscription. No item limit applies, so the counts stay exact when the detailed
lists are shortened.

| Field | Meaning |
| --- | --- |
| `commitments.unfinished` | Loops that are `open` or `candidate` |
| `commitments.overdue` | Unfinished loops that meet the [overdue rule](#review-overdue-items) |
| `commitments.unassigned` | Unfinished loops with no owner |
| `commitments.agent_owned` | Unfinished loops whose every owner is an agent entity |
| `commitments.named_owner` | Unfinished loops with any other owner |
| `proposals.undecided` | Decisions that are proposed or still candidates |
| `proposals.older`, `proposals.older_than_days` | Undecided proposals older than the stated number of days |
| `notes.unfolded` | Observations that no visible claim cites |

Sensitive, redacted and out-of-scope records are not counted. When the budget
cannot fit the counts, the section is
`{"truncated": true, "omitted": true, "reason": "budget_exhausted"}`. Follow the
[intake rule](../../tedd/references/knowledge-graph-lifecycle.md#intake): report
one line, and offer a review only when the operator asks or an item affects the
task.

## Review what is waiting

`review-queue` lists the records behind those counts. Use the consumer and
active subscription from the task's verified registration, as for
[overdue review](#review-overdue-items). The command reads only.

```bash
python3 -B scripts/knowledge_graph.py --config '<approved-graph-config-path>' review-queue \
  --consumer-id '<registered-consumer-id>' \
  --subscription-id '<active-subscription-id>' --limit 20 --mode review_safe
```

On Windows, use `py -3 -B` and enter the command on one line. If the CLI does
not recognize `review-queue`, the loaded build lacks this command.

| Section | Contents and order |
| --- | --- |
| `summary` | The [waiting counts](#waiting-work) |
| `commitments` | Unfinished loops: overdue first by days overdue, then by nearest checkpoint, undated last |
| `proposals` | Undecided decisions, oldest first |
| `notes` | For each entity the subscription names, how many of its observations no visible claim cites |

Each commitment carries `id`, `title`, `status`, `source_tier`,
`authority_state`, `owner_refs`, `owner_kind`, `next_action`, `checkpoint_at`,
`overdue` and `days_overdue`. `owner_kind` is `none`, `agent` when every owner
is an agent entity, or `named`. Present `none` and `agent` items as waiting on
the operator. Present a `named` item as waiting on that owner, unless the
verified profile shows the owner is the operator. Each proposal carries `id`,
`title`, `status`, `requested_status`, `source_tier`, `authority_state`,
`decided_at` and `age_days`.

`--limit` accepts 1 to 50 and applies to each section; `truncated: true` means
more matched within scope. There is no pagination cursor. A candidate remains
a proposal and a listed item is not an instruction: the queue shows what waits,
and only the operator's answer settles it. To save those answers, follow
[answer the review queue](capture-and-correction.md#answer-the-review-queue).

## Find notes to fold

An observation is folded when a claim cites it in `observation_refs`. A folded
observation leaves the `records` list of a task context when that task can read
the citing claim, and that claim has reached the truth surface as `current`,
`conflicting` or `superseded`. A task that cannot read the claim keeps seeing
the observation. Folded observations stay in search results, exact `get`
lookups and history.

`fold-candidates` lists the observations still unfolded, beside the claim
slots that could absorb them:

```bash
python3 -B scripts/knowledge_graph.py --config '<approved-graph-config-path>' fold-candidates \
  --consumer-id '<registered-consumer-id>' \
  --subscription-id '<active-subscription-id>' \
  --subject-id '<entity-id>' --family '<family>' --limit 20 --mode review_safe
```

`--subject-id` and `--family` are optional. A subject narrows notes and slots
to one entity; a family narrows the notes to one family. `--limit` accepts 1
to 50 notes.

| Field | Contents |
| --- | --- |
| `unfolded` | Every unfolded observation for the subject, before the family filter and limit |
| `families` | Up to 50 families with their note counts, largest first |
| `notes` | Unfolded observations ordered by family, key and ID, with `id`, `family`, `key`, `value`, `observed_at`, `source_tier` and `entity_refs` |
| `slots` | Up to 50 [current claims](#current-claims) for the subject |

The command reads only and proposes nothing. Folding is a reviewed capture;
follow [fold notes into claims](capture-and-correction.md#fold-notes-into-claims).

## Review overdue items

Subscription `recall_context` and CLI `context` include an `overdue` section
for unfinished `open` or `candidate` loops. A loop qualifies when its checkpoint
is in the past, or when it has no checkpoint and was first observed more than
30 days ago. Updating it does not reset that age. Scope and privacy filtering
run before the item limits; candidate status and source authority are preserved.

### Read the context summary

The section holds at most five items and 1,500 characters. The caller's
`max_chars` covers the complete disclosed context, including metadata and any
requested plan token. The [waiting counts](#waiting-work) and the bounded
[current claims](#current-claims) are reserved first. Fitting recent records
and their plan read set come next; overdue details use what remains, and
stated preferences follow. Oversized records can themselves
be omitted with `truncated: true`. A budget too small for required metadata
returns `invalid_request`.

Read `items`, `truncated`, and `omitted` together. `reason: budget_exhausted`
means details did not fit. A shortened list is not a total, and an empty list
with truncation or omission does not establish that nothing is overdue. Follow
the [intake rule](../../tedd/references/knowledge-graph-lifecycle.md#intake): one
summary line, with questions only for items relevant to the task.

### Run a focused review

Use the consumer and active subscription from the task's verified registration.
Do not infer them from the directory or create a broader subscription for this
read. From this installed skill's directory, replace the placeholders with the
approved graph configuration path and those existing identifiers:

```bash
python3 -B scripts/knowledge_graph.py --config '<approved-graph-config-path>' loops-review \
  --consumer-id '<registered-consumer-id>' \
  --subscription-id '<active-subscription-id>' --limit 20 --mode review_safe
```

On Windows, use `py -3 -B` in place of `python3 -B` and enter the command on one
line in PowerShell; the backslashes above are Bash continuations. If the CLI
does not recognize `loops-review`, the loaded build lacks this command. A scope
or consumer refusal requires checking the existing binding, not widening it.

The result contains `items` and `truncated`. Each item includes its `id`,
`title`, `status`, `source_tier`, `authority_state`, `owner_refs`, `next_action`,
`checkpoint_at`, and `days_overdue`. Empty owner references and a null checkpoint
remain unknown. Owner references are saved IDs, not resolved names; a candidate
is not an accepted obligation. Items are ordered by whole days overdue,
descending, then ID. For an undated item, `days_overdue`
counts whole days beyond the age threshold, not its entire age. A newly eligible
item can therefore have `days_overdue: 0`.

| Option | Default | Allowed range and meaning |
| --- | --- | --- |
| `--limit` | 20 | 1–20 returned items; `truncated: true` means more matched within scope. |
| `--overdue-days` | 0 | 0–36,500; the checkpoint must be strictly more than this many days in the past. |
| `--age-days` | 30 | 1–36,500; an undated item must be strictly older than this many days since first observation. |

For example, add `--overdue-days 7 --age-days 60` to review checkpoints more
than seven days past and undated commitments older than 60 days. These flags
affect this command only, not the defaults of later context summaries. There is
no character-budget flag or pagination cursor for this command; a truncated
result is not a complete backlog.

Reading does not acknowledge changes or create, capture, resolve, or reschedule
records. The four MCP tools are unchanged; `loops-review`, `review-queue` and
`fold-candidates` are CLI commands.

## On-demand MCP search

Check that the connected server advertises `search_memory` before calling it.
If it is missing, follow the [MCP and CLI fallback procedure](setup-and-recovery.md#optional-mcp-and-cli-fallback).
Absence does not authorize installing a new build or using global CLI search to
bypass task scope.

Use search alongside recent context when a specific older record could change
the task. Reuse the exact existing active subscription and its consumer, agent
and task. Obtain those identifiers from the verified task registration and
subscription; do not infer them from names or the working directory. A request
cannot change the subscription's selection rules or create another subscription.

### Request and matching

This is an MCP argument object, not a shell command. Replace the placeholders
with the verified identifiers. An optional `run_id` records the run, but grants
no authority.

```json
{
  "subscription_id": "<active-subscription-id>",
  "consumer_id": "<registered-consumer-id>",
  "agent_id": "<registered-agent-id>",
  "task_id": "<registered-task-id>",
  "query": "logo approved",
  "limit": 10,
  "max_chars": 12000
}
```

Start with literal words or an exact canonical record ID. Text matching is
case-insensitive and uses SQLite's Unicode word tokenizer. Quotes do not enable
a query language, and `logo OR approved` treats `OR` as a word.

Search first looks for records that contain every term. An eligible exact-ID
match comes first, followed by those records ordered by most recent record
update and then stable ID. The result reports `match_mode: all_terms`. This
order is not a confidence or authority score and does not use corpus-wide
relevance ranking.

When no eligible record contains every term, search returns partial matches
and reports `match_mode: any_terms`. It sets aside common English function
words such as `what`, `the` and `before`, lists the remaining terms in
`considered_terms`, and returns records that contain at least one of them.
Records are ordered by how many distinct considered terms they contain, then by
most recent record update and stable ID. Each record lists the terms it contains
in `matched_terms`. That count belongs to the record alone, so no other record,
inside or outside the subscription, changes its position. A one-term query has
no partial form, and a query of function words alone has no partial matches.

A partial match is a lead, not an answer. A record that matched `logo` but not
`approved` does not show that anything was approved. Compare `matched_terms`
with `considered_terms`, read the record's own status, and search again with
the record's wording when the missing term matters.

The optional `match` argument selects this behavior:

| `match` | Returns |
| --- | --- |
| `auto` | Records containing every term; partial matches only when there are none. This is the default. |
| `all` | Records containing every term, or an empty list. |
| `any` | Partial matching for every query. Records containing more of the considered terms still come first. |

Check that the connected `search_memory` tool lists `match` before relying on
partial matches or sending the argument. A build without it requires every term,
returns an empty list when one is missing, and refuses `match` as
`invalid_request`.

Matching compares whole words, so `approved` does not match `approval` in either
pass. When word endings may explain a missing or partial match, try one
deliberate prefix broadening within the same subscription. For example, retry
`logo approved` as `logo approv*`, keeping the other terms. A prefix is an
unquoted word that normalizes to at least four ASCII letters or digits, followed
by one trailing `*`. `approv*` matches `approved` and `approval`; `approved*`
does not match `approval`. `harbor*` can match both `harborstudio` and
`harborside`, so it cannot establish that two names identify the same project.
A prefix counts as one term in both passes. Short, quoted, punctuated, leading,
embedded or repeated wildcards return `invalid_request`, not broader results.

The one-retry limit applies to prefix broadening, not every search attempt.
Different relevant literal words, such as `palette approved` and then
`teal approved`, remain appropriate within the same scope and budgets. Do not
repeatedly shorten prefixes, drop the question's constraints or change scope to
force a match. An empty result is not proof that the event never happened.

| Argument | Default | Accepted range |
| --- | --- | --- |
| `query` | Required | Nonblank text, at most 2,000 characters and 32 distinct terms; punctuation alone is invalid |
| `limit` | 10 | 1 to 100 records |
| `max_chars` | 12,000 | 2 to 100,000 characters of disclosed result-list JSON |
| `match` | `auto` | `auto`, `all` or `any` |

The character cap applies after review-safe sanitization and includes the list
brackets, separators and first record. Metadata is outside that cap. Search
returns only the latest saved revisions of compact graph records. It searches
indexed fields such as names, aliases, titles, summaries and values, not archived
source bytes. Sensitive, redacted, tombstoned and unavailable current payloads
are excluded before result limits, including for exact-ID requests. Previous
revisions require the separately authorized CLI history workflow.

### Interpret the result

`match_mode` says which pass produced the list: `all_terms` records contain
every term, and `any_terms` records are partial matches that each carry
`matched_terms`. `considered_terms` lists the terms that pass used. State a
partial match as partial when reporting it.

`result_count` counts returned records only. `truncated: true` means a count or
character limit withheld matching results. An empty truncated list may need a
larger character budget; an empty untruncated list may need fewer or different
query words. Neither proves that no memory exists. A scope or ownership refusal
is an error, not a no-match result; resolve the exact registration rather than
widening the read.

Results retain record IDs, revisions, watermarks and their disclosed payloads.
A watermark is the graph's mutation sequence number; it is not an event date or
an authority score. Respect lifecycle, decision status, authority and source
tier separately. A decision with `status: proposed` is still a proposal even
when its `authority_state` is `accepted`.

Search never advances inbox acknowledgement. Reconcile the inbox independently
before a consequential action. Use the
[source-evidence workflow](../../tedd/references/source-backed-memory.md) when a
memory is challenged; neither search nor a challenge authorizes a durable correction.

## Multi-agent coordination

Read and follow [references/multi-agent-protocol.md](multi-agent-protocol.md) whenever more than one task, child agent, monitor, or email worker can touch the shared brain. Point every production agent at the same configured store. SQLite WAL permits concurrent local readers and serializes short packet-sized write transactions; a timeout is a reconciliation/retry signal, never permission to bypass the store.

Each committed mutation receives a global watermark and hash-chain link for complete audit order. Each changed record receives its own immutable revision for relevant optimistic concurrency. An unrelated client mutation may advance the watermark without invalidating a reviewed plan; a changed entity, claim surface, current claim, decision, or loop in the plan read set rejects the write and requires re-recall.

Inspect a stable task consumer with `consumer-status` before registration. Resume an exact existing owner and active subscription; use the shared private profile's agent-instance ID only for a genuinely new consumer. For a saved project binding, copy its returned selectors exactly: `entity_ids` become `--subject-id`, and only its `record_ids` become `--record-id`. An empty record list stays empty. For separately approved manual scope, subscribe to dependent records with `--subject-id` and approved entities, surfaces or loops with `--record-id`. Pull the bounded inbox/context pack at task start and before an important decision or handoff. Acknowledge only a complete authenticated delivery. These scoped pulls provide awareness without injecting unrelated clients into context. They do not automatically wake an agent; push requires separate Codex/runtime integration.

For every v4 command that exposes the collaboration envelope, pass the required caller-controlled agent, task, run, and based-on-watermark provenance; include the optional parent task when one exists. A genuinely new Tedd consumer uses the stable opaque agent-instance ID from the shared private profile, not its display name, an operator name, or an email address. An existing consumer keeps the exact owner returned by `consumer-status` until an optional coordinated `consumer-rebind`:

```bash
python3 scripts/knowledge_graph.py consumer-status --consumer-id codex_task_123
python3 scripts/knowledge_graph.py consumer-register --consumer-id codex_task_123 \
  --producer-agent-id '<configured-agent-id>' --task-id task_123 --parent-task-id task_parent \
  --producer-run-id run_456 --based-on-watermark 42
python3 scripts/knowledge_graph.py subscribe --consumer-id codex_task_123 \
  --record-id entity_client_alpha --subject-id entity_client_alpha \
  --record-id loop_client_alpha_review
python3 scripts/knowledge_graph.py inbox --subscription-id '<returned subscription_id>' --limit 100
python3 scripts/knowledge_graph.py context --subscription-id '<returned subscription_id>' --max-chars 12000
```

`--producer-agent-id`, `--task-id`, `--parent-task-id`, `--producer-run-id`, `--discovery-kind`, and `--based-on-watermark` describe the caller, durable outcome, and snapshot it actually used. Captured source text cannot populate or raise these fields. Use the dry-run fingerprint plus v4 plan token to protect reviewed writes. The mutation feed and record history retain provenance so another agent can audit who wrote a change and which snapshot informed it.

Never auto-rebind during installation or profile setup. If an explicit rebind is later approved, quiesce the exact task, discard in-flight pages and plans, preserve its subscriptions and cursors, then re-pull inbox/context and rerun capture dry runs. See `multi-agent-protocol.md`.
