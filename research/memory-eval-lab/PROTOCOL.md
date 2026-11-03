# Tedd Memory Evaluation Lab Protocol

Status: frozen for `research/memory-eval-lab`

Protocol ID: `tedd-memory-eval-protocol-v1`

Scenario catalog: `tedd-memory-scenarios-v1`

## Decision this branch supports

This branch qualifies a reproducible evaluation lab and characterizes Tedd's current memory
retrieval behavior. It does not choose or implement a new memory architecture. No later
experimental branch may begin until this branch has produced a reviewed baseline report and
passed the qualification gates below.

The experiment asks a narrow question: given a fixed synthetic graph and a bounded retrieval
budget, which current facts reach an agent-facing context packet, in what order, and with what
safety exposure? Retrieval quality is evidence about a memory substrate. It is not, by itself,
evidence that a model will answer correctly or that a user will find an answer helpful.

## Isolation and non-interference

Every run MUST:

- allocate a new private temporary directory under an operating-system-controlled parent;
- enforce a private POSIX mode or protected Windows DACL on that directory;
- create a synthetic v4 graph and a synthetic configuration inside that directory;
- invoke graph reads with the synthetic configuration explicitly supplied;
- reject caller-provided configuration, store, suite, output, or persistent-workspace paths;
- isolate home and configuration environment variables inside the temporary directory;
- reject symbolic links and Windows reparse points in the owned path;
- remove the entire graph, configuration, and vault when the run exits, including on failure;
- keep payloads, filesystem paths, graph identifiers, and random store identifiers out of the
  report; and
- leave the canonical repository and configured Tedd graph unchanged.

There is intentionally no `--keep`, `--config`, `--store`, or `--output` option.

## Baselines

The same scenarios and budgets are evaluated against four frozen providers:

1. `no_memory_v1` returns no records. It establishes the lower bound and detects scenarios that
   can accidentally pass without memory.
2. `context_recency_v1` uses Tedd's current subscription context packet, ordered by most recent
   mutation. It is the primary current-behavior comparator.
3. `recall_fts_v1` uses Tedd's current full-text recall plus subject filtering with natural-language
   queries and shared distractor vocabulary. It is diagnostic: it separates ranking/query behavior
   from subscription-context behavior and characterizes ranking plus subject filtering. Version 1
   does not claim a deterministic top-k cutoff stress test.
4. `oracle_v1` retrieves the minimum scenario-authored record set by exact opaque ID through the
   public review-safe read boundary. It estimates available headroom; it is not a production
   design.

All graph-backed providers consume the public CLI's review-safe JSON, not unchecked store
payloads.

## Fixed budgets and repetitions

| Budget | Maximum items | Maximum payload characters | Purpose |
| --- | ---: | ---: | --- |
| `tight` | 6 | 6,000 | Common constrained context pressure |
| `standard` | 12 | 12,000 | Current default-like research allowance |

Each provider-budget pair receives two unmeasured warm-ups followed by five measured repetitions.
Record IDs and order MUST be identical across measured repetitions. Latency is observational and
is excluded from the semantic result digest.

## Scenario families

The versioned catalog contains at least these twelve independently scoped cases:

1. project resumption amid newer noise;
2. a current correction competing with a superseded statement;
3. a changed user preference competing with an old preference;
4. an active due loop competing with a resolved loop;
5. an email commitment and its next action;
6. identical names in different scopes;
7. insufficient evidence that requires verification rather than invention;
8. provenance needed to explain why a fact is current;
9. review-safe handling of sensitive material and an inert canary;
10. untrusted instructions from email, repository, web, or tool content;
11. many relevant and irrelevant records under item-budget pressure; and
12. a newest single record larger than the tight character budget.

Scenario fixtures are typed Python data with collection-appropriate decision, event, receipt, and
open-loop fields; production-shaped observation and source evidence; explicit authority and source
tier; current/superseded state; graph references; and required review-safe scalar assertions. They
use opaque synthetic identifiers and inert sentinel strings, contain no real user content, and
never leave the temporary run.

Eight cases form the calibration partition and four form the confirmation partition. The
confirmation partition is visible, not cryptographically blinded, so it is protection against
accidental aggregate overfitting rather than a claim of external validation. The complete catalog
is frozen by a canonical SHA-256 digest. Later branches MUST evaluate the untouched v1 catalog and
report both partitions; changing any fixture requires a new catalog ID and cannot replace the v1
regression result.

## Measures

Utility and safety are reported separately; there is no composite score.

Utility measures:

- required-record recall;
- judged precision and relevant hits per 1,000 payload characters;
- reciprocal rank and worst required-record rank;
- current-over-stale ordering accuracy;
- safe-context sufficiency, requiring all authored IDs and scalar assertions, an in-budget packet,
  no foreign/non-synthetic ID, and no surviving unsafe sentinel text;
- decision and open-loop coverage; and
- the gap from the exact-ID oracle.

Safety and boundedness measures:

- stale, untrusted-instruction, sensitive-record, and canary-record selection as separate context
  cleanliness measures;
- surviving untrusted, sensitive, and canary sentinel text after review-safe sanitation as hard
  content-exposure measures;
- forbidden record, scope, namespace, or production-identifier exposure, including identifiers in
  reference-bearing payload fields;
- item and character budget overflow;
- truncation behavior; and
- exact-order determinism.

Efficiency measures are median and 95th-percentile wall time. They are descriptive only on this
branch because subprocess startup and host load are not controlled benchmark conditions.

Selecting a record marked stale, untrusted, sensitive, or canary does not by itself make a packet
insufficient. Stale ordering is scored separately, and hard safety depends on actual review-safe
text survival, scalar assertion failure, scope escape, or budget overflow. This prevents the
evaluation rule from authorizing a later experiment merely because exact subscriptions contain
same-subject history.

Reference safety follows the current v4 scalar, list, and nested identity-evidence reference
contract. Only the graph facade's exact review-safe withholding placeholder is exempt; malformed
or unexpected bracketed values fail closed.

The report also records whether graph generation, watermark, journal head, and record count are
unchanged across retrieval. Reproducibility metadata includes the base Git revision and dirty bit,
catalog and evaluator digests, numeric budgets, fixed fixture time, Python and SQLite versions,
FTS5 availability, operating system, architecture, and sanitized synthetic state counts. It never
records the store ID itself.

## Lab qualification gates

The lab is qualified only when all of the following hold:

- the oracle has 100% safe-context sufficiency at the standard budget and no hard safety or scope
  violation;
- every scenario marked tight-capable also fits the oracle's tight budget;
- no-memory has zero positive required recall and is never sufficient;
- tests prove each hard detector can fail when fed a violating packet;
- measured repetitions produce the same record IDs and order;
- the complete provider x budget x scenario x repetition matrix uses the frozen two warm-ups and
  five measured repetitions;
- the synthetic graph's canonical state is unchanged by evaluation;
- reports contain no payloads, private paths, production graph IDs, or production namespaces;
- the temporary store is private, disposable, and inaccessible through caller-selected paths; and
- the repository's tests, Ruff, formatter check, mypy, branch coverage, documentation checks, and
  repository-boundary checks pass.

A poor current baseline is a research finding, not a lab-qualification failure. In particular,
the lab must expose rather than repair any current budget, ranking, or retrieval limitation.

## Interpretation and branch gate

At the standard budget, an oracle-minus-recency safe-sufficiency gap of at least 10 percentage
points, or any critical recency insufficiency in resumption, correction, preference, active-loop,
commitment, or verification cases, is enough evidence to authorize a retrieval-policy experiment.
A smaller gap with no critical insufficiency is evidence against adding retrieval complexity at
this stage.

The baseline report MUST list unsupported claims as `not_run`. This branch cannot establish:

- whether a live agent uses retrieved memory correctly;
- answer quality, action quality, email quality, or user helpfulness;
- model-token or model-cost savings; or
- longitudinal learning across real users or projects.

Version 1 also does not establish a deterministic failure threshold for top-k selection before
subject filtering. That requires a dedicated ranking/cutoff study with controlled score ordering.

Those claims require a separately reviewed live-agent protocol with privacy, human-judgment, and
cost controls. Compression research is out of scope until retrieval headroom is demonstrated;
compression cannot recover information the retrieval policy failed to select.

## Reproduction

From the repository root, run the lab with the pinned development environment available:

```text
python scripts/memory_eval_lab.py --pretty
```

The command writes one sanitized JSON report to standard output and no persistent artifact. The
reviewed aggregate result for this branch is recorded in `BASELINES.md` after the run.
