# Codex and Claude shared-memory qualification

This page summarizes tested hosts and behavior for shared memory, selective
saving, overdue review, and scoped preferences. Check [current availability](sources/qualification.md#current-availability)
for source and installation status. Use the [Claude setup guide](CLAUDE_CODE_SETUP.md)
for installation and the [memory workflow](MEMORY_WORKFLOW.md) for everyday use.
The dated records behind these summaries, with exact candidates, test counts,
and failed attempts, are retained in the maintainers' private qualification
archive; they are not part of the public repository.

## RC11 scoped preferences and MCP MemoryBench

RC11 adds dated preferences for a project, client, or explicitly approved
cross-project personal scope. MemoryBench v2 measures actual MCP search and
the overdue and preference sections returned to a task.

Mac checks tested feature code at `cf2b83c`,
which declared RC10. The later RC11 preparation changes version metadata and
documentation, not that feature logic.

| Host | Verified checks | Evidence |
| --- | --- | --- |
| Windows | Full Python 3.10 and focused Python 3.10/3.14 feature checks. After the RC11 version bump, 77 affected tests passed with one optional launcher skip; a clean-commit repeat passed 17 packaging/protocol tests with that same skip. | Feature and workflow results; RC11 preparation |
| Linux under WSL | Full Python 3.10 and focused Python 3.10/3.14 feature checks. After the version bump, 77 affected tests passed on Python 3.10 with no skips. | Feature and workflow results; RC11 preparation |
| Apple Silicon macOS 26.5.2 | Full Python 3.10 release and 3.14 quality runs: 1,975 tests and 194 skips each; 73% quality coverage. Includes all six workflows and the MCP extension. Native Claude/Codex checks are described below. | Mac results and supplemental checks |

Claude Code 2.1.299 and Codex CLI 0.163.0 each passed three focused preference
scenarios on the Mac: current versus future defaults, a one-time format request,
and separation between projects. The one-time request did not change the saved
preference. Claude offered to save a standing update; Codex waited for the
operator to choose one.

An earlier prompt combined an exact paragraph format with a requirement to
explain a preference conflict. It did not produce a clean conflict-handling
result. The successful follow-up allowed commentary; the dated record
keeps both results.

Synthetic Mac benchmarks and the Windows documentation walkthrough checked
actual MCP reads, scope, dates, privacy, response budgets, and unchanged saved
records and reader state. They demonstrate those paths, not general recall
quality or usefulness over weeks of work. The earlier private CLI benchmark
remains a separate measurement.

These checks used isolated settings. They did not repeat Desktop GUI,
bundled-hook discovery, Obsidian UI, or production installation tests.
These qualification results come from local checks. The
[release requirements](RELEASING.md) still apply to distribution and installation.

## RC10 overdue review

RC10 adds read-only review of overdue commitments within a task's approved
scope. The feature checks tested `e4c8a5a`,
which declared RC9.2. RC10 changed version metadata and documentation without
changing the overdue-review logic.

| Host | Verified checks | Evidence |
| --- | --- | --- |
| Windows | Python 3.10 full quality: 1,937 tests, 334 skips, 71% coverage. Python 3.14 focused checks and later RC10 packaging checks also passed. | Windows/Linux results; RC10 preparation |
| Linux under WSL | Python 3.10 full quality: 1,937 tests, 169 skips, 73% coverage; Python 3.14 focused checks passed. | Windows/Linux results |
| Apple Silicon macOS 26.5.2 | Python 3.10 release and 3.14 quality: 1,937 tests and 194 skips each; 72% quality coverage. Both passed after a private test-directory correction; initial setup failures remain in the record. | Mac results |

On the Mac, Claude Code 2.1.299 and Codex CLI 0.163.0 each passed two focused
read-only tasks. They reported overdue items, continued unrelated work, and
asked for a choice when an item affected the request. No commitment changed.
CLI and MCP checks covered scope, candidate status, unknown owners and dates,
disclosure limits, and unchanged records and reading cursors.

The private three-case CLI benchmark and isolated semantic review retain their
limits in the dated checkpoint.
These checks do not measure everyday effectiveness or cover a fresh installed
package, Desktop GUI behavior, or another selective-saving run. No RC10 hosted
CI pass is recorded. The live overdue-item trial depends on the
one-time commitment review.

## RC9.2 selective saving

RC9.2 adds selective saving and checked handoff receipts. Its Mac results and
test-only repair are recorded at `66e9a5f`,
following `2f2a22f`.
The dated delivery record
retains its local-CI exception and installation limits. No hosted CI pass is
recorded for that delivery.

### Native saving and reminders

All seventeen selective-saving cases passed in native Windows and Mac Claude
Code 2.1.299 and Codex CLI 0.163.0. They cover batched outcomes, attributed
approvals, corrections with retained history, scope isolation, no-save and
no-file limits, receipt recovery, interruption, and session lifecycle changes.
Claude's Stop hook paused once for the memory handoff; Codex used a reminder
without forcing a continuation. See the
Windows record
and Mac record.

The Windows Codex conversations used reviewed, trusted project hooks; the Mac
conversations used reviewed, trusted user-level hooks. Passing these CLI/TUI cases
does not establish Desktop GUI behavior. No native Linux saving acceptance is
recorded.

### Codex hook setup

Tedd retains its portable root manifest. On Codex CLI 0.163.0, the supported
reminder route uses explicit hooks reviewed through `/hooks`. Installing
the plugin alone does not enable the reminders. Follow the [Windows guide](WINDOWS_SETUP.md#check-handoff-hooks-in-an-rc92-test-installation)
or [Mac test guide](MACOS_TESTING.md#check-selective-saving-and-reminders).

An isolated normal marketplace install reported Tedd enabled and available but
listed no hooks. Inline declarations and the default hook filename did not
change that result. The [Codex 0.163.0 loader](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core-plugins/src/loader.rs#L966-L976)
skips hooks for the portable Agent Plugins format before trust review. Removing
the portable manifest in a disposable control exposed all three plugin hooks;
the shipping package keeps that manifest. Earlier Mac checks also found no
bundled hooks on CLI 0.162.0-alpha.17.2 or 0.163.0. See the
hook investigation.

A separate native check kept the portable package unchanged and discovered all
three documented project hooks after workspace trust. Each had project source,
a ten-second timeout, and `trustStatus: untrusted`, with no warnings or errors.
The untrusted workspace had correctly excluded the hooks. This check did not
trust or execute the handlers; the conversation tests above cover execution.

A later Windows discovery control found the same three definitions in the
isolated user's `hooks.json`, without project-hook configuration. All had user
source, ten-second timeouts, and untrusted status, with no warnings or errors.
This establishes discovery, not another Windows saving conversation. The guides
now describe one-time user setup and a project-only alternative; they do not
enable hooks or bypass review.

The guides generate commands with explicit installed paths and graph settings.
Synthetic checks passed with spaces and apostrophes in paths under Windows
PowerShell 5.1/7 and Linux Bash/POSIX sh, including UTF-8 input forwarding.
Those command checks do not constitute another native host acceptance run.

### D1 local runtime checks

| Check | Result and scope |
| --- | --- |
| Windows Python 3.10 and 3.14 | 1,919 tests and 334 skips each; the Windows 3.10 quality run measured 71% branch coverage. |
| Linux Python 3.10–3.14 | 1,919 tests per version; 169 skips on 3.10/3.14 and 175 on 3.11–3.13. Quality runs on 3.10 and 3.14 measured 73%. |
| Mac Python 3.10.5, after the test repair | Full release check passed: 1,919 tests, 194 skips, no failures or errors. |
| Mac Python 3.14.8, before the test repair | Full quality check passed: 1,919 tests, 194 skips, 72% branch-enabled coverage. |
| Mac Python 3.14, after the test repair | All 53 affected reader cases passed without skips; Ruff, formatting, and strict mypy also passed. |

The Windows/Linux checks
apply to their recorded source snapshot. The later Mac repair changes when one
reader-recovery test cancels; it changes no shipping code. The full Mac 3.14
run was not repeated after that edit. The affected-module follow-up
passed all 53 cases without skips on Linux under WSL, Python 3.10.12 and 3.14.8.
Windows Python 3.10 and 3.14 each skipped all 53 cases because the fake daemon
requires POSIX. Those skips do not test the repaired behavior on Windows.
A later test-only change increased the startup allowance for synthetic reader
workers. The affected reader-job/process suites then passed on Windows Python
3.10/3.14 (30 cases, four POSIX-specific skips each) and Linux Python 3.10/3.14
(all 30, no skips). This did not change runtime deadlines or shipping code.
That D1 follow-up did not repeat the Mac checks. These affected-suite results
do not repeat the full matrix or establish hosted GitHub Actions passes.

### Installation and remaining limits

The native Mac saving tests passed. The fresh installation matched all 165
source files, and all 165 still matched after testing. The post-run tree also
contained 58 generated Python bytecode-cache files, so it was not an exact-tree
match. On October 17, the maintainer approved the source merge without repeating
that comparison. The extra files do not change the native saving results above.

The Mac fixture passed graph validation and a same-agent semantic audit. It
does not establish an independent Mac Graph Steward review or projection
qualification. Earlier Windows reviews retain their own recorded scope.
Desktop GUI qualification, release tags, and production installation remain
separate. The
[release requirements](RELEASING.md) still apply.

## RC8 source and verification

RC8 source was merged on October 15, 2026, through
PR 14 at
`3258ad76cb23e376df534554f975688845f7ba0d`.
The package version is `1.2.0-rc.8`. This records source publication;
it does not record a Git tag, GitHub Release, or production plugin upgrade.
The source-merge record did not include a production plugin upgrade.

The required main CI run
and the preceding feature run
failed before tests could run because GitHub blocked jobs for a payment or
spending-limit issue. Neither is a passing CI result.

RC8 was merged using local Windows, Linux, and macOS verification under an
explicitly approved, one-time exception to the CI requirement. The exception
acknowledged billing-blocked CI and unverified interactive permission handling
in the Claude desktop app on Windows. It applies to this source merge only;
normal [release requirements](RELEASING.md) remain in force.

The CI workflow includes an optional Python 3.15 development check. It did not
run for RC8. The earlier RC7 experimental pass recorded in
[source qualification](sources/qualification.md#current-availability) applies to RC7.

### Native host checks

Compare your host version with the table below. These results cover the named
candidates and versions. The standalone Claude Code CLI and the Claude desktop
app's Code tab are tested separately. For your installation, retrieve a known
record within the task's approved scope, then repeat after restarting the host.

| Host | Recorded result | Limits and evidence |
| --- | --- | --- |
| Windows Claude Code CLI 2.1.286 | One graph endpoint; scoped reads, literal and prefix search; resume, fork, clear, and manual compaction; reviewed saving from both Codex and Claude, preserving authorship and revision history; repeated apply created no duplicate records | Narrow noninteractive permissions were checked. Interactive prompts were not qualified. October 4 acceptance |
| Windows Codex CLI 0.130.0-alpha.5, Python 3.10.4 | Disposable guided setup, native health, resumed context, exact-ID search, and prefix search | Used isolated configuration overrides with `--ignore-user-config`; not production Desktop discovery or restart. October 4 setup checks |
| Claude desktop app on Windows, latest recorded 2.42304.0 with embedded Code 2.1.299 | Scoped reads, scope isolation after fork and clear, manual identity refresh after compaction, and cold-process resume with direct `/tedd:tedd` | Earlier loading needed the documented inline option repair. Automatic identity refresh remains unverified, as do interactive permission approval and denial. Cold-process resume |
| macOS 26.5.2 ARM64; Codex 0.162.0-alpha.17.2; standalone Claude Code 2.1.299 | Native scoped reads and search, reviewed saving, retry, and lifecycle checks | Candidate `1fe82621b91cda9b736514a03c6d92b14907b020`; predates the final Windows setup-error repair. Mac handoff |
| Same Mac; Claude desktop app 2.42304.0 with embedded Code 2.1.299 | One-time permission approval and denial; restart, resume, fork, clear, and compaction; saving and retry | Same named candidate; did not require the Windows inline option workaround. Mac handoff |

There is no recorded native Linux Claude acceptance. Claude uses the shared
graph and reviewed CLI saving; its package does not enable Codex source retention.

### Local runtime checks

The final Windows code checkpoint includes a narrow repair that returns structured
SQLite setup errors instead of tracebacks. Earlier Mac and Linux full runs
predate that repair. The evidence therefore does not establish a complete
passing platform matrix for the final merged commit.

| Checkpoint | Result | Evidence |
| --- | --- | --- |
| October 4–5 Windows Python 3.10 and 3.14.0 | 1,882 tests each; 334 skips on 3.10 and 340 on 3.14.0; Windows 3.10 coverage 71% | Non-Mac checkpoint |
| Same checkpoint, WSL Ubuntu Python 3.10.12 and isolated Linux amd64 Python 3.14.6 | 1,882 tests and 173 skips each | Same checkpoint; runtime regression checks, not native Claude acceptance |
| Mac candidate `1fe82621b91cda9b736514a03c6d92b14907b020`, Python 3.10.5 release and 3.14.8 quality | 1,882 tests and 192 skips each; no failures or errors; quality coverage 72%; six supplemental native Codex configuration cases passed | Mac handoff |
| Final Windows code checkpoint `1bdcf3b` | Affected Python 3.10 and 3.14 setup checks passed. Full Windows 3.10 quality run: 1,884 tests, 334 skips, 71% coverage; Ruff linting and formatting, mypy across 210 files, documentation, repository-boundary checks, and shell syntax checks passed | Windows closeout; later changes were repository documentation |

Skipped tests retain their platform and dependency limits. Local results do not
change the failed CI status, update an installed package, or establish native
behavior on an untested host.

## Historical records

The implementation plans, original failures, fixes, and dated checkpoints
behind this summary are retained in the maintainers' private archive. This page
records the final source-merge outcomes without those records.
