# Preserve selected workflow sources

Use this reference before source acquisition or memory binding. For a disputed
memory, start with [checking saved sources](source-backed-memory.md). Browser,
Google and source-MCP operations also need [host integration](source-host-integration.md).
Viewing and deletion use the separate [maintenance procedure](source-viewing-and-maintenance.md).

## Preserve sources during already-authorized work

Once the user has enabled collection, consider only inputs Tedd actually reads
or sends in the authorized workflow. Reading a file is not a reason to keep it.
Retain evidence for a durable fact, decision, outcome or diagnosis; relevant
conditions, contradictions, rejected alternatives or unresolved questions; or an
explicit request to keep the source. Name that purpose in the existing task
context; do not add a score or another ledger. A purpose never grants access.

Choose the authorized input, read through a qualified route, establish that
purpose, then reuse a verified reference or preserve enough context to check it
later. Bind memory only after publication. “Use once” overrides standing retention
approval before either write. Remove only Tedd-created temporary copies when done
or cancelled, never the user's original. Keep admitted evidence if memory binding
fails. Do not add a poller or collect unrelated tabs/activity. Successful captures
can stay quiet; report meaningful exclusions, partial coverage and failures.

- Prefer a direct provider export. A qualified selected-DOM snapshot is an observed
  view, not a provider-original transcript. Copy tool-returned bytes directly
  through the host bridge before summarizing. Never retype, reconstruct, or
  “improve” the original with a model. Unknown metadata stays unknown.
- Preserve delivered outbound messages, not unsent drafts. Mark partial history,
  unavailable attachments, and observed ordering honestly. Available reply,
  quotation, forwarding, attachment, and document-version links belong with their
  source units; missing relationships are not guessed.
- An approved original may be in a shared or synced folder. The archive must not
  be: keep originals, reading copies, and manifests in the approved private local
  root outside Git, Obsidian, cloud sync, network shares, and canonical graph storage.
- For PDFs, preserve original bytes and separately bound extracted/page readings.
  Use the qualified reader's byte digest, page count, and visual inspection before
  publication. Do not execute embedded content. Screening is defense in depth, not
  proof that every arbitrary PDF is secret-free. Refuse unsafe or uninspectable inputs.
- Explicit pause, exclusion, or closure stops capture. Waiting for the next reply
  during authorized work is not a pause. On resumption, recheck the current scope
  and workflow state; do not backfill activity that happened while paused.

Choose the smallest useful source, not just the material that supports a conclusion.
Keep substantive alternatives and negative evidence. Skip incidental tool progress,
unsent drafts, unrelated page sections, mailbox history, and repository-wide dumps.
An uncommitted code file is a saved working-copy snapshot, not an invented commit.
For a report quoting a primary source, keep the report's assistant authorship and
the exact primary-source relationship distinct; neither promotes the other.

### Source routes and their limits

Check the loaded command's help and the host's qualification before using a route.
Missing tools or helpers require separately authorized setup; this procedure does
not authorize an installation change.

| Source used in the workflow | Interface | Important limit |
| --- | --- | --- |
| Selected local UTF-8 text, Markdown or code | `intake-file` / source MCP `intake_file` | Purpose-bound exact byte range or justified whole file; no scripts are executed. |
| Delivered tool result | `capture-file` | Exact supplied bytes; the caller must establish actual delivery. |
| Selected plain web text | `capture-file`, kind `web` | An observed text snapshot, not HTML extraction, a whole-page capture, or a provider original. |
| Saved email | `capture-file`, kind `email` | Single-part `text/plain` only. Multipart mail, HTML, and attachments are refused. |
| Generic support chat | `capture-chat` | Requires a qualified direct export/selected-view bridge; preserve attribution and partial history. |
| Selected local PDF | `intake-file` or legacy `capture-pdf` | Intake preserves the whole file and requires `--whole-file-reason`, plus the qualified reader and visual check below. No PDF-page selection for acquisition. |
| Selected local DOCX, XLSX, PPTX | `intake-file` or legacy `capture-file`, with the host reader | Intake preserves the whole file and requires `--whole-file-reason`; no section/sheet selection for acquisition. Saved readings remain partial. A separately qualified LibreOffice helper can render selected layout pages. |
| Google Docs, Sheets, Slides | Optional source MCP adapter | Exact REST observations, task-linked access, current permissions, and the [trusted Codex lifecycle hooks](source-host-integration.md#source-endpoint-and-lifecycle-hooks). The ordinary CLI has no Google session. |
| Downloaded Google DOCX/XLSX/PPTX exports | Host-configured source MCP `import_exports` | One private owner confirmation binds an ordinary batch. Provenance is owner-confirmed, not provider-verified. No default installation enables this route. |
| Standalone image | `intake_file`, review acknowledgment, then preservation | Requires `acknowledge_review` and qualified image helpers; see the image procedure below. |

Use only a route qualified for the current host and available in the loaded
package. Successful source acquisition does not establish complete document
coverage or independent provider provenance. Browser-export imports use the
owner-confirmed policy in [host integration](source-host-integration.md#owner-confirmed-browser-exports).

If a source cannot be retained, an authorized compact graph capture may keep its
external link only. Say that no local copy exists; do not return an archive receipt
or claim the link will remain available. Access to a cloud file alone does not
authorize permanent retention. Never bypass a provider restriction by downloading
another format or switching viewers.

Apply the same selection rule to different arrival paths:

- **Upload or desktop file:** use the exact readable path delivered with the task,
  whether it names the original or a Codex-managed copy. Both use `intake_file`
  without a dedicated attachment API. Preserve that file even if it differs from
  the prepared upload; do not substitute a same-name file or extracted text. If no
  readable path or supported byte transfer is available, report that limitation.
  A screenshot visible in the conversation does not prove the original file is
  accessible or preserved. Do not scan attachment storage or describe a merely
  referenced image as inspected.
- **Folder or code:** listing a folder is not a bulk capture request. Keep the
  relevant function and needed caller/config/version context, not neighboring
  files. Skip dependency trees, build output, caches and credentials by default.
  A dependency bug can justify one necessary passage and lockfile entry, not the
  whole `node_modules` tree. Do not follow links outside the authorized root.
- **Shared Google link:** browser export is the intended simpler retained-copy
  route, after the intended host's acquisition and cleanup checks pass.
  A shared temporary download is not made private retroactively by copying it;
  use it only under the owner's ordinary-content cache exception.
  Use the optional API only when separately configured and authorized; a provider
  denial cannot select a weaker fallback. Never fetch excluded sheets/sections
  and strip them afterward.

Preserve a sufficient selection, including separated exceptions, attribution,
headers, units and necessary dependencies. A small file is not automatically worth
saving whole. `intake-file` reads an exact half-open byte range
for text, Markdown and code. `capture-file` remains the legacy whole-input command.
Do not reconstruct excerpts or present excerpt-local line numbers as original-file
locations. A partial passage keeps its actual acquired digest and byte container,
not an invented whole-file hash. A retained copy supports later historical checking
under local permission; it does not promise knowledge of remote permission changes.
Existing provider-checked sources keep their current-access requirement. Missing
context or link-only evidence stays explicitly incomplete, even when a newer
source or a later model suggests a correction.

### Select local evidence without bulk capture

Use `intake_file` on a qualified source endpoint with the reviewed
[prompt/turn hooks](source-host-integration.md#source-endpoint-and-lifecycle-hooks).
The trusted prompt hook accepts an absolute path in backticks, double
quotes, on its own line, or in Codex's file-list entry (`## filename: path`).
Attachment entries enroll files, never whole folders; labels and quoted/fenced
source blocks cannot enroll other paths or Google links. Model arguments cannot
enroll another input. An explicitly selected folder still permits only selected
descendants, not scanning or archiving the tree.

Managed copies under the host's `attachments/<entry>/<file>` directory use the
same reader; other `.codex` state and credential paths stay excluded. A host with
a non-default `CODEX_HOME` must explicitly pass that same value in the source MCP
server's `env` configuration; Codex does not forward it automatically. Do not change
an installed configuration without approval. Tedd never removes app-owned copies.

Supply `--purpose fact`, `context` or `keep` for the retention reason. For example,
the approved handbook bytes `[120:450]` use these additional `intake-file` flags:

```text
--input <exact-user-selected-path> --metadata <private-metadata>
--acquired-sha256 <digest-of-those-330-bytes> --workflow-state active
--purpose context --byte-start 120 --byte-end 450
```

Include the same registered session/scope flags as the local example below.
For a necessary dependency passage or lockfile entry, also use
`--relevant-generated`; only bounded byte selections receive that exception.
Credentials, Tedd's managed stores and projections remain excluded. Whole text,
PDF or Office inputs instead need `--whole-file-reason` explaining why all their
contents are needed and permitted. There is no whole-Office/PDF exclusion filter:
do not acquire excluded pages or sheets and remove them afterward.

The reader preserves selected bytes directly. Line numbers are relative to the
named `bytes[start:end]` container. Separate needed passages can be preserved as
separate representations of that path; never imply that different acquisitions
prove one provider revision. Local identity follows the exact path, not a title or
an inferred rename. Old citations and manifest formats remain unchanged.

`--use-once` wins over every purpose; no purpose also skips preservation. The prompt
hook additionally recognizes explicit “use once” and “do not save/retain” controls,
so omitting the tool flag cannot undo them. Its conservative refusal covers selected
local sources in that task, including prior selections and new memory bindings.
An old source without a mappable origin is not newly bound while such a refusal
is active. Historical authorized reads remain available; nothing is erased.
This is a bounded safeguard, not a complete language-understanding system; Tedd
must honor differently worded exclusions too. Do not restart the endpoint to clear
a refusal. Restart loses in-memory selections, not the user's instructions.

Quiet success is fine. `not_preserved` names a skipped purpose, temporary-only use,
excluded input, generated material or missing context selection. Report relevant
missing evidence; do not turn that result into a memory binding. The command never
deletes the user's input. Clean only exact Tedd-created disposable copies.

### Review a standalone image before keeping it

Check that the loaded endpoint advertises `acknowledge_review` and has an image
helper qualified for this host. Tool availability alone does not establish that
the host can process the required format.

Use the same task-selected path, purpose, whole-file reason, metadata and scope
as local intake. Identify the format from bytes, not its extension. The supported
format set is PNG/APNG, JPEG, WebP, AVIF, GIF, BMP, TIFF, HEIC/HEIF, ICO and restricted
static SVG; each retains its format-specific limits. This does not expand the
older embedded-Office format set. A missing or unqualified helper refuses the
route; renaming or converting the file is not a way around that refusal.

First call `intake_file` with `--review-only` to list the image units. This
inventory does not mean their pixels were reviewed or the original was saved.
Then repeat the call with `--review-only --image-unit <position>`, using a
one-based position from that inventory. Inspect the returned image, then call
`acknowledge_review` with its exact `review_id`, not an `argv` array.
A receipt binds the delivered pixels, original bytes, task, audience
and viewed region. Never acknowledge pixels you did not inspect. Every retained
frame, page, item, auxiliary plane or icon variant needs its own review. A cropped
container view may also require `--image-view reconstruction` to inspect the
otherwise hidden image area. The endpoint refuses preservation while required
coverage is missing. Do not replace these receipts with `--visual-review-sha256`.

An overview fits within 2048 × 2048 pixels. It is useful for layout, but does not
prove that small wording or numbers were read. Request native detail with
`--image-rectangle left top right bottom`: zero-based coordinates in the oriented
image, including the left/top edges and excluding the right/bottom edges. Keep
each detail view within 2048 pixels per dimension, and send the four coordinates
as separate `argv` strings. Review the detail needed to interpret small text or
symbols; an acknowledged overview alone does not prove they were read. Check
the returned `output_size`: an overview can shrink to fit the PNG budget. Native
detail is not silently rescaled and can be refused by PNG or MCP response byte
limits; select a smaller relevant region when needed. Keep the stated scale,
color/HDR and animation-timing limits when describing evidence.
Auxiliary planes are not calibrated measurements. SVG uses a fixed approved font;
active content, external resources and unsupported constructs are refused.

For owner-only material, include `--owner-review` on the inventory, selected-view
and preservation calls, with the separate approved private-viewer permission.
The human reviews and confirms there; model acknowledgment cannot stand in for
that confirmation. Missing owner
display is not permission to send the pixels to the model.

Keep declared metadata categories and sensitivity restrictions separate from
pixel review. JPEG/HEVC decoders can ignore bytes inside encoded image data.
`encoded_bytes_not_exhaustively_inspected` discloses that limit: those bytes may
remain in the exact private original, but Tedd must not claim to have inspected
them. Structural, declared-metadata, native-warning and every-item checks still
apply. AV1 retains its separately qualified strict validation.

After review, call the same intake without review-only selection to preserve the
whole permitted original. Read `status`, `stage`, `reason` and the returned receipt.
Acquired, decoded, rendered and reviewed do not mean saved; preserved does not mean
linked to a memory. If binding fails, retain and report the valid source receipt
rather than claiming a completed memory. Use-once and cancellation still prevent
new retention, and a changed original requires fresh review.
Review receipts also expire with the active turn. Preserve the source only while
the exact-byte review and task authority remain valid; an expired acknowledgment
is not a reason to bypass the check. Image preservation creates no OCR or vector
index and does not establish that a remembered interpretation is accurate.

### Capture a local document

After collection is explicitly enabled, `configure --allow-local-documents` grants
one exact task scope access to ordinary local retained copies. It does not start
collection, authorize sensitive sources, or enable cloud capture. Pass the same
configuration and session/scope flags as below. `--deny-local-documents` removes
that access without erasing history; this differs from pausing collection.

```bash
python3 scripts/tedd_sources.py --config "<approved-config>" capture-file \
  --input "<exact-workflow-file>" --metadata "<private-capture-metadata>" \
  --acquired-sha256 "<digest-of-acquired-bytes>" --workflow-state active \
  --consumer-id consumer_example --subscription-id subscription_example \
  --producer-agent-id agent_example --task-id task_example --producer-run-id run_example \
  --subject-id entity_example
```

The host bridge supplies the closed metadata contract, not the source body or a
user annotation for each file. It binds the registered scope, task/run/interaction,
logical source, exact observed snapshot, acquired representation, and reader.
For this local route, details identify provider `local`, the stable local principal,
`retained_copy` policy, and `ordinary` sensitivity. Preserve known title/time/version
metadata; leave unknown values unknown. The input digest must match the bytes read.
Use the packaged [metadata contract](../../obsidian-knowledge-graph/scripts/tedd_knowledge_graph/source_contracts.py)
and [conversion boundary](../../obsidian-knowledge-graph/scripts/tedd_knowledge_graph/source_documents.py)
when implementing a host bridge; never reconstruct an original from a model summary.

These text-like inputs have an 8 MiB acquisition ceiling. Unsupported encodings,
overlong units, secrets, and generated projection material are refused rather than
silently truncated. A renamed source keeps its identity only when that identity is
known; a title match is not enough. Verify the opaque receipt, then remove only
approved disposable staging files. Successful source capture does not itself add
a memory claim or alter retrieval.

For an explicitly approved local DOCX/XLSX/PPTX original, add
`--reader-python "<qualified-python-executable>"` to `capture-file`, use its exact
kind, and identify `local_file` acquisition and `original` representation. These
files share the configured PDF byte ceiling. The bounded worker screens every XML
part before preservation; macros, opaque embedded files, unsafe relationships
and uninspectable packages are refused. Ordinary Mac Office print-ticket metadata
is accepted only after bounded structure, field and text checks; it is not document
evidence and no printer driver runs. Windows/private-driver binary settings remain
unsupported. ZIP comments, extra fields and ZIP64
containers are not supported. Do not strip them to bypass refusal or label
a downloaded Google export a local original. A missing worker leaves capture unavailable.
Saved formulas are not recalculated. Locations refer to the saved package, not a
live Office or Google document. Saved Office readings remain partial and do not
contain rendered layout; preserve their context gaps when checking a memory.

New Word readings with footnote/endnote callouts use `tedd-ooxml:7`. A marker such
as `[footnote:7]` preserves the source's internal note ID (not necessarily its
printed number) and matches a saved location such as `word/footnotes.xml:7`.
Read that exact note before interpreting the qualified statement.
`note_context_unavailable` means a referenced note has no readable saved text;
do not substitute a nearby note. Existing originals, readings and citations remain
unchanged. Layout and other partial-reading limits still apply.

Word readings containing tracked moves use `tedd-ooxml:8`. Their `moveFrom` and
`moveTo` labels identify proposed moved wording, not accepted final text. Keep the
`unresolved_suggestions` warning when checking a claim; older readings are unchanged.

Image-bearing Office files require Pillow in the host-selected worker Python, not
in Tedd's core runtime. Do not install it silently. Supported embedded formats are
restricted PNG, JPEG, WEBP, AVIF, GIF and BMP, subject to the installed codecs.
PNG keeps its strict chunk/stream checks; the other formats use bounded standard
decoding with format and exposed-metadata checks. Numeric EXIF dimensions,
resolution and orientation, plus bounded standard RGB/gray matrix colour profiles,
are validated rather than categorically refused. Text in accepted profiles still
passes credential screening. GPS, maker notes, XMP, comments and unknown metadata
remain refused. These checks do not detect all hidden encoded data. New image
readings use `tedd-ooxml:6`, `tedd-ooxml:7` with Word note callouts, or
`tedd-ooxml:8` with tracked moves;
earlier saved readings and originals are not rewritten.
Animated GIF/WEBP/AVIF must decode every frame; each receives a separate anchor.
Animated PNG, TIFF, HEIC/HEIF and SVG remain unsupported. These are embedded-image
capabilities, not a standalone-image intake route.

For an authorized Office original, first call `intake_file` with the normal
input, metadata, digest, purpose, whole-file reason and session flags, adding
`--review-only`. It returns partial text and embedded-frame units without saving
anything. For every `image_page` unit, repeat that call with
`--review-only --image-unit <position>` and inspect the returned MCP image.
The inventory returns all `unit_count` units or fails at its output limit; it is
not paginated. A truncated tool display is not proof that all images were reviewed.
Its position is a reading-unit number; its location names a package part and
frame, not an Office page number. Inspect every frame of an animation. Timing
remains an explicit gap, so a timing-dependent claim needs further evidence.

Only after this review, remove the review flags and supply the exact original's
`--visual-review-sha256` to preserve it. Decoding, a digest match or text extraction
is not visual approval. If necessary layout or visual context is unavailable,
refuse capture. Do not strip a refused part or call a converted derivative the
original. Saved readings retain their context gaps; existing text-only Office
inputs need no image decoder. Later, use `render_image` on the exact saved frame
URI; combine the publication receipt's archive/scope/manifest with that returned
unit's `anchor_id`; do not guess it from the frame number. No reacquisition is
needed. Previews contain decoded pixels without original metadata, animation
timing or color-profile management, not a guarantee of display-color fidelity.
Image orientation is applied before producing metadata-free pixels; colour profiles
are preserved in the private original but not applied to these previews.
The CLI equivalents require the qualified
`--reader-python`; send their PNG result directly to an image bridge, never a log
or text prompt. Sensitive human-only evidence uses `owner_view` instead.

### Check a document's layout

On a qualified host, add `--review-only --document-page <number>` to the normal
Office intake call to inspect one page before saving. Repeat for the pages needed
to check the evidence. Rendering alone does not approve preservation; the same
exact-byte visual review and retention requirements still apply.

For a saved Office source, source MCP `render_document` accepts its exact
`--source-uri`, `--document-page` and session/scope flags. It reads the archived
original, even if the external file was deleted. The response names the selected
page and page count separately from the original source reference. That page is
a **LibreOffice rendering**, not a saved Office page anchor or a new original.
Fonts, pagination and calculated cells may differ from Microsoft Office. Do not
replace saved wording or formula evidence with this interpretation. Saved readings
and their coverage gaps remain unchanged; one rendered page is not a complete
document review.

The helper uses a fresh private profile with macros, active content and link
updates disabled. Unknown executable fields and formula functions are refused
before conversion. Only a small calculation subset is supported; an inert text
reading may remain available when rendering is refused. Conversion has process,
memory, input/output and time limits, not a filesystem quota or an OS security
sandbox. Temporary conversion files are removed after use or cancellation.
`cleanup_incomplete` means a private temporary folder remains and needs attention;
do not retry that unchanged condition or claim that cleanup succeeded.

These helpers are optional host software, not new plugin Python dependencies.
Qualify the approved binaries and private directory before configuring them;
do not install software or alter an existing endpoint merely to open evidence.

An unchanged local reread in the same verified task/run/source context reuses its
saved reference after fresh permission and integrity checks. Changed bytes,
reader, representation, task or scope retain distinct history. Earlier encounters
and exact replay/conflict rules remain intact; no history is pruned to make room.

`tedd_sources.py` always requires `--config`. `status` is read-only. `init --enable
--archive-root <approved-root>` is a one-time explicit configuration action, not a
collection daemon. Scoped commands require the six session/scope flags shown above.
Personal work uses an ordinarily registered private task subject, not a guessed
client or global subscription.

After explicit approval, `configure --max-chat-bytes 200000` lowers future chat
captures to 200,000 bytes; pass the same configuration and session/scope flags.
`init` and `configure` also accept `--max-pdf-bytes`, `--max-manifest-bytes`, and
`--max-reading-bytes`. Values must be positive. Defaults are also hard caps:
64 MiB per PDF, 8 MiB per chat batch, 1 MiB per manifest, and 32 MiB per reading.
A 100 MiB PDF ceiling is not supported. Omitted settings stay unchanged
on re-enable or configuration. Lowering these limits neither changes the total
archive quota nor makes saved sources unreadable. Refuse oversized new captures;
never truncate them or delete history to fit.

Stable-ID chat exports reuse identical message bytes within the same scope. Each
saved version reconstructs the exact acquired export, including formatting and
order: no model rewrite or chain of earlier versions. Edits create new objects;
reordering can reuse unchanged ones. If any message lacks an ID, preserve a whole
snapshot instead of guessing identity.

For a qualified host bridge, `capture-chat` requires the direct private `--input`,
closed `--metadata`, exact `--acquired-sha256`, and `--workflow-state`.
`capture-pdf` additionally requires `--reader-python`, `--visual-review-sha256`, and
`--visual-pages`. The host supplies actual acquisition metadata; source text cannot
choose the store, consumer, scope, authority, or workflow state. Use `--help` for
the exact syntax. An unqualified commercial widget or missing reader is a stated
capability limit; do not replace it with a whole-browser dump or manual transcript.

Keep the graph concise. Bind a normal capture's `source.uri` to the exact returned
archive reference after durable publication. Pass `--source-subscription-id` and
each `--source-subject-id` on both graph dry run and apply. Keep the existing
authority ceiling, reviewed fingerprint, private plan file, and concurrency checks.
Archive originals never go into graph payloads, ordinary recall packets, or Obsidian.
An owner presentation is a separate derivative, governed by
[owner-viewing permissions](source-viewing-and-maintenance.md#owner-navigation-and-privacy).
