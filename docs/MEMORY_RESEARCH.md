# Tedd memory research: August 2026 evidence

- **Report date:** 2026-08-09
- **Status boundary:** Pilot status below was reported on this date. This report does not establish whether its planned September 6 close occurred or whether later results exist.
- **Scope:** Completed synthetic scale, retrieval, and agent studies, with operator-reported status-only coverage of a manual observation pilot
- **Current finding:** The tested text-and-recency retrieval method made context smaller and more precise, but it reduced downstream answer correctness and omitted required evidence. It failed the predeclared agent-evaluation requirements, and no production integration followed.

## Abstract

Tedd stores durable, revisioned project knowledge in a local graph and retrieves a limited set of records for an agent's working context. This research examined three different questions: whether the graph could operate at useful synthetic scale, whether a retrieval method selected better evidence, and whether that evidence improved an agent's answer.

The strongest result is a paired study of 24 synthetic tasks. The existing recency method achieved 97.92% mean correctness, compared with 91.67% for a method that combined full-text matching with recency. Across the 20 tasks that required memory, the combined method used 57.77% fewer evaluator tokens, a deterministic measure of retrieval-packet size. It nevertheless produced two fewer fully correct tasks and four required-evidence omissions, compared with none under recency. Retrieval efficiency therefore did not translate into better agent performance.

A separate 10-case retrieval study helps explain why the combined method advanced to agent testing: it produced sufficient packets in 10 of 10 cases instead of 5 of 10 and used about half as many evaluator tokens. A later retrieval-only study used synthetic histories of previously verified use to reorder records already present in the recency pool. It nearly doubled relevant evidence per evaluator token, but four positive cases still lacked required evidence and source support. The extra local-ledger validation and reordering step also exceeded its 10 ms time limit. Neither retrieval-only result establishes that an agent would make better decisions.

The graph itself completed a synthetic benchmark through 366,000 current records on one Windows host. This is useful engineering evidence for that workload, not a universal capacity guarantee. At the report date, the qualified operator described a manual confirmed-use observation pilot as ongoing. No pilot result was available for analysis here.

## What Tedd's memory system does

Tedd's graph keeps entities, decisions, events, receipts, open loops, and their revision history in a private SQLite-WAL store. Records remain available unless they are explicitly corrected, redirected, redacted, or deleted through a governed operation. Age alone does not erase memory.

When an agent needs context, Tedd selects records from an authorized task scope and packs them into a fixed item and character budget. The resulting packet is working context, not the full memory store. A retrieval method can therefore save context while still harming an answer if it leaves out one necessary fact. The studies below measure storage, retrieval packets, and agent answers separately for that reason.

## Research questions

1. Can the production-native graph load, validate, query, back up, and clean up synthetic stores through the measured 366,000-record tier?
2. Under the same final context budget, can text matching and recency select more relevant evidence than recency alone without adding unsafe material?
3. Does that retrieval change improve downstream agent correctness without increasing omissions, stale claims, unsupported claims, or safety failures?
4. Do memories that a person explicitly confirms as useful recur often enough to justify a future controlled ranking study? This fourth question is still being observed and has no outcome yet.

## Retrieval methods compared

The agent study used the same final budget for both methods: at most 12 records and 12,000 characters of stored record text.

### Recency method

The baseline loaded the task's authorized record set (called a subscription) in the graph's existing recency order. The task query did not change that order. Records were packed directly into the final budget.

The implementation identifier for this method was `context_recency_v1`.

### Text-and-recency method

The tested method did not use embeddings or a language model for retrieval. It followed this fixed sequence:

1. Gather up to 24 full-text matches tied directly to the authorized task subject and up to 24 recent records from the task's authorized set. Both lists came from the same graph snapshot and each had a 24,000-character limit.
2. Remove duplicates, then retain only records that directly referenced the task subject. Reject source-only, stale, inactive, unapproved, untrusted, sensitive, superseded, redirected, or redacted records; records outside the same graph namespace, which is a label separating record domains; records whose stored ID disagreed with the ID in their content; and records with malformed, foreign, or dangling references.
3. Require at least one full-text match that shared two meaningful query words after common stop words were removed. Without that anchor, the method returned no memory.
4. Add narrowly related evidence when it covered a missing query term or supplied source and supporting records under the fixed reference rules.
5. Combine text-match position and recency position with reciprocal-rank fusion using the fixed constant `k=60`. Reciprocal-rank fusion favors records appearing near the top of either list. The fixed rule combines the lists without asking a model to judge them.
6. Pack the resulting order into the same 12-record and 12,000-character final budget as the baseline.

The implementation identifier for this method was `scope_anchored_hybrid_v3`. The report uses the plain description above because the word “hybrid” does not explain what the method actually did.

The retrieval-only study also used a tight budget of 6 records and 6,000 characters. The agent study used only the standard 12-record and 12,000-character budget.

## Evidence hierarchy and study design

The studies answer different questions, and their repetitions do not create additional independent cases.

| Evidence class | Independent sample | What it can establish | Immutable source |
| --- | --- | --- | --- |
| Portable evaluator reference | 12 authored scenarios | Reference behavior for recency, full-text search, an idealized answer key, and a no-memory control under two budgets; five repetitions produced 480 measurements but did not create 480 independent cases | Protocol and result at `8bd5bf3` |
| Graph-capacity engineering | One execution at each of four synthetic tiers | Whether one Windows host completed the specified load, validation, query, backup, one-writer mutation, and cleanup workload | Scale protocol and results at `da53a1b` |
| Retrieval-packet comparison | 10 fresh cases: 8 requiring memory and 2 requiring an empty packet | Packet recall, precision, sufficiency, safety flags, deterministic token count, and latency; no agent outcomes | Retrieval protocol and results at `e2530ad` |
| Agent effectiveness | 24 paired tasks: 20 requiring memory and 4 requiring no memory | Answer correctness, omissions, claims, source support, safety, context use, and latency for one fixed task set | Agent protocol and aggregate result at `4a18649` |
| Confirmed-use retrieval follow-up | 32 fresh synthetic longitudinal trajectories: 28 positive and 4 empty | Whether synthetic histories of verified use could improve retrieval packets from the same recency pool; no agent outcomes | Confirmed-use retrieval result at `6c6412a` |
| Supporting-tool engineering | Synthetic qualification and disposable-graph checks | Whether a manual confirmation observer and a narrow receipt producer could preserve privacy and leave retrieval and graph state unchanged; no memory-quality or user-benefit estimate | Observer result at `240424d` · Producer result at `6631fb0` |

The immutable sources are commits in the maintainers' private research archive;
they are not part of the public repository.

## Results

### Agent effectiveness

This result carries the most weight because it measured completed answers rather than retrieval packets alone. Twenty-four fresh tasks covered project decisions, email drafting, multi-record reasoning, stale-state correction, safety boundaries, and cases where the correct behavior was to use no memory. Each task ran once with each retrieval method, producing 48 isolated agent calls. The requested model was `gpt-5.6-sol` at low reasoning on native Windows with CPython 3.14. Graders received presentations that concealed the retrieval method and produced two grades for each response.

Each grader assigned correctness of 0, 0.5, or 1 against the task's written rubric. A response's score was the mean of its two grades, and the reported condition mean was the average across all 24 task scores. A task counted as fully correct only when both graders assigned 1 and neither grade recorded an omission or another counted defect. For omissions and claims, the scorer kept the larger of the two grader counts for each response and then summed those 24 response-level values; the table does not treat 48 grades as independent cases.

| Outcome | Recency method | Text-and-recency method | Difference |
| --- | ---: | ---: | ---: |
| Mean correctness | 97.92% | 91.67% | −6.25 percentage points |
| Fully correct tasks | 23/24 | 21/24 | −2 tasks |
| Required-fact omissions | 0 | 4 | +4 omissions |
| Critical omissions | 0 | 4 | +4 omissions |
| Stale claims | 1 | 0 | −1 claim |
| Unsupported claims | 0 | 0 | No difference |
| Positive-task evaluator tokens | 8,501 | 3,590 | −57.77% |
| Positive-task correctness per 1,000 evaluator tokens | 2.29385 | 5.01393 | 2.19× |

Both conditions recorded zero scope, privacy, sensitive-data, untrusted-instruction, and unsafe-action violations.

The smaller context made the text-and-recency method look more efficient, but correctness and required evidence were the primary concerns. It failed the predeclared correctness, fully-correct-task, and omission requirements. The public aggregate describes one fixed 24-task sample; it does not include repeated model samples, a public confidence interval, or an inter-rater agreement statistic. The result therefore supports a decision about this tested setup, not a population claim that text-and-recency retrieval is always worse.

### Retrieval-packet behavior

The earlier 10-case study compared the same two methods without calling an agent. Required recall is the share of required records that appeared in the packet. Precision is the share of returned records judged relevant. A strictly sufficient packet included all required evidence and source support while also passing the safety, assertion, and budget checks. Correct abstention means returning no memory when no memory was needed.

| Outcome | Recency method | Text-and-recency method | Difference |
| --- | ---: | ---: | ---: |
| Strictly sufficient packets | 5/10 | 10/10 | +5 packets |
| Correct empty-packet abstentions | 0/2 | 2/2 | +2 cases |
| Required recall | 1.0000 | 1.0000 | No difference |
| Precision | 0.46875 | 1.0000 | +0.53125 |
| Evaluator tokens | 2,584 | 1,285 | −50.27% |
| Flagged retrieval exposures | 5 | 0 | −5 flags |
| Standard-budget retrieval 95th percentile | 2,280.513 ms | 4,184.149 ms | +83.47% |

The semantic counts and token totals describe the 10 cases; repetitions did not multiply them. The latency percentile came from 50 measured standard-budget reads per method: five repetitions across each case. The exposure count records evaluator flags for unwanted synthetic packet content, such as stale or out-of-scope records. It is not a count of external actions. Evaluator tokens are a deterministic packet measure, not billed model tokens.

This study improved packet sufficiency, precision, and filtering, which justified testing downstream answers. The later agent study then showed why retrieval scores cannot stand in for answer quality; the method saved context but omitted facts that the agent needed.

### Confirmed-use retrieval follow-up

A later study tested a different idea. Here, a valid “confirmed use” was either a deterministic verified outcome or an explicit user confirmation tied to an exact record revision. Synthetic confirmed-use histories could move frequently useful records higher, but only among records already present in the existing recency pool. The method could not discover a record that the pool had excluded. Memory and revision history remained durable; only ranking priority could age.

Among 28 positive trajectories in a 32-trajectory study (the other four correctly required empty packets), the method increased mean required-evidence recall from 47.62% to 95.24% and relevant evidence per 1,000 evaluator tokens from 1.0141 to 2.0182, a 99.02% relative gain. It also reduced critical omissions from 4 to 0. However, 4 positive trajectories still lacked complete required evidence and source support. The 95th-percentile time for validating the separate local ledger and reordering a packet was 86.81 ms against a predeclared 10 ms limit. That latency used 160 measured reads: five per trajectory after one warm-up. Repetitions measured latency and determinism; they did not turn 32 trajectories into 160 independent cases.

This was a valid retrieval-only result, but it failed the predeclared complete-recall, source-support, omission, and local-ledger latency requirements. It did not test agent answers, user benefit, or production traffic, and it did not justify a ranking change. A proposed version 2 stopped on an abstention-scoring defect before producing a complete aggregate; its partial diagnostic values are excluded from this report.

### Graph-capacity engineering

The repeatable scale study completed one run at each synthetic tier with one writer on a Windows host using CPython 3.10.4 and 12 logical CPUs. Every tier passed its correctness, validation, query, memory, and cleanup checks.

| Current records | Prior revisions | Total run | Full validation | Context query | Database |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 1,000 | 5.657 s | 0.546 s | 5.273 ms | 23.656 MiB |
| 100,000 | 10,000 | 64.770 s | 7.063 s | 8.092 ms | 235.535 MiB |
| 366,000 | 36,600 | 251.139 s | 28.376 s | 3.814 ms | 867.293 MiB |

Each query time is one observation rather than a latency distribution. The study used one synthetic corpus shape on one machine, so it is an engineering qualification for that run, not a general service-level or capacity guarantee. [Knowledge-graph scale posture](SCALE.md) describes the current operational boundary.

## Supporting tools and pilot status at the report date

Two later engineering efforts concerned how confirmed use could be observed without changing retrieval.

Engineering tests showed that the observer could store privacy-preserving confirmation entries without changing retrieval or the graph, and that it rejected malformed or conflicting input. A separate tool could derive receipts for one narrow class of structured decision output without storing the decision text or raw record identifiers. It persisted nothing and was never connected to the ongoing pilot. The tool never changed retrieval and measured neither memory quality nor user benefit. Its detailed test counts and timings remain in its engineering report because they do not answer the research questions above.

At the report date, the qualified operator described the manual confirmed-use observation pilot as ongoing under its prospective protocol at `f5a1a32`. “Observation” meant that recorded events could not change retrieval; collection itself was not automatic. Only a direct user confirmation could add an event. The reported planned close was 2026-09-06 at 17:58:39 UTC, or earlier at 150 accepted events. The protocol prohibited interim analysis, synthetic backfill, producer connection and collection-rule changes. This report contains no verified closeout.

The pilot estimates recurrence among memories that the operator chose to confirm. Manual recording creates selection and burden bias, so a sparse sample would mean that this collection design gathered too little evidence, not that useful memories rarely recur. Even a positive recurrence signal would support designing a controlled study; it would not, by itself, justify changing retrieval or ranking.

## Excluded attempts

Incomplete or integrity-failed attempts remain preserved for engineering history but do not contribute efficacy evidence. Earlier retrieval and agent attempts stopped before producing usable aggregates. The confirmed-use version 2 evaluator mishandled an empty-evidence case and produced no usable result. None of those attempts are used to claim benefit or harm, and their partial measurements are intentionally omitted here.

## Limitations

- The outcome-bearing studies used synthetic tasks on Windows. They do not establish benefit on private user data, production traffic, Linux, or macOS.
- The agent study used one fixed 24-task catalog and one model call per task and condition. It does not measure model-run, curator, provider, or temporal variation.
- The requested model alias and client were recorded, but a remote model build cannot be made immutable in the same way as local source.
- Raw blinded catalogs, task outputs, grades, and private receipts remain outside Git. Public aggregate results can be audited for lineage, but they do not permit independent regrading from `main`.
- Evaluator tokens compare retrieval packets; they are not API billing units. Token efficiency is secondary when correctness or required evidence declines.
- The scale benchmark ran each large tier once on one host and one synthetic corpus shape.
- The observation pilot was manually selected and reported as ongoing on August 9. It has no result in this report.

## Current conclusion

The local graph completed the measured synthetic storage, validation, and query workloads. The evidence does not support the tested text-and-recency retrieval method as a replacement for recency. That method improved retrieval-packet metrics and reduced context, but the downstream agent produced less accurate answers and omitted required facts.

The confirmed-use follow-up improved retrieval metrics but failed its complete-recall, source-support, omission, and local-ledger latency requirements. It never reached an agent evaluation. No completed study supports changing production ranking today. Future retrieval work should begin with a new, clearly defined method and should treat agent correctness and omissions as decisive outcomes, not assume that smaller packets are better.

## Reproducibility and data availability

The repository publishes synthetic evaluation code, protocols, aggregate results, and SHA-256 hashes that bind reports to specific files. Some historical source commits and study runners are retained in the maintainers' private research archive. Production graph content, private user data, raw blinded cases, model responses, grades, and private collection state remain private.

Run the current public retrieval instrument from the repository root:

```bash
python scripts/memory_eval_lab.py --pretty
```

The current evaluator follows [protocol revision 2](../research/memory-eval-lab/PROTOCOL-V2.md). The archived reference measurements used [revision 1](../research/memory-eval-lab/PROTOCOL.md); no revision 2 reference run has been published with a matching SHA-256 hash.

Reproducing the archived scale study requires access to the private research archive, including its source commit, protocol, and `scripts/memory_scale_bench.py` runner.

For a benchmark you can run from this public repository, follow [Knowledge graph scale and limits](SCALE.md#archived-study-and-current-benchmark). Its code, test data, and procedures differ from the archived study, so its measurements describe the current benchmark rather than reproducing the historical results.
