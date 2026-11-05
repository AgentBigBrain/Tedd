# Changelog

All notable changes to Tedd are documented here. Plugin versions follow Semantic
Versioning. Repository-only updates have dated entries without changing the
plugin version. See [current availability](docs/sources/qualification.md#current-availability)
for source and installation status; older entries retain their original test limits.
Tedd commit identifiers from before the public repository was created refer to records
in the maintainers' private archive and are kept as historical references.

## 2026-11-05: Repository updates

- Correct the public scale-study reproduction guidance and explain historical
  commit references retained in the private archive.
- Skip Unix-socket test fixtures when the host denies the required capability.
  Unexpected socket errors and cleanup failures still fail their tests.
- Test child file permissions separately so that check runs when sockets are
  unavailable.
- Skip the untrusted-symlink fixture when running as root, where the graph's
  path policy deliberately trusts that root-owned link. Keep the rejection
  test active for other users.

## 2026-11-03: Repository updates

- Add a DeepWiki link for exploring Tedd's public code, with a badge stored in
  the repository so the image loads without an external badge service.
- Give fresh Windows setup-test folders explicit private ownership and access
  controls before checking setup behavior.
- Keep the optional Python 3.15 check on pull requests and development branches.
  Checks on `main` use the supported Python versions.
- Use standard Intel Mac runners for routine macOS CI after repeated Apple
  Silicon runner-allocation failures.

## 1.2.0-rc.12: 2026-11-03

- Publish Tedd with fresh public history, vendor-neutral provider guidance, and
  installation commands pinned to the public release tag.
- Keep personal hosting procedures, design plans, and private evaluation records
  outside the public package. Document current graph compatibility separately
  from the private migration roadmap.
- Make the saving checks self-contained and explain how to update a pinned Codex
  marketplace installation. Restore executable permissions on the Unix verifier
  and use consistent text line endings across platforms.
- Allocate the Windows handoff buffer and construct its helper path directly
  through .NET, avoiding module discovery during those startup steps.

Graph and source behavior, archive formats, and access rules are unchanged from
RC11. Compatibility remains Python 3.10 through 3.14 on Windows, macOS, and Linux,
store schema v4, and projection format v4. No graph migration or rebuild is
required. Installing this version leaves private profiles, project bindings,
memories, and source credentials in place.

## 1.2.0-rc.11: 2026-10-18

- Save stated preferences as dated, single-subject claims with a condition and
  reason. Preserve earlier values when a confirmed standing preference changes.
- Show scoped preferences at the start of task context, with privacy filtering,
  explicit conflicts, and bounded detail inside the overall response budget.
  Keep the effective value until a scheduled replacement takes effect.
- Preview an optional operator-global preference scope in guided setup. Create
  it only through reviewed capture and approve it as an extra project subject;
  other record types cannot use it as shared context.
- Extend MemoryBench with opt-in suites for real MCP search and delivered
  overdue/preference context. Keep the CLI baseline, reject refused or unstable
  measurements, and add synthetic end-to-end privacy and workflow checks.

Current task instructions take precedence over stored preferences. One-time
exceptions do not change the saved preference. Store schema v4, projection format v4, the
four read-only MCP tools, and runtime dependencies are unchanged.

## 1.2.0-rc.10: 2026-10-17

- Add read-only `loops-review` for overdue commitments in the task's existing
  subscription, plus a short summary in scoped recall. Check scope and privacy
  before applying limits; preserve proposals and unknown owners or dates.
- Keep recent memory records ahead of the overdue summary and report shortened
  or omitted results. Enforce the character budget for the complete disclosed
  context, including metadata.
- Flag new commitments without a checkpoint while allowing unknown dates.
  Ask what to do only when an overdue item affects the task; reads never
  resolve, reschedule, hand off, withdraw, or otherwise change a commitment.

Store schema v4, projection format v4, dependencies, and the four read-only
MCP tools are unchanged. See [overdue-review qualification](docs/CLAUDE_CODE_QUALIFICATION.md#rc10-overdue-review)
for tested source, Windows/Linux/Mac results, and remaining delivery requirements.

Python 3.10–3.14 on Windows, macOS, and Linux remains supported. Source readers
and archive formats are unchanged; no migration is required. The maintainer
approved this source delivery using local cross-platform checks. A release tag
and production installation remain separate.

## 1.2.0-rc.9.2: 2026-10-17

- Add selective milestone saving, with a closing report
  of saved outcomes, candidates, and anything left unsaved. Group compatible
  outcomes into one reviewed capture and validate reusable-fix summaries.
- Add private handoff receipts checked against the capture journal. Preserve
  original authorship on duplicate retries and repair missing receipts without
  repeating a capture. Distinguish verified saves from nothing-to-save assertions.
- Add Claude and Codex reminders for missing handoffs. Honor no-save, read-only,
  and no-file instructions; provide `TEDD_NO_LOCAL_WRITES=1` to silence the
  handoff hooks and disable receipt commands before they access local state.
- Preserve Codex hook input on Windows PowerShell with a bounded pipe adapter.
  Document one-time user-level hook setup for Codex, with project hooks as an
  alternative, while retaining portable packaging and normal hook trust.

Python 3.10–3.14, store schema v4, projection format v4, and the four read-only
MCP tools are unchanged. No migration or new dependency is required. The
[RC9.2 qualification summary](docs/CLAUDE_CODE_QUALIFICATION.md#rc92-selective-saving)
records local Windows, Linux, and Mac checks, native CLI results, required
Codex hook setup, and remaining installation limits. This entry records source
publication on `main`, not a release tag or production installation.

## 2026-10-15: Documentation and setup guides

- Clarify Codex and Claude installation, first-time project setup, and everyday
  memory instructions. Provide the exact RC8 source revision and a focused
  qualification summary with the accepted merge exception and remaining limits.

## 1.2.0-rc.8: 2026-10-15

- Extend task-scoped memory search with explicit trailing prefixes such as
  `logo approv*`. Literal queries and exact IDs retain their existing behavior;
  all terms still must match, malformed prefixes are refused, and privacy and
  subscription rules are applied before result limits.
- Add guided setup for an existing graph, with interpreter checks, a preview,
  fingerprint-bound application, and private backups. Preserve unrelated host
  settings and source-helper arguments; do not initialize memory or enable
  source retention. New Obsidian configurations use `Tedd Knowledge`, while
  existing and custom folder names remain unchanged.
- Return safe structured setup refusals for SQLite failures instead of raw
  tracebacks, distinguishing available corruption and access codes from temporary
  unavailability and preserving cancellation.
- Add Claude Code shared-memory integration with per-user Python and
  graph paths, one read-only endpoint, and explicit session and project binding.
  Keep reviewed saving, authorship, and revision history shared with Codex;
  Codex's source-retention lifecycle is not enabled in Claude. The
  [qualification summary](docs/CLAUDE_CODE_QUALIFICATION.md) records Windows and
  macOS host results, their exact candidates, and remaining limits. Follow the
  [Claude guide](docs/CLAUDE_CODE_SETUP.md) for installation and project setup.
- Remove optional personal contact fields from package metadata while retaining
  public publisher information and required attribution.

Compatibility targets remain Python 3.10–3.14 on Windows, macOS, and Linux,
canonical-store schema v4 and projection format v4. Source archive formats,
readers, and access contracts are unchanged. No migration is required. Setup and
installation do not create or migrate a graph, refresh Obsidian, enable source
retention, or rename an existing knowledge folder.

Source merged through PR 14 at
`3258ad76cb23e376df534554f975688845f7ba0d`. GitHub's billing or spending-limit gate
blocked required CI before tests. This source merge used local Windows, Linux,
and macOS evidence under an explicitly approved, one-time exception to the CI
requirement, with interactive permission handling in the Claude desktop app on
Windows still unverified. The [qualification record](docs/CLAUDE_CODE_QUALIFICATION.md#rc8-source-and-verification)
preserves that exception; it does not change normal release policy or record a
tag, GitHub Release, or production installation.

## 2026-10-02: Development guidance

Available on `main`. These changes update repository guidance and checks, not
the installed plugin or its version.

- Improve the repository-local Python skill with shared CLI/MCP request examples,
  cause-based failure classification, bounded parser controls and focused decoder
  and interface guidance. Shorten generic cards and rename four local references
  to reflect unittest, shared support, subTest cases and reviewed dependencies;
  retain all 45 pinned upstream mappings. Graph/source runtime and lint/type policy
  are unchanged. Allow documentation and boundary checks to verify unstaged card
  renames while retaining missing-link, required-file and artifact checks.

## 1.2.0-rc.7: 2026-10-01

- Add a read-only `search_memory` graph MCP tool for finding older compact
  memories in an existing task subscription. It supplements recent context with
  literal word or exact-ID matching, applies scope and privacy rules before
  result limits, and preserves each result's revision, status and authority.
  It uses the existing index without a migration or new runtime dependency.

Compatibility targets remain Python 3.10–3.14 on Windows, macOS and Linux,
canonical-store schema v4 and projection format v4. Source archive formats,
readers and access contracts are unchanged. No migration is required.
Installation does not initialize or migrate
the graph, refresh the Obsidian projection or enable source access.

RC7 is available on `main`. The private source delivery record
records the completed merge and exact-commit CI. Installation is a separate step;
merging the feature does not update an existing RC6 installation.

## 1.2.0-rc.6: 2026-10-01

- Add on-demand startup for the optional Mac reader's approved virtual machine.
  Synthetic checks cover a cold PNG request, isolated installed-code execution, offline
  reader-image import with quota enforcement, and shutdown of the test runtime.
- Fix partial uploads when rendering Office pages through the isolated reader.
  Accept either empty Docker DNS representation (`null` or `[]`) while continuing
  to reject nonempty DNS settings and enforce the other isolation checks.
- Preserve binary ISO bytes on Windows and compare descriptor and path timestamps
  against their respective baselines. Unchanged files pass on Python 3.14 while
  mutation checks remain enforced.
- Validate Windows Office-generated PDFs on a pinned file handle before reading:
  require a trusted owner, private access and one link, then transfer ownership
  and recheck. Exposed or untrusted output remains refused.
- Improve platform regression tests for private ISO fixtures, Linux descendant
  cleanup and image resampling. Allow 45 minutes for Windows release verification;
  Linux and macOS retain their 30-minute budget.
- Update the source and Mac guides to distinguish basic graph/text use, enrolled
  readers and private viewing. Consolidate current availability and test results
  in the [qualification page](docs/sources/qualification.md#current-availability).

Compatibility remains Python 3.10–3.14, canonical-store schema v4 and projection
format v4. Source archive contracts are unchanged; no migration is introduced.
RC1 still cannot read archives containing the `image` source kind, and rollback
must preserve current restrictions and forgetting exclusions. Installation does
not enable a reader, connect Google or refresh an Obsidian vault.

RC6 was merged into `main` and installed in the recorded Windows Codex setup.
The [delivery record](docs/sources/qualification.md#october-1-main-delivery-and-windows-installation)
distinguishes source CI, installed-file verification and native graph checks.
Optional Mac results cover the stated synthetic workloads, not factory-clean
deployment, all formats, live Google access or private owner viewing. Their
PDF and sleep-check limits remain unchanged.

## 1.2.0-rc.5: 2026-09-20 (staging candidate)

- Put relationships, decisions and commitments before long observation history in
  generated entity notes; fold display rows after the first five without changing
  selected records or links. Simplify dashboard loop navigation and put commitment
  outcomes or next steps before technical detail.
- Give generated Bases one readable clickable column plus bounded, explicitly
  labelled context, state, source, date and action fields. Serialize note-backed
  metadata and view order in Obsidian's stable forms, keep special-root folder
  filters safely encoded and balance the State formula. Preserve candidate and
  conflict warnings and keep same-named entities distinct.
- Add a constant `tedd-reading` class to generated Markdown notes and document an
  optional, owner-controlled Reading-view CSS starting point. Tedd does not install
  the snippet, write `.obsidian`, alter themes or change canonical memory.
- Fail closed when a context target is omitted, sensitive or unclassified; encode
  dynamic Base folder filters safely and label candidate actions as proposed.
- Preserve Python 3.10–3.14 and Windows/macOS/Linux compatibility targets,
  canonical store schema v4, projection format v4 and the existing source-archive
  reader contract. No migration is required. Installation does not refresh the
  Obsidian projection or enable the optional CSS; both remain explicit owner
  actions. Windows file-based quality and isolated semantic review passed. Windows
  Obsidian 1.13.7 preserved all managed hashes after reopening all six tables and
  their commitment views. The empty Candidates view did not qualify populated
  candidate rows. Exact-candidate macOS and Linux gates and exact-commit CI remain
  separate qualification requirements.

## 1.2.0-rc.4: 2026-09-20 (staging candidate)

- Make the generated Obsidian view easier to read without changing canonical
  memory. Add direct subject-to-observation navigation, bounded human labels,
  honest open/proposed/resolved commitment wording, reader-first dashboard
  sections and readable Base links. Hidden records cannot displace visible
  dashboard entries, captured bracket syntax cannot create display-property graph
  edges, and record notes no longer create a dashboard-only graph hub.
- Make coverage-run repository checks pass their local scripts import path
  explicitly while preserving an existing `PYTHONPATH`.
- Preserve canonical store schema v4, projection format v4 and the existing
  source-archive reader contract. No migration is required; explicitly refresh
  the generated Obsidian view after installation. Python 3.10–3.14 and
  Windows/macOS/Linux remain compatibility targets. This candidate has Windows
  file-based verification; native Obsidian acceptance and exact-commit
  cross-platform CI remain separate qualification gates. No source-reader,
  retention or rollback permission is widened.

## 1.2.0-rc.3: 2026-09-20 (staging candidate)

- Make ordinary graph connections understandable in the generated Obsidian view.
  Add privacy-gated observation notes, navigable claim and evidence lineage,
  direction-preserving relationship explanations, grouped person/project
  summaries, bounded-state dashboard metadata, and useful Bases. Keep the graph,
  recall, source archive, and source-preview qualification unchanged.
- Add an opt-in, injectable isolated-reader request manager for PNG, PDF and
  DOCX, with typed selectors, bounded pipes, guest lifetime supervision and exact
  job recovery. Add explicit admission for an already-enrolled private, warm
  macOS runtime; reject missing or changed runtime identities. This is a
  development checkpoint, not general private-document qualification; installing
  the package does not enable this route. Record synthetic ARM64 and Desktop
  fixture evidence, passing manual cold-readiness measurements, and the remaining Desktop Stop,
  automatic-startup, privacy, clean-machine and Windows verification gates.
- Add protected macOS Keychain credential storage without a plaintext fallback.
  Keep live Google authorization and Desktop acceptance as separate checks.
- Refuse unqualified macOS native-reader boundaries explicitly instead of
  applying Linux-style memory limits that fail on Mac or weakening the shared
  resource/crash-privacy contract. Direct native PNG, PDF and Office workers
  remain blocked; the experimental isolated route is described above.
- Bound inspected page selectors before integer conversion and recover owned
  subprocesses when reader-pump startup fails. Add failure regressions and bound
  test-process readiness and cleanup without relying on fast interpreter startup.
- Correct test isolation for Mac filesystem probes and checkout-local virtual
  environments. Update the Mac testing guide to distinguish local text checks
  from blocked native-reader workflows. Preserve the RC2 release bytes and do not
  migrate a graph.
- Keep the Python 3.10–3.14 and Windows/macOS/Linux compatibility targets,
  canonical store schema v4, projection format v4 and existing source-archive
  format. No graph migration is required; explicitly refresh the configured
  Obsidian projection to see the new connections. Installation itself does not
  refresh the vault, collect sources or change access permissions.
- Retain RC2 and its configuration for rollback. Preserve current source
  restrictions and forgetting exclusions, and never substitute older evidence
  for a newer reference. Ordinary recall is unchanged; private MemoryBench and
  backup/restore rehearsals were not rerun for this projection update. Native
  Obsidian acceptance, platform-specific reader qualification and exact-commit CI
  remain separate from local tests and prerelease installation.

## 1.2.0-rc.2: 2026-09-15 (staging candidate)

- Add standalone image evidence to the source archive and private viewer.
  Preserve exact originals after review, with separate views for frames, pages
  and readable detail. Distinguish reviewed, saved and memory-linked outcomes.
  Supported formats and their limits are in the [source reference](docs/sources/reference.md#supported-image-formats).
- Add confirmed Google disconnect and separately confirmed recovery, with local
  access disabled before revocation and protection against stale operations.
  Saved Google evidence remains in the archive but needs current authorized
  provider access to be read again.
- Update the source guides for image review, helper setup, Google connection
  controls and rollback limits. See [source qualification](docs/sources/qualification.md#current-availability)
  for tested hosts and build availability.
- Keep Python 3.10–3.14, canonical store schema v4 and projection format v4.
  Windows/Linux source qualification remains separate from installed acceptance;
  Mac source qualification is deferred. No graph migration or projection rebuild
  is required. Existing source archives remain readable, but RC1 cannot read an
  archive containing the new `image` kind. Preserve a compatible backup and
  current restrictions/forgetting exclusions before any rollback; newer memory
  references must never resolve to substitute older evidence.
- Retain the recorded synthetic source backup/restore and denial-preserving
  recovery checks. Ordinary graph recall is unchanged, so this version preparation
  does not rerun private production MemoryBench or modify live graph data. Package
  installation, configured-helper checks and native smoke remain separate gates.

## 1.2.0-rc.1: 2026-09-13 (staging candidate)

- Give the source-backed memory and skill updates a distinct staging version.
  This candidate is for explicit testing, not a public release or a merge to main.
  Python 3.10–3.14, store schema v4 and projection format v4 remain unchanged;
  no graph migration or rebuild is required. Preserve the previous installation
  and configuration for rollback. Mac source qualification remains deferred.

- Align skill guidance with the production no-`Any` gate and qualified source
  routes. Preserve unknown loop owners and dates, and select browser/native
  surfaces by capability while retaining applicable action confirmations.
  Split graph procedures into packaged setup/recovery, recall, capture/correction
  and projection references while retaining entry-point invariants and anchors.
  Check reference reachability and public handoff fields instead of prescribing
  ordinary wording or direct root links to every Python rule card.

- Make Tedd's instructions take precedence over conflicting Superpowers guidance.
  Continue authorized implementation through verification and repair, isolate
  blockers to dependent work, and reuse scoped approvals. Load memory and delivery
  procedures only when relevant; keep privacy, cancellation, and release boundaries.
  Remove core-skill heading and wording constraints from the repository checker.

- Add an opt-in private source archive and explicit source-backed memory checks.
  Preserve selected text, code, PDF and Office evidence separately from compact
  graph memories. Add actual Desktop attachment intake, authenticated Google
  observations, owner-confirmed browser exports, reviewed memory binding and
  Windows/Linux private viewing. Keep partial readings, provider permissions,
  shared-source forgetting and recovery explicit. The agreed Windows/Linux scope
  passed qualification and separate synthetic staging acceptance; Mac remains
  deferred and native Obsidian source previews remain disabled. Collection and the
  source MCP endpoint are opt-in, not enabled by ordinary plugin installation.

- Refresh the main documentation for source-backed memory. Add a documentation
  catalog, architecture overview, source user/setup/reference guides and a separate
  documentation maintenance policy. Split packaged source instructions by task
  and move early capability probes into `docs/archive/source-memory/`, preserving
  dated evidence and links. Extend the existing documentation checker to recognize
  source CLI examples and reject unsupported subcommands/options.

- Keep known filesystem paths private even when an input labels them as a web
  route. Preserve ordinary routes, explicit web URLs, and caller-supplied path
  rules; test both structured fields and prose with synthetic paths.
- Preserve safe backend error categories when subscription lookup fails, instead
  of incorrectly reporting an unknown subscription. Cover context and inbox
  failures without exposing backend details or leaving a read transaction open.
- Separate current setup guidance from dated release-testing history. Keep the
  prerelease checkpoint and regression records in `docs/archive/releases/`,
  preserving their findings without presenting them as current status.
- Correct the routing scorer's authorized-heartbeat check with neutral synthetic
  regression coverage. The public 12-scenario catalog and historical observations
  are unchanged; those observations do not establish live monitoring behavior.
- Start migration-test competitors at the contested publication boundary, not
  during preparation; preserve no-clobber assertions and propagate worker failures.
- Hold issuance time equal in signed-token transport-parity tests, preserving
  complete token comparisons and read-only state checks across clock boundaries.
- Probe the optional Python launcher instead of treating a Windows Store shortcut
  as a working interpreter. Exercise copied-package reads with the selected Python
  separately from the packaged launcher, retaining graph-state equality checks.
- Isolate release-test temporary storage without changing production privacy
  checks or existing system permissions.
- Add a read-only complete installed-file comparison and a Windows setup guide
  for explicit interpreter binding, upgrade rebinding, and fresh-task acceptance.
- Keep Superpowers working notes out of the proposed merge with ignore rules
  and a tested repository check; preserve local copies and earlier Git history.
- Run the existing CI matrix on `fix/**` pushes. Clarify setup and release
  instructions, including package-preserving MCP settings and the distinction
  between source tests, installed files, and desktop acceptance.
- These repairs leave record selection, runtime dependencies, canonical v4 data,
  and projection format unchanged.
  Previous test packages remain immutable.

## 1.1.0-beta.4: 2026-09-04 (test package)

- Add a private v4 capture-plan file handoff so default `review_safe` CLI output
  can support reviewed apply without printing signed tokens or changing the
  fingerprint, revision, uniqueness, authority, or store-generation guards.
- Correct v4 whole-store watermark help and bound capture recovery during work
  closure; distinguish delivered work from pending knowledge capture.
- Distinguish work requests, conversational invitations, and genuine closings in
  communication and burst handling, without mandatory humor or extra agents.
- Package these changes under a distinct version; the immutable beta.3 test
  package below remains the rollback source. Preserve Python 3.10-3.14 targets
  on macOS/Linux/Windows, store schema v4, and projection format v4. No graph
  migration or rebuild is required. See the
  beta.4 qualification record for dated source,
  CI, installation, and native-host evidence.

## 1.1.0-beta.3: 2026-09-04 (test package)

- Package the reviewed post-beta.2 path-redaction fixes under a distinct
  prerelease version; keep the Codex manifest, portable manifest, and MCP
  initialization version in sync. Do not overwrite the original beta.2 package.
- Preserve Python 3.10-3.14 compatibility targets on macOS, Linux, and Windows,
  canonical store schema v4, and projection format v4. No graph migration or
  rebuild is required. Orchestration and etiquette skills are unchanged.

- Repair Windows packaging fixtures with the existing platform-aware owner/ACL
  helper, including configuration-file permissions. Keep production checks intact.
- Apply the same fixture correction to later MCP subprocess and CLI cursor
  tests exposed by the next Windows run; preserve their diagnostic assertions
  and record each run separately in the qualification ledger.
- Isolate documentation-test Git configuration, templates, excludes, and
  repository overrides; report missing Git as an explicit dependency failure.
- Separate portable MCP document shape from Tedd's approved-launch policy,
  retaining the closed command and physical containment checks.
- Reconcile installation examples and release prose with the dated
  qualification checkpoints, including the failed
  Windows CI run. Label retained design documents as contributor history.
- The repair baseline `dfc7c20` passed the full remote CI matrix, including
  Windows Python 3.10 and 3.14. See the separate
  beta.3 qualification record for versioned-source,
  installation, and native-host evidence; source CI is not native MCP acceptance.

## 1.1.0-beta.2 follow-up: 2026-09-04 (branch source update)

- Share a filesystem-independent review-safe path policy across the legacy
  value helper, compatibility facade, and v4 output boundary. Withhold known
  path fields with a visible marker instead of silently dropping `path`.
- Cover custom roots, spaces, file URIs, Windows/UNC paths, and legal `?`/`#`
  filename characters. Preserve explicit web URLs, labeled routes, injected
  legacy policies, and path-redaction-disabled behavior.
- Conservatively withhold the remaining line after a detected unquoted path;
  quote paths or use structured fields when surrounding prose must survive.
  The representative review-safe projection changes accordingly; its
  operator-full baseline is unchanged.
- Correct beta.2 publication/installation evidence and enable the existing CI
  matrix for `tedd-*-beta.*` pushes. Publishing these source changes does not
  update the installed plugin. At this source checkpoint, installation and
  native acceptance remained at `28eeaca`; the later beta.3 installation has
  its own qualification record.

## 1.1.0-beta.2: 2026-09-04 (published beta checkpoint)

The initial `tedd-1.1.0-beta.2` branch checkpoint was published at `28eeaca` and that source was
installed on the qualification Mac. After a desktop restart on 2026-09-04,
native discovery exposed all three MCP tools and one native `brain_status` call
succeeded. This is not full release qualification: native `recall_context` and
`pull_changes` calls, Windows MCP, all-OS CI, and broader live behavior evaluation
remain unqualified. The published beta.1 checkpoint remains `593940a`.

- Preserve compatibility: Python 3.10-3.14 on macOS, Linux, and Windows;
  canonical store schema v4; projection format v4; and no installation migration
  or rebuild.
- Keep the portable Agent Plugins schema URL at `1.0.0`; it is a schema version,
  not the package version.
- Pin graph reads to operation snapshots and project `brain_status` diagnostics
  through a structural public allowlist.
- Use a contained portable MCP working directory and accept bounded standard
  request `_meta` objects while retaining closed request-shape validation.
- Record source, installation, and native-host evidence separately in
  Beta.2 qualification. Follow-up source changes
  to path redaction, release documentation, and beta-branch CI triggering are not
  part of the published or installed `28eeaca` checkpoint.

## 1.1.0-beta.1: 2026-09-03 (historical local test-build checkpoint)

At this dated checkpoint, beta.1 was a reviewed local qualification build, not
a tagged or remote release. Its later publication is recorded in
Qualification checkpoints; the statements below
describe the original local checkpoint, not current Git or installation state.

- Add adaptive routing for bounded work, a telemetry-free read-only MCP, and contextual communication etiquette.
- Accept SemVer prerelease identifiers in the repository package boundary and keep the two manifests and MCP initialization reply on the same version.
- Compatibility is unchanged: Python 3.10-3.14 on macOS, Linux, and Windows; canonical store schema v4; projection format v4; and no installation migration or rebuild.
- Qualification at this checkpoint was limited to local disposable-data checks,
  not a production release, remote artifact, tag, or substitute for full CI,
  installation, host discovery, or platform qualification.

## Historical untagged development

- Make contextual gratitude part of Tedd's default communication: recognize meaningful help with specific thanks, retain uncertainty and approval boundaries, respect established tone and conversation continuity, and let completed exchanges end naturally. Align support and heartbeat closure guidance so courtesy replies are not mandatory completion gates.

- Package the optional local, telemetry-free, read-only `tedd-graph` stdio MCP beta in the Codex manifest only. It exposes scoped `brain_status`, `recall_context`, and `pull_changes` reads without registration or acknowledgement; the CLI remains the authoritative fallback, native Windows uses that fallback until a launcher is qualified, and this tranche has no graph migration or version bump.

- Add an optional `orchestrate-work` skill that adaptively keeps work local, delegates bounded leaf agents, or offers explicitly authorized persistent tasks based on live runtime capabilities. Preserve one external conversation owner, carry active-turn deltas across task handoffs, and reconcile rapid message bursts through ordered message identity, urgent interrupts, grouped question maps, parent-task consultation, and a final stale-state check before sending.
- Add an executable v1 semantic coverage contract for substantial analyses and proposal reviews. Require all six durable outcome categories, bind the normalized ledger into capture fingerprints, resolve packet paths and exact existing IDs, persist coverage digests in v3 journals and v4 mutation details, reject unknown top-level packet and coverage-ledger fields, and keep record-level assistant recommendations in the candidate lane even when their packet evidence is verified.
- Audit the repository documentation against the packaged CLI, schema constants, manifests, current Codex validators, and release workflow.
- Correct v4 multi-agent, Graph Steward, projection-recovery, and heartbeat operating instructions.
- Make completed same-watermark inbox acknowledgement retries idempotent and remove rejected or no-op v4 options from CLI help.
- Pin the Node 24-native `actions/checkout` v7.0.1 and `actions/setup-python` v7.0.0 actions to immutable upstream commit SHAs; the read-only jobs do not persist checkout credentials.
- Add a non-distribution `pyproject.toml`, PEP 735 developer tools, repository-scoped Ruff and gradual mypy gates, branch coverage, Python 3.10-3.14 CI, macOS coverage, and a nonblocking Python 3.15 prerelease job.
- Add a Codex-discoverable, repository-local Python engineering skill under `.agents/skills` with 45 corrected Tedd-native rule cards, concise policy routers, machine-checked upstream coverage, and an exact MIT provenance notice; it is not shipped in the plugin and has no upstream synchronization contract.
- Extract the legacy graph CLI dispatcher and v4 mutation transaction into statically checked private modules with explicit legacy adapter boundaries behind stable flat compatibility facades. Preserve command routing, JSON output, hashes, and SQL state while closing unexpected-exception rollback gaps, rejecting caller-owned nested transactions without disturbing them, and retaining the 10,000-record packet contract through bounded revision reads. Project revised records from their latest observation so corrected event notes expose accurate update recency without changing occurrence time.
- Complete the responsibility-based decomposition of the legacy and v4 graph engines into statically checked store, query, migration, projection, backup, capture, coordination, and command-family services behind stable flat compatibility facades. Add an architecture contract that rejects service-to-facade back-imports, private-package cycles, and missing responsibility modules; extend Ruff, mypy, and explicit-`Any` enforcement to every production Python module.
- Make directory durability failures explicit, escape read-only SQLite paths correctly, snapshot capture inputs through one validated descriptor, and make plan-key creation first-writer-wins. Backups and exports now use coherent SQLite snapshots; backup destinations are immutable and publish a matching digest manifest last as their completion marker.
- Return stable JSON for command-line argument errors across both graph CLI generations.
- Add native Windows runtime support with `LockFileEx`, protected NTFS ACLs, reparse-point and remote-volume rejection, UTF-8 CLI streams, and portable benchmark metrics while retaining the existing POSIX locking and permission paths.
- Add cross-platform release verification, a native PowerShell entrypoint, Windows CI coverage, LF-stable shell scripts, and Windows setup guidance while preserving the Bash gate.
- Establish `tedd` as the canonical, domain-extensible skill and marketplace name, and retain `tedd-development` only as a non-implicit skill compatibility alias.
- Give Tedd an explicit public identity, private first-run operator profile, exact multi-operator resolution, and provider-backed shared-chat authorship rules.
- Add read-only consumer ownership inspection, preserve an existing parent-task link when an exact owner refresh omits it, and provide an optional explicit rebind that preserves subscriptions and acknowledgement cursors across agent-identity transitions.
- Add an explicit, idempotent, seed-free `init-v4` path so a new private installation can create and bind an empty SQLite-WAL brain without fabricating a v3 migration source or example data.
- Remove runtime coupling to the package publisher from behavioral instructions and use synthetic people in tests.
- Expose namespace, canonical ID, lifecycle, and redirect state in the Obsidian Entities Base so same-name identities remain reviewable.
- Document the private historical sizing reference as 328 canonical records plus 38 earlier revisions, distinguish the exact 328,000-record planning target from the conservative 366,000-record qualification tier, and document the aggregate 366,000-record/36,600-revision/32-writer qualification.
- Preserve the sealed memory-research branches as annotated archive tags and publish a scientific aggregate report with explicit research questions, intervention setup, methods, effect sizes, uncertainty, limitations, and immutable evidence links; recency remained the production default after the tested hybrid candidate improved context efficiency but reduced correctness and introduced critical omissions in blinded agent evaluation.
- Add an Agent Plugins v1 portable manifest alongside the Codex-native package, and version the public synthetic memory evaluator with stronger filesystem, reference, runtime-provenance, and evaluator-token contracts while preserving the frozen revision 1 evidence.
- Add the Tedd brain icon as the canonical composer and light/dark plugin logo with an exact-path PNG asset boundary check.

- Package Tedd and Obsidian Knowledge Graph as one private Codex plugin.
- Establish the SQLite-WAL v4 shared-brain architecture and mandatory multi-agent knowledge cycle.
- Add repository data-boundary enforcement, isolated verification, CI, installation guidance, and release policy.
- Exclude all runtime knowledge, client data, production MemoryBench material, credentials, backups, and generated Obsidian content.

Planned `1.0.0` compatibility and verification:

- Python 3.10-3.14 on macOS, Linux, or Windows; CI covers every supported Python version on Ubuntu and endpoint versions on Windows and macOS, with Python 3.15-dev monitored nonblockingly.
- Canonical store schema v4 and projection format v4 with v3 logical projection payloads and v2 authenticated ownership markers.
- Existing v4 stores require no migration. Legacy v3 JSON stores require an explicit side-by-side migration and fresh review-safe projection; installation never performs either action.
- The complete isolated suite, including identity-profile, migration, backup/restore, concurrency, privacy, and projection boundaries, must pass. The disposable 1,000-record/8-writer smoke benchmark passed during packaging.
- The private production MemoryBench suite passed after the read-boundary decomposition; its cases and report remain outside this repository.
