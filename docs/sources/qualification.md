# Source qualification and availability

## Current availability

Tedd **1.2.0-rc.13** adds labeled partial-match search, a current-facts sheet,
follow-through review, and reviewed decision and commitment outcomes. It retains
scoped memory, guided setup, shared Codex/Claude memory, selective-saving
handoffs, overdue review, and dated preferences. The install commands select
`v1.2.0-rc.13`.

Check the [release](https://github.com/AgentBigBrain/Tedd/releases/tag/v1.2.0-rc.13)
and the [CI results](https://github.com/AgentBigBrain/Tedd/actions/workflows/ci.yml)
for its exact public commit and verification evidence. After installation,
compare the package files and complete a scoped read in your host. Hook discovery,
source readers, and provider access have their own setup checks.

RC13 keeps store schema v4, projection format v4, and the standard-library runtime.
No graph migration, rebuild, or reindex is required. Existing capture packets
without the new outcome or observation-reference fields retain their fingerprints.
Source archive formats and reader contracts are unchanged. Installation leaves
the graph, source permissions, and Obsidian projection intact.

The public package omits private plans, evaluation records, and personal hosting
procedures. Keep local procedures in a private skill alongside Tedd.

The earlier development versions below explain when features were added. Their
host checks retain the versions and limits recorded at the time. Detailed
historical evidence remains in the maintainers' private archive.

| Version | Added capability | Source availability and evidence |
| --- | --- | --- |
| RC8 | Prefix search, guided graph connections, Claude shared memory | Merged to `main` October 15, 2026 at `3258ad7`. [Host and CI record](../CLAUDE_CODE_QUALIFICATION.md#rc8-source-and-verification) |
| RC9.2 | Selective saving and handoff receipts | Merged to `main` October 17. [Host checks](../CLAUDE_CODE_QUALIFICATION.md#rc92-selective-saving) |
| RC10 | Read-only overdue-commitment review | Merged to `main` October 17. [Host checks](../CLAUDE_CODE_QUALIFICATION.md#rc10-overdue-review) |
| RC11 | Scoped preferences and MCP retrieval benchmarks | Merged to `main` October 18. [Host and version checks](../CLAUDE_CODE_QUALIFICATION.md#rc11-scoped-preferences-and-mcp-memorybench) |

These are prerelease versions, not a final `1.2.0` release. Earlier local checks
and CI results apply to their named candidates; they do not qualify a later
package automatically. Updating source on GitHub does not update an installed
plugin or enable optional readers, Google access, or source previews.

In RC13, `search_memory` returns labeled partial matches when no record contains
every query word. Use `match: all` to require every term. Task subscriptions and
privacy rules apply before matching and result limits. See the
[search reference](../../plugins/tedd/skills/obsidian-knowledge-graph/references/recall.md#on-demand-mcp-search).

Task context also returns `current_state` and `waiting` sections. The
`review-queue`, `review-answers`, and `fold-candidates` commands help review
unfinished work and repeated notes. Writes still use the reviewed capture
workflow. See [current claims](../../plugins/tedd/skills/obsidian-knowledge-graph/references/recall.md#current-claims).

The dated records below describe earlier builds. They are historical evidence,
not the RC13 test report; use the release and its exact CI run for that report.

| Area | Verified scope and remaining limits |
| --- | --- |
| Core runtime | Python 3.10–3.14 on Windows, Linux and macOS. All 15 jobs passed for RC7 main commit `5447a53` in the main-branch CI run, including all nine core platform jobs. Python 3.15-dev passed as an experimental target. Platform and dependency skips are not native acceptance. |
| Earlier local regression checks | At `ba4bd86`, full Windows 3.10/3.14 and Linux 3.10/3.14 runs each passed 1,838 tests. Quality runs measured 71% coverage on Windows and 72% on Linux; strict mypy checked 208 files. These pre-version results do not qualify a later commit. |
| Windows and Linux sources | Earlier [document, image and Google-control checks](#september-15-windowslinux-checkpoint) retain their recorded hosts and helper versions. RC7's main-branch CI additionally passed the live Windows Office helper job. This does not qualify every optional helper on a new installation. |
| Optional Mac reader | The [October 1 completion](#october-1-approved-mac-testing-completion) covers synthetic warm reads, a cold PNG request, isolated installed-code execution, offline image import and scoped shutdown. Earlier records cover Desktop checks with PNG, PDF, and DOCX files and sleep recovery, with the limits stated below. Native Mac PDF, Office and image workers remain disabled; plain-text reading does not require the isolated runtime. |
| Mac Google and owner viewing | Protected Keychain credential storage is implemented and synthetically tested. Live Google sign-in, disconnect and recovery and the private owner viewer remain unqualified on Mac. |
| Obsidian memory view | Generated notes and tables have Windows Obsidian 1.13.7 acceptance evidence. They show permitted graph records, not archive originals. Native Obsidian source previews remain disabled. |

Mac testing used a prepared ARM64 runtime and synthetic documents. It does not
establish factory-clean offline provisioning, private-document handling, every
image or Office format, or new Desktop discovery for RC6. PDF page review used
the reader separately from native intake; no source MCP PDF-render tool is
provided. The sleep check does not establish Office parsing through suspension
or continuous guest expiry while the Mac is asleep.

Use [source setup](setup.md) for host prerequisites and
[the Mac testing guide](../MACOS_TESTING.md) for isolated checks. Use the setup checks to verify the reader, viewer, or provider connection you
intend to use.

## Dated qualification records

The checkpoints below summarize earlier private builds. Statements about
unfinished work or installed versions describe those checkpoints. Detailed
measurements and failed attempts remain in the private evidence archive.

### October 1 main delivery and Windows installation

RC6 was merged through PR 12
as main commit `769613b`. Its main-branch CI
passed. The recorded Windows Codex installation matched all 159 package files
against that clean source, with no missing, changed or unexpected files.
Both configured endpoints used RC6. Direct protocol checks passed, followed by
native desktop graph health, scoped inbox and recall checks after Codex restarted.

The desktop-owned source process and tool discovery were verified, but no scoped
native source operation was exercised. The restart helper's automatic relaunch
was not verified; the later running desktop was checked separately. These results
do not qualify every source workflow or installation on another host.

RC7 was subsequently merged through PR 13
as main commit `5447a53`. Its main-branch CI
passed all 15 jobs. The package adds the fourth graph tool, `search_memory`.
The feature delivery record
records its scope and remaining limits. The Windows installation above remained
on RC6; an RC7 installation needs its own file comparison and native checks.

### October 1 approved Mac testing completion

The approved synthetic Mac scope is complete on the development candidate based
on `fafd5fd`, not a released or everyday installed package. The stable source/test
candidate passed full Mac Python 3.10 release and 3.14 quality gates: 1,834 tests
per interpreter, 195/189 skips respectively, zero failures/errors; 3.14 measured
72% branch-enabled combined coverage above the unchanged 55% floor. An automatic
cold request reached verified readiness in 10.777049 seconds (15-second gate)
and completed a validated PNG read in 12.454212 seconds (30-second request).
Fresh isolated, network-denied installed-code execution also passed; it is not
a new native Desktop hook/MCP acceptance run or factory-clean Mac installation.

The exact F02 and dependency images were exported, independently validated and
imported into a separate empty, network-denied store. The owner-approved exact
small-workload amendment retained a real 3 GiB project quota/200,000-inode limit
and native EDQUOT proof on an owned 4 GiB loop filesystem; it does not lower the
original larger-workload F02 or cold-start floors. The imported PNG passed in
4.532007 seconds including exact cleanup. Owned services/storage were removed
and the dedicated VM stopped normally; process, former SSH listener and selected
runtime socket absence, six idle journals and seven compatible locks were checked.

Earlier warm and genuine-sleep evidence is reused with its original limits.
Private documents, specialized formats, factory-clean offline bootstrap,
Windows/Linux-host regression, exact-candidate CI and release/merge qualification
remain separate. Installed beta.4 and live memory were unchanged. See the
October 1 measurements, artifact identities and handoff.

### September 28 F02 warm and sleep checkpoint

The uncommitted F02 candidate remains based on `fafd5fd`; installed beta.4
was not updated. Its Office page transport keeps one `communicate` call across
partial stdin uploads. The exact original 140,259-byte synthetic DOCX returned
`text_screened` in 2.477 seconds under the unchanged request deadline. The
offline ARM64 F02 image
`sha256:0b5bfafb1087891fab625b6c1b8b17b432fc88142e944e38587db53639f49aa7`
matched all 114 staged source scripts. A separate isolated rc.5 package matched
all 156 plugin files; one native archived-DOCX page-1 call returned a visually
legible 20,061-byte PNG (SHA-256
`eff6a50a2b1cb7f576be918a085c13e2fe48aa60bfb5aeadb72b9e5a23352905`).
Its exclusive call window contained one exact F02 reader job that returned to
idle/zero; the job journal has no operation or input digest. This qualifies
only that archived page call, not five F02 pages, fresh intake or publication.

On the existing warm VM, 20 F02 samples per route had zero failures. PNG-page,
PDF-text, PDF-page and DOCX-page p95 times were 1.034977/2, 0.987713/5,
0.958169/5 and 1.611443/15 seconds (observed/target). Five successful reads
per route had a maximum **sampled incremental full-process** delta of
+25,674,544 bytes, below 4 GiB. These are coordinator samples, not F02 native
Desktop memory, an integrated cold request or a bound on sub-second peaks. The
final frozen-diff local Mac Python 3.14 quality command exited 0: 1,718 tests,
189 skips, zero failures/errors, 203 mypy files, 222 formatted files, 71%
branch coverage against the 55% floor, plus boundary, documentation and shell
checks. The changed process module separately passed 16/16 on Mac Python 3.10
and 3.12; no final-diff full 3.10/3.12, Windows/Linux-host or CI pass is claimed.

A separate fixed-worker diagnostic image
`sha256:c51bcab0752715b1e630ea10954b5c69cdd4c0d7f6ad9ef4c7cfe9227b6c69a8`
first passed no-sleep readiness with 82 same-job Running samples over 24.081316
seconds. The later genuine Mac case recorded system Sleep at 11:40:23 EDT and
Wake at 11:40:47, an unchanged policy-valid same job still Running after Wake,
an uncancelled bounded `source_unavailable` refusal with no result, and exact
idle/zero recovery followed by one synthetic PNG `text_screened` read. Probe
and recovery result SHA-256 values are respectively
`69b23bd3d11acd7a7c3d26a531714706ab0d49ab7487f0b0ac7886a633ac3802`
and `899f92997b5e3dd768127f1b52cd0022b30775f1171f401253c61e41081a0ee4`.
Earlier permission-packaging failure, decode-aborted sleep attempt and a
display-only/inconclusive attempt remain non-passes. This fixed-worker case
does **not** prove continuous guest expiry during suspension or Office parsing
through sleep. A separate private closeout report (SHA-256
`0617690c231b87036cababcdd790b07987629766cc60563c98b0b6327e6e7541`)
records one scoped graceful stop of the dedicated test profile, with three
idle journals, zero containers before shutdown, and no dedicated host-agent,
driver or socket afterward. The unchanged project config still declares an
older source endpoint against this stopped runtime; no source process was
loaded. Independent documentation/evidence review completed September 28 with
no actionable findings for this bounded warm synthetic checkpoint.
Automatic cold startup, portable-image import, clean-host deployment,
private-document intake, other-host/CI and release
qualification remain open. See the
F02 and genuine-sleep evidence.

### September 27 repaired warm-reader candidate

This earlier uncommitted checkpoint is based on `fafd5fd`. Its three-file F01
repair accepts Docker DNS readback as `null` or `[]` without relaxing other
isolation checks, with three regressions. Test-only follow-ups fixed a
clock-dependent graph nonmutation assertion and covered real child-side stdin
closure during a 32 MiB upload. The repaired ARM64 image
`sha256:ca4d1083bfc548cde7cab7f6b5be7bf64aeebb72f6eff5c6984b4b5c9f9551b8`
matches all 114 staged source scripts. A separate isolated F01 plugin install
matched all 156 source files; earlier base-commit installed-cache graph calls
were over stdio, not native Desktop. The older archive's fresh-VM result does
not establish portable import of this repaired image.

The approved warm synthetic PNG/PDF/DOCX slice has reviewed six-route,
cancellation, bounded transport, owned-recovery, guest-limit, authority and
publication evidence. Warm p95 targets passed with 20 repaired-image samples
per route; repaired-image representative coordinator full-process sampling
stayed below the 4 GiB incremental target in its sampled windows. The native
synthetic source-endpoint Desktop DOCX memory observation (+435,913,304 bytes,
below 4 GiB) is prior older-image evidence, not a repaired-image rerun. VM
pause/resume had a safe composite outcome, although its second fixture exited 1;
genuine whole-Mac sleep/wake was **not run at this F01 checkpoint**. The private
sleep fixture had only passed local review. The full 1,712-test 3.14 quality
and 3.12 release runs passed with 189 skips before the test-only additions; a
3.10 full release run passed with 195 skips after the graph-test correction.
The final added process module passed 16/16 on each of Mac Python 3.10, 3.12
and 3.14; at that stage this was not a five-file, 1,713-test full-suite pass.

An isolated installed F01 package subsequently passed empty native synthetic
graph readiness and selected PNG/DOCX native duplicate intake with partial
archive coverage and visual review. The qualified coordinator rendered ten
PDF pages for actual visual review; after a fresh direct selection, one native
PDF intake returned complete coverage, `duplicate`, and zero new bytes. The
exact repaired-image job was observed in the exclusive native call window;
its journal does not identify the operation. Pre/post checks found an idle
journal with zero containers. The response body was retained by the
tester; task call metadata alone does not expose it. The existing PDF archive
manifest is historical, not a new publication, and this split route does not
provide native PDF page rendering. Automatic cold on-demand startup remains
unimplemented and unqualified. Factory-clean offline host deployment is a
separate broader release gap, not an extra requirement for the approved
existing-runtime warm slice. Current Windows/Linux-host and exact-candidate
CI checks were handed off, not run here. These synthetic acceptance calls used
only isolated stores; installed beta.4 was not updated. No private-document
intake or release is authorized.
See the
repaired-candidate evidence and limits.

### September 27 local Mac checkpoint

At feature commit `686fcd1`, the full local quality gate passed on Mac with
Python 3.12 and 3.10: 1,709 tests per interpreter, with 137 and 189
platform-dependent skips respectively. Ruff, mypy, documentation, repository
boundary and shell checks passed. A fresh Desktop task made another direct,
review-only synthetic DOCX request successfully. Three full-process footprint
samples were idle or mistimed, but a fourth captured a direct Desktop request.
Its observed baseline-to-peak increase was 435,913,304 bytes, below the 4 GiB
incremental target for that synthetic request; other workloads and sub-second
children remain unqualified. An uncancelled competing-read diagnostic was
inconclusive at its 12-second observation deadline; its guarded VM recovered
with an idle journal and no containers. See the
Mac evidence record.
The installed beta.4 plugin and live graph remain unchanged.

### September 26 internal-drive Mac continuation

The refreshed private warm reader passed synthetic PNG/PDF/DOCX production
coordinator and fault-recovery checks at feature-branch checkpoint `069e56d`.
Each fault case left no published result or container, returned the journal to
idle, and allowed a later read. A fresh task subsequently discovered the native
source tools, reviewed the synthetic DOCX, and completed a genuine Desktop Stop
during a paused reader call. The container was removed before the safety guard
resumed it, and another direct Desktop review succeeded afterward. This does not
independently attribute cleanup to a particular lifecycle hook. Partial warm
host-memory sampling excluded Desktop and transient CLI processes. The preserved
binary images loaded into a separate empty ARM64 VM and completed one synthetic
PNG operation; a fully offline clean-host deployment remains untested. The
installed Tedd package and live memory were not changed. See the
isolated-reader record
for exact evidence and limits.

The provisioned Windows Office job found a generated PDF with no broad access
but a non-current owner. The source repair retains strict privacy checks,
but exact-candidate Windows CI did not start because of a GitHub Actions account
restriction. The feature branch is not merge-qualified by local Mac tests.

### September 26 RC5 macOS repository check

The development branch's Mac test-only correction keeps direct native-worker
tests skipped only where macOS deliberately refuses that worker. Separate
process-refusal and portable parser/metadata tests remain active. With optional
Pillow present, Python 3.12 passed the full 1,701-test quality/release gate
(134 skips, 75% coverage), including Ruff, mypy, documentation, boundary and
shell checks. A disposable Python 3.10 environment with the repository-pinned
PyYAML also passed all 1,701 tests (186 skips). These are local source checks,
not an installed-plugin or Desktop reader qualification. The experiment record
retains the details and remaining gates.

### September 17 owner-approved optional isolated Mac reader

The initial September 16 investigation below found that Tedd's native
PDF/Office/image worker boundary could not meet its resource and crash-privacy
contract on this Mac. The owner subsequently approved a different, optional
direction: an isolated Linux ARM64 reader in a private internal test runtime,
bound only to the development source checkout. This was not an update to the
installed beta.4 plugin and did not access live memory.

The exact warm runtime completed native Codex Desktop intake for synthetic PNG,
PDF and DOCX fixtures. Originals were preserved by digest, archived reads and
renders succeeded, the model visually reviewed the required PNG/PDF/DOCX output,
and the isolated graph remained unchanged. The production coordinator then passed
the bounded fault cases and all 80 fresh warm timing samples across PNG page, PDF
text, PDF page and DOCX page routes. The exact dependency and final images were
also retained together in a private, integrity-checked ARM64 archive. See the
isolated-reader record for
the evidence, measurements and limitations instead of treating this summary as a
replacement qualification ledger.

Five explicit starts of the existing stopped VM also reached the enrolled daemon
and image with no containers in 11.336–14.501 seconds, passing the unchanged
15-second readiness target each time. The final state was stopped with no runtime
processes. This measures manual readiness of the already enrolled VM; it does not
implement automatic on-demand startup or measure a cold document request.

This closes only the owner-approved synthetic PNG/PDF/DOCX slice and its bounded
existing-VM checks. Genuine Codex Desktop interruption remains inconclusive: two
user attempts reached the
operation too early and too late, and the native render returned. Automatic cold
on-demand startup is not implemented. Clean-machine offline loading, complete
incremental host-footprint accounting and current Windows/live-helper regression
evidence for the shared LibreOffice export change also remain open. Specialized
formats and the Mac owner viewer were deferred.

These changes form a development checkpoint on `feature/source-backed-memory-v1`,
not a release or approval to merge into main. The binary archive remains private
test evidence rather than a published artifact, and the installed beta.4 package
is unchanged. These results do not establish release readiness or authorize
private document intake outside the exact test runtime.

### September 16 macOS source follow-up

Mac testing of feature-branch commit
`4c9ad3a976df043094c733ed9f7c5eff348601e6` found a broader restriction than the
RC2 guide originally described: the shared PDF/Office/image worker's memory and
crash-privacy boundary is not qualified on macOS. Ordinary PNG is affected too.
Optional library installation does not resolve that boundary. The isolated
source follow-up adds an explicit refusal instead of weakening the resource or
privacy contract, alongside Mac Keychain and test-isolation repairs.

This section preserves the initial native-only investigation and its dated
conclusions. The subsequent owner-approved isolated-reader direction is summarized
above.

At that checkpoint these were source changes, not a replacement RC2 artifact or
an installed Mac upgrade. The existing Mac beta.4 installation remained separate,
and Mac native Desktop discovery, owner interaction and real-host cancellation
still required their own checks; a subprocess or unit-test pass could not establish
them. The later owner-approved results are summarized above.

The tested host is macOS 26.5.2 on arm64, Codex CLI `0.154.0-alpha.6.2`, with
Python 3.12.0 for the quality gate and Python 3.10.5 for minimum-version checks.
The native Keychain/connection/OAuth focused run completed 59 tests: 57 passed
and two Windows-only tests skipped. It used unique synthetic items, verified
cross-process reading, update, root/key isolation and exact deletion, and left
no credential files. Locked/denied Keychain failures were injected; the user's
actual keychain was not locked or reconfigured. A separate Python 3.10 focused
run completed 66 tests: 63 passed and three explicitly skipped.

Final source checks on September 16:

| Check | Result | Scope |
| --- | --- | --- |
| Python 3.12.0 release/quality gate | 1,486 tests, 1,301 passed, 185 skipped; exit 0 | Repository boundary, documentation, three shell scripts, Ruff lint/201-file formatting, configured mypy/183 files; branch coverage 70%, above the unchanged 55% floor |
| Python 3.10.5 release gate | 1,486 tests, 1,301 passed, 185 skipped; exit 0 | Minimum-version runtime, boundary, documentation and shell checks |
| Optional-reader boundary check | 12 tests passed | Actual Mac refusals still apply with Pillow 12.3.0, pdfplumber 0.11.9 and pypdf 6.10.0 installed in a separate disposable reader environment |
| Independent review | No unresolved critical/important findings | Credential code and actual-process boundary tests; direct Office/image launch guards added during review |

The skips cover platform-specific tests, unavailable optional readers/helpers and
explicit native-interaction checks; they are not format qualifications. Synthetic
renderer/parser doubles preserve cancellation, cleanup and publication-contract
coverage, but do not represent working Mac native readers. The package comprises
148 files at this source checkpoint; its aggregate SHA-256 is
`1126f4ac5c164573de6c9f023122ec9903e6c400a35bb4763ec128d5a154de65`.
This hashes the C-locale-sorted `shasum -a 256` lines for relative paths under
`plugins/tedd`, excluding `__pycache__` and `.pyc` files. It identifies the
uncommitted source snapshot, not a released or installed RC2 artifact. Subsequent
qualification prose edits were checked again without changing runtime/test bytes.
The installed Mac beta.4 package's before/after digest was unchanged; no plugin
marketplace pin, MCP binding, live graph, existing credential or profile changed.

The initial RC2 quality run failed with 28 failures and 44 errors across 1,459
tests (183 skips). Investigation separated real platform gaps from renderer
mocks that intercepted unrelated filesystem probes and a package-isolation test
that rejected its own reviewed virtual environment. A later diagnostic run
outside the release wrapper also encountered Mac temporary-directory aliases;
it is not qualification evidence. Production filesystem protections were not
loosened to make fixtures pass.

Native probes found that a 512 MiB `RLIMIT_AS` failed even with inherited
unlimited limits. The process had about 391 GiB of virtual mappings but only
about 13 MiB resident. A startup-relative allowance is not the promised total
worker/helper budget. Apple's [resource implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_resource.c)
rejects limits below existing mappings. The alternative footprint-limit call
returned `KERN_NO_ACCESS`, and the corpse-forking control returned
`KERN_NOT_SUPPORTED`; Apple's [task implementation](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/task.c)
requires privilege for the former and a development/debug kernel for the latter.
No system crash policy or privileged setting was changed.

A later synthetic C probe found a different native route:
`posix_spawnattr_setjetsam_ext` accepted an unprivileged child launch with fatal
active/inactive 512 MiB limits. The child's reported footprint plus remaining
allowance equaled 536,870,912 bytes. This is a promising
[private Apple spawn API](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/libsyscall/wrappers/spawn/spawn_private.h),
not a public-SDK guarantee or a tested memory-exhaustion boundary. Forked-child
inheritance, aggregate limits, process lifetime and crash-diagnostic containment
remain unresolved. It is not used by the runtime patch above; the failed direct
setters do not prove that all native approaches require administrative privilege.

At the start of the September 16 investigation, the agreed direction required Mac
support to stay native without a separately installed runtime. That sentence
records the dated plan, not the current approved direction. The native route still
needed a reviewed boundary for aggregate memory/CPU limits, process-tree
termination and crash-diagnostic containment; sampled monitoring, a
startup-relative address-space limit and ordinary signal handlers did not establish
it. After the feasibility findings below, the owner separately approved the
optional isolated Linux reader summarized above. That approval did not qualify the
native worker or change the installed plugin.

The subsequent native-boundary feasibility checkpoint
found that the tested spawn allowance did not carry across fork, and that ignored
or blocked `SIGXCPU` allowed a worker to exceed a one-second soft/hard CPU limit.
A child-only process limit blocked direct fork/spawn. A live Mach receiver handled
an ordinary abort, but destroying the receiver produced an OS crash report; fatal
memory/corpse containment remains unqualified. No pressure test or native-reader
enablement followed. The fail-closed suite passed again (12 tests); the full
release gate above was not rerun for these prototype/documentation-only changes.

An additional `mypy --platform darwin` experiment did not pass: the repository's
configured type-check target remains `win32`; Windows-only ctypes surfaces and
platform-constant test branches produce Darwin-target diagnostics. The configured
type gate and actual Mac runtime tests are separate claims. No type errors were
suppressed or coverage threshold reduced for this follow-up.

### September 15 Windows/Linux checkpoint

As of September 15, 2026, implementation and qualification of the
image and Google-control follow-up
are complete within the approved scope. Version `1.2.0-rc.2` is committed and
pushed to the feature branch and installed in the owner's Windows staging setup.
Installed-package and configured-command checks passed; a fresh Codex Desktop
session still needs its own smoke test. No live collection was enabled by the
upgrade. The previous `1.2.0-rc.1` package is preserved but disabled.

| Capability or check | Recorded result | What it does not establish |
| --- | --- | --- |
| Standalone image formats and helpers | Dedicated Windows/Linux format, constituent, metadata, native-helper and resource checks passed for the bounded [image contract](reference.md#supported-image-formats) | Every codec variant, unrestricted SVG or every file below the size ceilings |
| Ordinary screenshot workflow | Actual isolated Windows Codex review, overview/detail, preservation, reviewed memory binding and fresh-task evidence check passed | Automatic improvement in memory accuracy; a durable correction still needs a separate reviewed write |
| Human-only image workflow | Actual isolated Windows cancellation, private review/save, saved viewing and model text/image denials passed | A new Linux Codex owner-interaction pass or protection from screen capture and privileged local software |
| Google controls under interruption and concurrency | Deterministic command/MCP, paused-acquisition, two-process, replay and replacement-grant checks passed | Live Google behavior by themselves |
| Live Google connection | Isolated Windows capture/read, owner Cancel, confirmed disconnect, denied saved-Google read, unaffected local-image read, warned recovery and recovered saved-Doc inspection passed | A Linux Google-host qualification or a successful redundant recovery capture |
| Repository release and quality gates | Seven Windows/Linux entries collected 1,458 tests each; both Python 3.14 quality gates passed, with platform/dependency skips recorded. Affected checks covered two later interface repairs | A GitHub CI run, installation check or qualification of skipped optional helpers |

The live Google test used an approved existing account with a separate verified
test-only Cloud project, Desktop client and private credential root. That project
isolated revocation effects from grants in other projects. It did not narrow
`drive.readonly`, which grants Drive-wide read/download; the actual reads were
restricted to one explicitly linked synthetic Doc.

JPEG/HEVC retain the disclosed `encoded_bytes_not_exhaustively_inspected` limit.
AVIF sequences can be refused within the bounded contract. Color, HDR, animation
timing, auxiliary-plane interpretation and partial reading coverage remain
limited. Mac source qualification is deferred, and native Obsidian previews remain
disabled. The private viewer is a separate route. This work adds no OCR/vector
indexing and makes no broad model-effectiveness or performance claim.

A redundant capture during Google recovery returned `source_corrupt`; its exact
cause and historical turn terminal remain unknown. The later saved-source read
passed without recapture. Earlier client-setup/authentication output exposures
and the unconfirmed actor of the replacement Google consent remain recorded
limitations, so these results do not establish clean confidentiality or erasure.
The evidence report
preserves the failed attempts, source snapshots and final review/shutdown receipts.

The guides were refreshed after that final snapshot. Their later Markdown bytes
form a documentation-only delta.
At that checkpoint, runtime/helper/test/configuration bytes remained unchanged;
the version preparation below is a separate, later change.

### September 15 RC2 staging preparation

Preparation started from documentation commit
`534ec3440ec465574f70c55d20b12b439ecd3159` on the existing feature branch, with
the completed feature implementation then still in the working tree. The two package
manifests and shared MCP initialization version now identify `1.2.0-rc.2`.
This metadata change does not modify source acquisition, graph selection,
permissions, codecs or helper configuration. The earlier follow-up report and
plan remain immutable historical evidence.

Focused package, manifest, graph/source MCP initialization and subprocess checks
passed on Windows Python 3.10.4 and 3.14.0: 124 tests each, with one explicitly
unavailable packaged `python3` launcher case. The explicit-interpreter copied
package passed. Ruff lint/format, mypy, shell syntax, documentation and repository
boundary checks passed, as did the current Codex plugin validator and all five
skill validators. A single edited-line newline mismatch was corrected with the
pinned formatter; the affected suites passed again on both interpreters.

Those were pre-commit checks, not another full matrix or installation proof.
The exact-commit and installation results are recorded next.
The image-only qualification reader does not contain the PDF dependencies;
replacing the existing shared reader with it would drop PDF support. The upgrade
therefore qualified a separate combined reader before endpoint rebinding.
Neither earlier dirty-tree tests nor a matching version string qualifies an RC2
installation. See the [release procedure](../RELEASING.md) and the
[rollback compatibility boundary](setup.md#choose-the-build-and-preserve-a-rollback-path).

### September 15 RC2 installation

Commit `391b2ab7146b8d8a5680be13ffd466c6cc14771a` was pushed to
`feature/source-backed-memory-v1` and installed as `1.2.0-rc.2` in a separate
local marketplace. All 146 installed files matched the clean committed package,
with no missing, changed or extra files. The package manifest SHA-256 was
`6e5f0e7395c2992d0a4fed97c4f83b0e28137b4d90a5023c657268a99d783da2`.

Clean-checkout release gates passed on Windows Python 3.10.4 and 3.14.0:
1,459 tests each, with 161 recorded platform/dependency skips. The 3.14 quality
gate also passed lint, formatting, type checks and the coverage threshold (72%).
This feature-branch push does not trigger the repository's GitHub CI workflow;
no CI pass, merge, tag or public release is claimed.

The private combined reader's targeted run collected 172 image/document/helper
tests: 159 passed and 13 were explicitly skipped. Additional checks passed for
PDF extraction, PDF rendering and real Office-page rendering. An initial Office
check used an incomplete parser-only fixture
and failed; a complete synthetic document passed without changing Tedd's code.
The reader's pinned wheels were hash-checked, and its dependency check passed.
The earlier dedicated platform and large-image results remain separate evidence.

Eleven synthetic checks passed against the actual installed package, along with
direct graph/source MCP initialization and read checks. Both commands from the
saved Codex configuration initialized as RC2 and listed their tools. These are
not proof of fresh Desktop discovery or owner interaction. Google settings,
graph settings and lifecycle hooks were preserved; no live Google operation,
production graph write, migration or projection rebuild was performed. Previous
package/configuration backups remain private. Restart Codex and complete a fresh
session smoke test before treating this installation as Desktop-qualified.

## Earlier source-memory qualification

As of September 12, 2026, all non-deferred requirements in the agreed source-memory
Windows/Linux plan are complete. This page describes the tested feature and the
recorded staging installation. It does not imply that an older installed package
has these capabilities or that live collection is enabled.
The September 13 versioned staging upgrade is recorded separately below; it does
not replace the earlier workflow evidence.

## Verified routes

| Route | Implementation and qualification | Installation or remaining limit |
| --- | --- | --- |
| Local UTF-8 text, Markdown and code | Exact selected bytes, scoped retention, evidence checks and use-once controls | Available in the recorded Windows staging runtime |
| Local PDFs and DOCX/XLSX/PPTX | Bounded acquisition/readers; selected PDF and Office-page rendering; embedded Office image review | Optional qualified host helpers required; readings and rendering have explicit fidelity limits |
| Original-path Desktop attachment | Actual task delivery and source workflow passed | Preserve the delivered file, not a same-name substitute |
| Codex-managed Desktop attachment | Actual pasted-text attachment and loaded-tool workflow passed | Remote-only or unreadable attachments remain unsupported |
| Authenticated Google Docs/Sheets/Slides | Real Windows acquisition, linked-folder scope, sign-in reuse, versions, denied access and cancellation passed | Requires separately configured OAuth and trusted lifecycle hooks; no general Linux Google-host pass is claimed |
| Owner-confirmed browser exports | Ordinary DOCX/XLSX/PPTX workflow passed in Windows Chrome and Ubuntu Chrome through WSLg | Per-batch owner approval; origin is owner-confirmed, with no independent current Google permission/revision proof |
| Private evidence viewer | Native Windows/Linux text, PNG, selected PDF, embedded Office frame and Office-page checks passed | Requires an appropriate local desktop and host helpers |
| Native Obsidian source previews | Publication code exists; native privacy/cache qualification failed | Disabled on all platforms |
| Mac source workflows | Intended compatibility target | Qualification deferred |
| Arbitrary support widgets, remote-only attachments and unsupported formats | No universal acquisition route | Not claimed as supported |

Generic chat and supplied text routes retain the limits of their own acquisition
checks. The source CLI's presence is not qualification of every browser, support
provider, file format or operating-system environment.

## Candidate and installation

These entries preserve the September 12 installation record. The newer staging
upgrade has its own dated section below.

| Identity | Recorded value |
| --- | --- |
| Qualified runtime | `eaaa1412baea8fe8cd78738e4207492651705cd5` |
| Installed staging source | `bf3f85cb70821295d27c1d5b1ffd9d7e57f23301` |
| Final managed-copy acceptance documentation | `2b5a0f8` |
| Installed package version | `1.1.0`, in a distinct private staging namespace |
| Collection target | Retained synthetic staging graph; live collection disabled |

The seven Windows/Linux release entries and both Python 3.14 quality gates passed
at the named runtime with explicit platform/dependency skips. The later Desktop
acceptance used that unchanged installed runtime. Source publication or a
documentation update does not update an installation.

The private staging namespace is an operator-specific installation record, not a
public marketplace identifier or a generic setup command. New distributions follow
the [release process](../RELEASING.md).

## September 13 versioned staging upgrade

Version `1.2.0-rc.1` was installed from exact feature-branch commit
`2931daacf331f785c8ca8e30646b9d154df41d51`. The version identifies the source-memory
package and the later skill guidance adjustments.
It is a staging candidate, not a merge to main, public release or live-data rollout.

The full gates ran from clean standalone checkouts of that commit on local NTFS
and Ubuntu's Linux filesystem. Each run collected the same 1,277 tests; skipped
tests remain explicit platform or dependency limits, not passing feature checks.

| Host and interpreter | Gate | Result | Skipped tests |
| --- | --- | --- | --- |
| Windows Python 3.10 | Release | Pass | 74 |
| Windows Python 3.14 | Quality and coverage | Pass | 74 |
| Ubuntu Python 3.10 | Release | Pass | 53 |
| Ubuntu Python 3.11 | Release | Pass | 65 |
| Ubuntu Python 3.12 | Release | Pass | 65 |
| Ubuntu Python 3.13 | Release | Pass | 65 |
| Ubuntu Python 3.14 | Quality and coverage | Pass | 65 |

Both quality gates passed Ruff, formatting and mypy. Plugin and skill validators
passed separately. No GitHub Actions run was created: the existing workflow does
not trigger on this feature-branch push. Local gate results are not a CI result.

Codex's supported installer created a distinct private staging namespace. All
132 installed files matched the clean source both before and after the installed
smoke, with no missing, changed or unexpected files. The complete inventory's
SHA-256 is `d706661661b3ee4eee3a0820acb1d055c843250495632a8aa327ed054461ccea`.
The previous installed tree and private configuration were preserved for rollback.

Six existing synthetic source checks passed against the actual installed cache:
disabled capture, scoped local retention after original removal, exact owner
evidence, wrong-owner refusal, later retraction, and endpoint initialization
without Google credentials. A separate direct MCP smoke initialized version
`1.2.0-rc.1` and called all three graph read tools. Its graph bytes stayed unchanged,
and the disposable fixtures were removed.

Only the new Tedd plugin is enabled. Both explicit Windows endpoint paths now
target its installed cache; the duplicate bundled graph launcher remains disabled.
Configuration comparison proved unrelated settings, Google/helper arguments,
and lifecycle-hook contents and approval state unchanged. The retained synthetic
graph and its configuration remained byte-identical; no live graph was used.

Compared with the earlier qualified runtime, the only shipped Python change is
the MCP version identifier. Earlier Google, document-viewing and Desktop evidence
therefore retains its original date and source binding; it was not rerun here.
Direct installed-package tests do not establish fresh Desktop discovery or calls
after a restart. Mac and disabled Obsidian-preview limits remain unchanged, and
these engineering checks do not measure an improvement in model behavior.

## Evidence

The following dated records are retained in the maintainers' private archive
and are not part of the public repository.

- Authenticated Google acceptance
- Windows browser exports and selected-sheet evidence
- Combined document and lifecycle acceptance
- Ubuntu Chrome exports through WSLg
- Installed Desktop round trip
- Actual managed-copy Desktop acceptance
- Runtime and release-run manifest
- Managed-copy acceptance manifest
- Design and delivery records

These are engineering and host-workflow results. They do not establish an
improvement in model accuracy, user productivity or large-archive latency.
