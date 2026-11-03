# Check memories against saved sources

A memory is a useful summary, not the whole conversation. When the user asks
“Are you sure that is what they said?”, check the specific memory and its evidence
before defending or retracting it. A valid file hash proves which bytes were saved;
it does not prove the summary is correct or that a speaker had authority.

This is an opt-in source archive, separate from the graph. Loading this reference
does not enable it. Do not create an archive, change an installation, or backfill
old conversations merely to answer a challenge.

## When the user questions a memory

1. Identify the disputed statement. If “Are you sure?” could refer to several
   things, ask which one; do not search unrelated memories. If the statement was
   your own reasoning rather than recalled memory, say so and check its actual
   basis; do not invent a graph record or pretend a new source was the original basis.
2. Resume the exact existing consumer and authorized subscription. Use its scoped
   context to identify the memory. A known record ID does not authorize another
   client's data. Do not switch to global recall or `operator_full` to get past a refusal.
3. Call `source-evidence` for that record, including the cited revision if known.
   For provider-checked Google evidence, use source MCP `check_memory` with those
   flags through the configured endpoint; the standalone CLI has no provider
   session. Apply [host integration](source-host-integration.md#google-host-integration)
   without changing permissions or creating a new source to answer the challenge.
   This checks the remembered value, current replacement, and saved evidence.
   Keep the default 12,000-character/five-version limits initially.
4. Read the actual passages, attribution, conditions, and coverage, not just the
   list of links. Follow a reply, quotation, or attachment only when it affects
   the disputed meaning. Resolve the attached version exactly; “the second option”
   in version 2 need not mean the second option in version 3. Use scoped lookup
   and selected inspection for missing context. Never substitute a nearby source.
5. Check later captured corrections or retractions. “Later captured” is not proof
   of provider chronology. Unknown speakers and dates stay unknown. A proposal,
   quoted suggestion, or assistant-authored report is not independent confirmation.
6. Give the supported answer with the saved version/date and message or page
   anchor. Confirm an accurate memory when the evidence supports it; do not change
   your answer just to agree. Correct missing words such as “not” and “if” explicitly.
7. Correct the answer now when justified. Changing durable memory is a separate
   action: ask for that correction unless already explicitly authorized, then use
   the normal reviewed successor capture. A challenge alone never applies it.

The examples below run from the `obsidian-knowledge-graph` skill directory.
Use the approved configuration and exact registered session values. On Windows,
replace `python3` with the qualified Python 3.10+ launcher, such as `py -3.10`.

```bash
python3 scripts/knowledge_graph.py --config "<approved-config>" source-evidence \
  --id claim_example --consumer-id consumer_example --subscription-id subscription_example \
  --producer-agent-id agent_example --task-id task_example --producer-run-id run_example \
  --subject-id entity_example --max-chars 12000 --max-versions 5
```

The example is the local-evidence CLI path. Source MCP `check_memory` takes the
same operation flags in `argv`, without `source-evidence` or `--config`.
`--revision` is optional. The public command allows at most 48,000 characters and
five source versions. Increase the initial text budget only for relevant missing
context; ask before exceeding those investigation limits. A smaller valid budget
can still be too small for a complete packet. `limit_exceeded` means the request
or its result exceeded a bound, not that the stored source is corrupt. Correct an
invalid argument once; do not relabel a command error as evidence about the source.

`complete: false`, omitted units, unresolved links, or unread later versions mean
work remains. They do not mean the disputed claim is false. Inspect the returned
next anchor or exact linked version within the same overall budget. If the
necessary context cannot be read, explain that limit instead of inventing a quote.
Stop on scope, privacy, corruption, or path-safety failures: no bypass or wider search.

For a table, ambiguous extraction, OCR, or an image-only PDF page, use the applicable
PDF skill and a qualified host renderer. The source CLI's `render-page` takes the
exact `--source-uri`, `--page`, `--reader-python`, and `--renderer`, plus the same
session/scope flags. Send its bounded PNG straight to the host image display; do
not print its base64, add it to a prompt, or make a permanent second copy. OCR and
extracted text remain derivatives. If the original page cannot be inspected, say
which part remains unverified.

For a saved standalone image, use the qualified image reader to inspect its exact
returned unit with `render_image`. Follow the [image review procedure](source-preservation.md#review-a-standalone-image-before-keeping-it)
for detail, cropped views and coverage. An overview is not enough to settle a
dispute over a small word or number; missing readable detail stays unresolved.
Neither decoding nor a matching original hash proves that hidden encoded bytes
were inspected. Owner-only evidence remains on the private-viewer path.

## A saved source with no memory record

Use bounded metadata lookup within the exact authorized task/project, then inspect
only the selected result. This is a fresh source check, not a remembered fact.
For provider-checked Google sources, use source MCP `lookup` and `inspect` through
the configured endpoint so provider access can be checked. The CLI example below
is for permitted local evidence; it has no Google provider session.

```bash
python3 scripts/tedd_sources.py --config "<approved-config>" lookup \
  --source-id manual --source-version v2 \
  --consumer-id consumer_example --subscription-id subscription_example \
  --producer-agent-id agent_example --task-id task_example --producer-run-id run_example \
  --subject-id entity_example
```

Lookup also accepts `--query` for source metadata, not a global full-text search.
It returns at most five matches per page; continue with its `next_offset` and
`snapshot_sha256` only when needed. `inspect --source-uri <returned URI>` uses the
same flags and a default 12,000-character budget. A missing or revoked source is
not permission to search another scope. A deleted external link need not block a
check when the verified private copy still exists; identify that preserved copy,
not an allegedly current online page.

## Preserve sources during already-authorized work

Before retaining a source, read [source preservation](source-preservation.md).
It covers purpose, exact selection, local intake, document review and memory
binding. Collection must already be enabled for the exact authorized task scope.

For browser exports, Google access or source-MCP setup, also read
[source host integration](source-host-integration.md). The trusted host supplies
task selections, current provider access and cancellation; a model cannot supply
those grants through tool arguments. Use-once instructions override retention.

## Owner navigation and privacy

Read [viewing and maintenance](source-viewing-and-maintenance.md#owner-navigation-and-privacy)
before viewing evidence or changing display approval. Retention, owner display,
model access and vault exposure are separate permissions. The private viewer is
separate from the unqualified Obsidian source-preview route.

## Pause, forget, and recover deliberately

Read [source maintenance](source-viewing-and-maintenance.md#pause-forget-and-recover-deliberately)
before restriction, forgetting, backup/restore or recovery. Disabling collection
keeps history. Restriction denies a source without erasing it; forgetting requires
reviewed impact and can leave shared bytes or older backups. Never treat incomplete
cleanup as successful erasure.
