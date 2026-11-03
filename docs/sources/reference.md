# Source reference

This page lists supported formats, command requirements and limits. For tested hosts
and installation status, use [qualification and availability](qualification.md).

Start with [using saved sources](usage.md) for everyday requests, or
[source setup](setup.md) for service configuration and the registered identifiers
required by the commands below.

## Representations and coverage

| Input | Preserved representation | Reading limits |
| --- | --- | --- |
| Local text, Markdown, code | Selected UTF-8 bytes or justified whole file | Byte offsets are half-open: start included, end excluded; code is not executed |
| PDF | Exact whole original | Bounded text/page readings with visual review; no acquisition-time page exclusion |
| DOCX, XLSX, PPTX | Exact whole OOXML original | Partial structured readings; optional separate layout rendering |
| Embedded Office images | Images inside the preserved document | Restricted PNG, JPEG, WEBP, AVIF, GIF and BMP when the reader has the required codecs; review every animation frame |
| Standalone images | Exact whole original, including permitted metadata | Separate reviewed PNG views; format, constituent, detail and resource limits below |
| Google API | Version-bound structured REST observation | Partial Docs/Sheets/Slides content, with current-access checks |
| Owner-confirmed Google export | Exact exported DOCX/XLSX/PPTX bytes | Owner-confirmed origin/account; provider revision and current access are unknown |
| Selected web text | Exact supplied text observation | No automatic whole-page acquisition or browsing-history collection |
| Saved email | Single-part `text/plain` | No multipart, HTML or attachment extraction |
| Qualified chat acquisition | Direct export or selected-view observation | Preserve attribution and gaps; no universal live-support connector |

Standalone image support does not expand the embedded Office format set.
Legacy DOC/XLS/PPT and unsafe or uninspectable packages
remain unsupported. A supported extension alone does not guarantee that a file
will pass screening.

Spreadsheet readings preserve formulas and saved cached values without
recalculation. A selected-sheet export is a separate route limited to one literal
sheet without formulas or defined names. Optional LibreOffice rendering is a
derivative: fonts, pagination and calculated cells can differ from Microsoft Office.
Neither a rendering nor extracted text replaces the acquired original.

### Supported image formats

Standalone images use the `image` source kind. These format names describe
bounded readers, not support for every codec variant or file with that extension.

| Format | Retained parts and useful limits |
| --- | --- |
| PNG / APNG | Still image, or animation frames and any separate poster; review every retained part |
| JPEG | Still image; encoded bytes are not exhaustively inspected |
| WebP / GIF | Still images or animation frames as applicable; every retained frame needs review, and previews do not reproduce timing |
| BMP | Restricted uncompressed `BITMAPINFOHEADER` / `BI_RGB` files; private driver and profile extensions are refused |
| TIFF | Bounded classic TIFF pages; unknown tags, private blobs, SubIFD trees and unsupported compression are refused |
| AVIF | Supported still items, auxiliary images and bounded sequences; requires the qualified strict AV1 helper, and unsupported or over-budget sequences can be refused |
| HEIC / HEIF | Supported image items and auxiliary planes through the qualified HEIF reader; encoded HEVC bytes are not exhaustively inspected |
| ICO | Each icon variant, including variants with the same dimensions, is a separate review unit |
| Static SVG | Restricted inert SVG with a fixed approved font; active content, external resources and unsupported constructs are refused |

Exact originals stay in the private archive. Metadata-free PNG views support
overviews and native detail after orientation is applied. Removing metadata from
a view does not remove it from the original. Declared description, identifying
and location metadata must satisfy the source's sensitivity policy before saving;
pixel review does not override that check. These views do not create an OCR or
vector-search index.

Every retained constituent and required uncropped view needs a separate review
receipt; decoding or displaying it is not approval. Image positions use one-based
`--image-unit` values. Native detail uses `--image-rectangle left top right bottom`
in the oriented image's zero-based coordinates, including left/top and excluding
right/bottom. Pass the four coordinates as separate MCP `argv` strings. Use
`--image-view reconstruction` for a required uncropped container view.

| Image boundary | Hard maximum |
| --- | --- |
| Encoded original | 64 MiB; a lower configured document byte limit still applies |
| Dimension | 131,072 pixels |
| One constituent | 128 million pixels |
| Total image inventory | 256 million pixels and 256 constituents |
| Overview or native-detail output | 2,048 pixels per dimension; larger detail regions need separate selections |
| Rendered PNG | 8 MiB before transport encoding |
| Source MCP response | 8 MiB including the encoded image and response fields |
| Combined worker/helper operation | 2 GiB memory budget and one 30-second deadline |

These are ceilings, not promises that every file below them can be processed.
Format checks or helper/resource requirements can refuse an input earlier.
An overview may be reduced below 2,048 pixels to meet the PNG budget; read its
returned `output_size`. A native-detail selection is never silently rescaled.
It can exceed a byte or response limit even within the dimension ceiling, in
which case choose a smaller relevant region. A PNG that fits its own byte limit
can still exceed the encoded MCP response budget.

The Linux native-helper route requires a non-root process, an unreadable sealed
in-memory executable, and the existing kernel `suid_dumpable` policy set to zero.
Unknown or incompatible prerequisites cause a refusal; the helper leaves system
settings unchanged. The shared budget reserves memory for the executable copy,
the worker and the native child. These limits are not a security sandbox.

JPEG/HEVC can preserve bytes that a decoder ignores inside encoded image data.
The response discloses `encoded_bytes_not_exhaustively_inspected` rather than
claiming those bytes were reviewed. Declared metadata, structural, native-warning
and every-constituent checks remain required; AV1 keeps its qualified strict
validation. Color, HDR, timing and auxiliary-plane interpretation also have
explicit limits: previews use 8-bit color without calibration or HDR reproduction,
and auxiliary-plane values are not calibrated measurements. The [image procedure](../../plugins/tedd/skills/tedd/references/source-preservation.md#review-a-standalone-image-before-keeping-it)
defines review and separate owner/model permissions. Check the [archive compatibility and rollback requirements](setup.md#choose-the-build-and-preserve-a-rollback-path)
before enabling image preservation.

## Identity and scope

An immutable source reference has the shape
`tedd-source://<archive-id>/<scope-id>/<manifest-digest>#<anchor>`. Use returned
identifiers and anchors. A PDF page, spreadsheet cell and embedded frame are not
interchangeable anchor numbers.

The source scope includes the graph store ID and generation, namespace and exact
authorized subject IDs. Commands also bind the registered consumer, producer agent,
task, run and subscription. A remembered title, email address or source-supplied
identifier cannot replace those bindings.

Local intake derives source identity from the normalized selected path and version
from acquired bytes. It does not infer that a renamed file is the same logical
source. Google identities and owner-confirmed exports have separate provenance
rules. Known excerpt relationships group exclusions and forgetting; they do not
turn owner assertions into independently verified provider identity.

## Limits

| Boundary | Default / hard maximum |
| --- | --- |
| Initial total archive quota | 5 GiB; optionally lower at initialization |
| Text/chat-like acquired input | 8 MiB |
| PDF or Office acquired input | 64 MiB |
| PDF page count | 256 |
| Manifest | 1 MiB |
| Derived reading | 32 MiB and at most 4,096 units |
| Browser export batch | At most 8 files, also bounded by the document acquisition ceiling in aggregate |
| Evidence response | 12,000 characters by default; at most 48,000 and 5 versions |
| Metadata lookup | Up to 5 results per snapshot-bound page |

Acquisition byte ceilings can be lowered without deleting history. The total quota
is set through `init --max-bytes`; that interface cannot change an existing quota.
An operation that exceeds a limit is refused; existing history is kept. Complex
documents may reach a format or safety limit before reaching the byte ceiling.

Capture also enforces a 100,000-file inventory bound, with a 200,000-file
maintenance reserve. These file limits do not guarantee response times. Checks
for excluded sources still need work as archives grow; the
[graph benchmarks](../SCALE.md) do not measure source-archive performance.

## Commands and tools

The CLI requires `--config` before its subcommand. Scoped commands require
`--consumer-id`, `--producer-agent-id`, `--task-id`, `--producer-run-id`,
`--subscription-id` and one or more `--subject-id` flags. `status` and `disable`
omit those session flags. Connection-level Google controls instead
bind the explicitly selected account, client and private credential root.

Ordinary source MCP operations use underscore tool names and an `argv` array
containing only operation flags. Configuration and helper executables are fixed
by the host. `acknowledge_review` instead takes a
`review_id`; `disconnect_google` and `reconnect_google` take empty objects and
request host confirmation. Check the loaded endpoint's tool list before use.

| Task | Source MCP | CLI equivalent |
| --- | --- | --- |
| Preserve selected local evidence | `intake_file` | `tedd_sources.py intake-file` |
| Import an owner-reviewed export batch | `import_exports` | Use the configured endpoint; standalone CLI has no owner-confirmation adapter |
| Find / inspect saved evidence | `lookup`, `inspect` | `tedd_sources.py lookup`, `inspect` |
| Link evidence to a reviewed graph memory | `bind_memory` | `knowledge_graph.py capture` with source bindings |
| Check a memory's evidence | `check_memory` | `knowledge_graph.py source-evidence` |
| Open the owner viewer | `owner_view` | `tedd_sources.py owner-view --private-viewer` |
| Review a standalone image | `intake_file` with review flags, then `acknowledge_review` | The qualified source endpoint owns review delivery and acknowledgment; a standalone CLI render is not a review receipt |
| Read saved image pixels | `render_image` | `tedd_sources.py render-image`; format helpers and access checks still apply |
| Read a saved Office page | `render_document` | `tedd_sources.py render-document`; requires qualified Office layout helpers |
| Disconnect / prepare Google recovery | `disconnect_google`, `reconnect_google` | `tedd_sources.py disconnect-google`, `reconnect-google`; explicit account/client/credential bindings and owner confirmation |
| Restrict / forget / recover | No public MCP tools | `tedd_sources.py restrict`, `forget`, `recover` |

The table shows command names. Use each command's `--help` for complete syntax,
[setup](setup.md) for a first capture, and the
[packaged procedures](../../plugins/tedd/skills/tedd/references/source-backed-memory.md)
for binding, viewing and maintenance. The local CLI runs under the operator's
authority and does not exercise Codex's task-selection or cancellation hooks.
Do not use it to bypass a source operation that those hooks denied. Acquiring
Google content through the API or reading provider-checked evidence requires the
configured source endpoint and its provider session.

## Results and recovery

| Result | Meaning / next action |
| --- | --- |
| `inactive` | Collection is disabled or the workflow does not permit capture; nothing was saved by this attempt |
| `not_preserved` | Selection or retention policy did not produce a saved source |
| `duplicate` | Exact evidence already exists; use the returned receipt |
| `partial` | Inspect individual export receipts: earlier files may have committed |
| `scope_denied` | Scope or authority does not permit the operation; do not widen it to retry |
| `privacy_rejected` | A private file, directory or source fails its privacy checks; correct only the intended approved input or work-file setup |
| `capability_unavailable` | The operation needs a host tool or connection that is missing, unconfigured or unqualified |
| `source_unavailable` | The selected source or reader is unavailable; check source access and reader recovery state before retrying, without substituting different evidence |
| `source_corrupt` | The source or its metadata is invalid, unsafe or unsupported; the operation did not succeed |
| `limit_exceeded` | The operation exceeded a limit; check which one before choosing a smaller permitted operation |
| `reauthentication_required` | The provider rejected the saved grant; renew consent through the configured route |

On macOS, direct native PDF, Office and image reader calls can return
`capability_unavailable` with `reason: macos_worker_boundary_unqualified`.
The native worker's resource/crash-privacy boundary is not qualified; installing
more codecs or retrying unchanged cannot resolve it. No worker parsing or native
launch takes place through that boundary. The parent may already hold approved
source bytes. Composite import results retain their existing `not_preserved`
shape with a capability reason. The [optional isolated Mac reader](setup.md#optional-isolated-mac-reader)
provides a separate route for supported operations; it does not enable the native
worker. Check [current qualification](qualification.md#current-availability)
before configuring it.

Always inspect coverage, warnings and cleanup/recovery fields as well as status.
Successful capture can coexist with incomplete export cleanup. Keep edited files
and pending recovery intact; do not repeat an unchanged blocker or report deletion
until verified. See [viewing and maintenance](../../plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md).

### Image review and preservation results

Image progress uses `stage` in addition to the operation's `status`:

| Stage | Meaning |
| --- | --- |
| `acquired` / `decoded` | Bytes were obtained or decoded; no preservation claim |
| `rendered` | A selected view was produced; acknowledgment is still separate |
| `reviewed` | The delivered view was acknowledged for its exact bytes, task and audience |
| `preserved` | A durable archive receipt identifies the saved original |
| `bound` | A reviewed graph write linked a memory to the preserved source |

`source_review` with `preserved: false` is a review response. For model review,
inspect the returned image before sending its `review_id` to `acknowledge_review`.
Human-only review uses the private viewer's confirmation instead. Receipts expire
with the active turn and cannot authorize changed bytes. Read status, reason and
coverage together; a saved original can coexist with a failed memory binding.
Keep its valid source receipt in that case.

### Google connection results

Disconnect reports the local and provider outcomes separately:

| Field | Meaning |
| --- | --- |
| `local_access_disabled` | Whether this connection is locally disabled |
| `credentials_removed` | Whether its saved credential was removed |
| `provider_revocation` | `not_started`, `sending`, `confirmed`, `proven_not_sent` or `uncertain`; local disable does not prove provider revocation |
| `sender_retired` | Whether that local revocation sender can no longer send |
| `replacement_generation_active` | Whether a newer connection is active when the earlier result is reconciled |

`google_disconnect_cancelled` leaves the connection unchanged.
`google_disconnect_incomplete` can mean local access was disabled while credential
removal remains incomplete. Reconciling the same operation ID returns its existing
outcome; it does not silently retry an uncertain revocation against a replacement
grant.

`google_recovery_awaiting_consent` means the owner approved preparation for fresh
consent, not that access has resumed. `earlier_revocation_uncertain` preserves
that uncertainty. After consent, inspect the saved source through the configured
endpoint to check restored access; no new capture is needed. `source_corrupt`
during a separate capture is not proof of failed sign-in and must not be bypassed
by rewriting source identities. Follow [disconnect and recovery](usage.md#disconnect-and-recover-google-access)
for the warning and owner steps.
