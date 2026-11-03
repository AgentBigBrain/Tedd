# Project the graph into Obsidian

Use this after an authorized capture/correction when a vault is configured, or for an explicitly requested projection/recovery. Routine reads do not require projection. Source previews are a separate disabled route; this procedure projects compact graph records.

Read the [graph skill](../SKILL.md) for the shared invariants. Run command examples from the `obsidian-knowledge-graph` skill directory, using its configured store and a supported interpreter; `scripts/` paths are relative to that directory. On Windows use the qualified `py -3` or `python` launcher and native paths.

## Projection workflow

Project after successful capture or correction when a vault is configured.

```bash
python3 scripts/knowledge_graph.py project --mode review_safe
```

When Obsidian is running, navigate away from any generated `.base` view (or
close that tab) before projecting. An active Base can retain stale in-memory
configuration and write it back after an external projection. After projection,
verify the authenticated manifest hashes once the app has settled. The
projector emits Obsidian-stable Base serialization; reopening a freshly
projected Base must not change its managed-file hash. Treat any such change as
a projector compatibility defect, not an acceptable normalization step.

Use `review_safe` by default. It hides values marked sensitive and source-lineage identifiers, omits sensitive relationship edges, and applies the same sanitizer to note bodies, relation labels, filenames, URIs, and user-controlled frontmatter. It scrubs recognized credentials plus common absolute local paths. Non-sensitive evidence remains readable. Use `operator_full` only when the user explicitly requests it and the vault is confirmed private.

Source-note and page-image previews have separate owner-display approval. The
feature branch can configure or revoke it, but publication remains disabled
until native qualification; saved approval is not proof of a working viewer.
Neither `operator_full` nor the `20 Evidence/` directory grants permission to copy
archive originals into the vault. Read the [owner-navigation boundary](../../tedd/references/source-backed-memory.md#owner-navigation-and-privacy)
before discussing preview support. Ordinary projection and recall remain source-body-free.

At v4 scale, projection is a bounded review cockpit rather than one Markdown file per historical revision. It includes the complete current state through 7,500 records. Above that boundary, it selects up to 1,500 priority roots by collection, conflict state, recency, and stable ID, adds their forward-reference closure, and reduces the root set as needed to remain within the 7,500-record bound. It renders one dashboard plus bounded record notes and Bases. Historical revisions remain available through exact `history` or revisioned `get` commands; `export` contains current state only.

The generated vault subtree contains:

```text
00 Dashboard.md
10 Entities/
11 Claims/
12 Incidents and Events/
13 Open Loops/
14 Decisions/
15 Releases/
20 Evidence/
21 Receipts/
22 Observations/
30 Bases/
40 Review Actions/
```

The dashboard links the generated Bases and explains how to read the view. Start
with an entity note. For permitted records, it puts relationships, decisions and
commitments before recorded observations, while keeping remembered statements,
recent activity and evidence distinct. Observation
notes preserve what was recorded without presenting it as an accepted current
claim. Claims can link through their recorded observation to compact evidence;
decisions, events, loops, receipts, and entities link directly to their permitted
evidence. Missing or hidden references remain plain unavailable text, never a
dangling or guessed link.

Entity note-backed summaries are limited to 25 rows per section and say when they
are shortened. When a selected section has more than five rows, the first five
remain visible and the rest appear in one collapsed native callout. Expanding the
callout does not select new records or alter links. Every omitted permitted child note still links to the entity, so
backlinks and the local graph can reach it. Bases retain matching claims,
observations, events, decisions, open loops, and entities. Receipts do not
currently have a dedicated Base; omitted receipt notes remain reachable through
backlinks and the local graph. Selected relationships are not shortened; their
complete selected set remains in the note, with rows after the first five folded
rather than discarded. Their wording preserves stored
direction, predicate, status, source level, and evidence; the projector never
infers an inverse relationship or unstored approval or ownership.

Every new navigation surface uses one privacy predicate for the record,
relationship, endpoints, owner/about references, reverse indexes, note path, and
Base row. Content unavailable in `review_safe` cannot create a new backlink,
filename, relationship, owner/about link, observation artifact, or
navigation-specific count. Existing aggregate dashboard totals can still reflect
canonical collection counts. This presentation rule does not replace canonical
scope checks.

The canonical `open_loops` collection stores complete loop episodes across their lifecycle. The `open_loops.base` projection separates them into Active, Candidates, and Resolved views, while the dashboard reports each count and routes readers to the commitments table; choose Resolved there to review completed work. A resolved loop stays in the same canonical collection with `status: resolved`, `resolved_at`, and its evidence-backed resolution; do not create a separate truth store for closed loops.

The native tables use one clickable wording column and at most four supporting
columns. Their headings describe the question a reader is answering (such as Name,
Context, State, Source, Next step and Outcome) rather than exposing internal field
names. Source labels describe how a record was saved; they do not prove its truth.
Candidate authority and conflicts remain visibly unresolved even when a stored
status resembles acceptance.

## Optional local reading style

Generated Markdown notes carry the constant `tedd-reading` class. Content remains
complete without styling. An owner may experiment with the following Reading-view
snippet to hide Obsidian's redundant inline filename and properties block while
leaving the Markdown title and body visible:

```css
.markdown-preview-view.tedd-reading .metadata-container,
.markdown-preview-view.tedd-reading .inline-title {
  display: none;
}
```

This repository does not install or enable the snippet, write `.obsidian`, or
change a theme. Enable or disable it through Obsidian's normal CSS-snippet control.
Source mode and the Properties sidebar keep the metadata accessible. The selectors
were checked in a disposable Windows vault with Obsidian 1.13.7, its built-in dark
appearance and an approximately 1024 × 800 window. Other themes and hosts remain
unverified; disable the snippet if it does not suit the local view.

Projection materializes the bounded hot set and its forward-reference closure through a read-only SQLite connection; rendering and publication occur while that connection remains open, without a canonical write transaction. It takes its own projection lock and refuses to regress a manifest to an older watermark. The authenticated manifest records the canonical store schema, compatibility projection-payload schema, snapshot watermark, projection digest, mode, generated paths, and per-file hashes; an incremental run rewrites only changed generated notes and deletes only authenticated stale generated files. `canonical_store_schema_version` describes the live source of truth, while `projection_payload_schema_version` describes the bounded compatibility view passed to the renderer. This keeps a slow vault update from holding a canonical write transaction, misleading an operator about the canonical version, or blocking other agents' captures.

An intentional canonical restore may legitimately have a lower watermark than the existing projection. Run the ordinary projection once; it safely returns `status: stale_projection_skipped` without changing files. Review and fully validate the restored canonical graph, then copy `projection_digest` from that skipped result. This digest covers the bounded projection payload and is not the SQLite file hash or a value returned by `doctor`. Bind the one-time regression to that exact payload:

```bash
python3 scripts/knowledge_graph.py project --mode review_safe
python3 scripts/knowledge_graph.py project --mode review_safe --force-current-digest "<projection_digest from the skipped result>"
```

The projector rejects a mismatched digest; do not use this override for ordinary stale-work races.
