# Releasing Tedd

## Source, releases, and installations

A merge updates source. A release identifies a verified, versioned build. An
installation changes the package loaded by the selected host, such as Codex or
Claude Code. A green source test does not
prove that a desktop has discovered or called the
installed tools. Reserve `Unreleased` for changes not yet published. Repository-only
updates published on `main` use dated changelog entries without a plugin version
bump. A version's `rc` suffix identifies a prerelease, not whether its source is
available or its package is installed. Record those facts separately.

See [source qualification](sources/qualification.md#current-availability) for
current feature availability and installation records.

The compatibility target remains Python 3.10-3.14 on macOS, Linux, and Windows,
store schema v4, and projection format v4. A compatibility target does not
override a failed platform gate recorded in the qualification evidence.

The [Windows setup guide](WINDOWS_SETUP.md) covers explicit interpreter binding,
complete installed-file comparison, upgrade rebinding, and fresh-task acceptance.
The release wrapper creates private disposable fixture storage and passes it
only to child tests through `TEMP`, `TMP`, `TMPDIR`, and `LOCALAPPDATA`. It does
not change the caller's environment or existing directory permissions. This
avoids treating a Windows AppData directory with extra write permissions as a
private test root; production path checks remain unchanged.

## Release gates

Unpublished changes stay under `Unreleased`. Once repository-only updates are
published on `main`, date their changelog entry; they do not require a plugin
version bump, tag or installation when package bytes are unchanged.
When preparing a package release:

1. Confirm the working tree contains only intentional infrastructure changes.
2. Select a distinct version for changed package bytes. Move the applicable `Unreleased` notes to a dated version and synchronize the exact SemVer version in `plugins/tedd/.codex-plugin/plugin.json`, `plugins/tedd/.claude-plugin/plugin.json`, `plugins/tedd/plugin.json`, and the MCP initialization `serverInfo.version`. Set these values before final verification so the checks cover the version being distributed.
3. Run `./scripts/verify-release.sh` on macOS/Linux or `.\scripts\verify-release.ps1` on Windows. The direct cross-platform entrypoint is `python scripts/verify_release.py`; use `python3` when that is the installed Python 3.10+ command.
4. Install the reviewed pip version (`python -m pip install pip==26.1.2`), install the PEP 735 developer group (`python -m pip install --group dev`), and run `python scripts/verify_release.py --quality` for Ruff, gradual mypy checks, and branch coverage.
5. Validate the canonical skills, compatibility alias and repository-local Python guidance with the current Codex skill validators using the commands below. The required repository gate validates the Codex-native manifest, local MCP declarations, Agent Plugins v1 portable manifest and metadata parity. Validate the Claude-native manifest with the selected Claude Code installation as described below. Plugin Creator package validation is an optional additional check.
6. Run the private production MemoryBench suite only when a read boundary changed. Run full graph validation and Graph Steward review for architecture, identity, authority, migration, privacy, or projection changes. Use isolated or explicitly authorized state and keep private evidence outside Git.
7. Verify the repository-local commit identity, commit the reviewed candidate, and run final gates from a clean checkout of that exact commit. Push when authorized and check its own CI run. CI runs on pull requests and on `main`, `fix/**`, and `tedd-*-beta.*` pushes; pushing another branch alone does not start it.
8. Open a pull request when authorized. Require review and green CI before merge. Do not infer permission to merge or install from permission to push.
9. After the reviewed merge and green CI, tag the release (`vMAJOR.MINOR.PATCH`, with its prerelease suffix for a beta) and verify the approved marketplace installation. Distributed artifacts are immutable; never replace an existing version with different bytes.

For a prerelease installation, first commit the reviewed source and select its
exact commit with a distinct prerelease version. Record the commit, version,
CI run, installed-file comparison, and actual native checks separately. Compare
the complete installed tree with a clean source snapshot, including unexpected
files as well as the expected files. Stop concurrent package updates during the
comparison. File timestamps and a matching version string are not proof of
matching bytes. A source branch may still carry an older package version during
development; that is not approval to distribute its changed contents under that
version.

Build metadata (`+...`) remains intentionally excluded from accepted package versions. It must not be used to distinguish distributed artifacts.

## Codex validators

### Required skill validation

First confirm that the skill validator exists in the selected Codex installation.
If it is missing, report skill validation as unavailable and leave that requirement
open; do not record a pass or substitute an unrelated check. Run these commands
from the repository root:

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/tedd/skills/tedd
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/tedd/skills/orchestrate-work
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/tedd/skills/tedd-development
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py plugins/tedd/skills/obsidian-knowledge-graph
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/python-development
```

If Codex is installed under a nonstandard home, replace the `~/.codex` prefix with that installation path. These current-Codex validators supplement the repository's self-contained release gate; they do not access the configured graph or Obsidian vault.

On Windows PowerShell, the equivalent commands are:

```powershell
py -3 "$HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py" plugins\tedd\skills\tedd
py -3 "$HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py" plugins\tedd\skills\orchestrate-work
py -3 "$HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py" plugins\tedd\skills\tedd-development
py -3 "$HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py" plugins\tedd\skills\obsidian-knowledge-graph
py -3 "$HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .agents\skills\python-development
```

Use `python` instead of `py -3` when that is the configured Python 3.10+ interpreter.

### Optional Plugin Creator validation

Some Codex installations provide the Plugin Creator package validator. When the
script is available, run it as an additional check from the repository root:

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/tedd
```

On Windows PowerShell:

```powershell
py -3 "$HOME\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" plugins\tedd
```

If the script is absent, record this optional check as unavailable, not passed.
Its absence does not block merge or release and does not require installing
Plugin Creator. Investigate any defects it reports when run. Required repository,
skill, copied-package, host acceptance and CI checks remain unchanged.

## Claude Code validation

From the repository root, use the selected Claude Code installation:

```bash
claude plugin validate plugins/tedd
```

Manifest validation does not prove that Claude Code loaded the plugin or connected
to its graph server. Follow the [Claude Code setup guide](CLAUDE_CODE_SETUP.md)
with an approved disposable project and graph. Verify native discovery, a scoped
read, and the required session lifecycle checks separately. Keep production
installation and access to existing project memory outside that test fixture.

## Compatibility contract

Every release note must state:

- plugin version;
- supported Python and operating systems;
- canonical store schema version;
- projection format compatibility;
- source-archive reader compatibility and any rollback restrictions;
- whether an explicit migration or rebuild is required;
- whether private MemoryBench and backup/restore rehearsals passed when applicable.

Never use a plugin install hook to migrate or initialize live runtime state. A fresh operator explicitly runs the seed-free `init-v4` command; an existing v3 operator runs the digest-gated `upgrade-v4` flow after backup and review.

## Optional MCP qualification

The Codex-only manifest points to `.mcp.json`, while the portable Agent Plugins
v1 package carries root `mcp.json`. Both declare the same one local stdio
`tedd-graph` server; the portable declaration includes the v1 MCP schema and
explicit `type: "stdio"`. Keep `mcpServers` out of the closed portable Agent
Plugins v1 `plugin.json`. The repository gate validates only this closed local
package contract and never downloads a schema at runtime. Shape checks are
separate from Tedd's approved-launch policy: well-formed stdio fields alone do
not approve a command. The full validator also requires the reviewed launcher
and physical containment without symlinks or reparse points. A different
launcher needs an explicit policy change and executable package qualification,
not merely an existing file. Before release, run
the copied-installed-tree packaging test:

```bash
python3 -m unittest tests.tooling.test_tedd_mcp_packaging
```

The explicit-interpreter case always uses the test suite's selected Python.
A separate case exercises the exact packaged `python3` command when its
bounded runtime probe succeeds. A missing executable, Store shortcut, failed
probe, or unsupported Python is reported as optional launcher unavailability,
not a successful packaged-launch test.

The copied-package check initializes, lists, and calls all advertised graph read tools against a disposable
v4 store, and proves graph/config/logical-state equality afterward. Read-only
means no durable graph, configuration, WAL, or logical-state writes; an already
open SQLite `-shm` coordination file can update its contents or metadata while
its file set, size, and mode remain stable, so do not claim absolute zero
filesystem writes.

The launcher always exposes initialization, `ping`, and tool listing before
opening graph state. A missing, malformed, private, unhealthy, or unavailable
graph is reported only by an `isError` tool result with a safe category; no
configuration path, token, SQL detail, or diagnostic is written to stdout or
stderr. Notifications without an ID are intentionally silent.

Both cases copy the package beneath a path containing spaces. The packaged
case's interpreter can differ from the interpreter running the Python suite.
Neither case is native desktop discovery, a native tool call, a portable-client
discovery test, a duplicate-loading test for a dual-format client, or real-host
interpreter selection. Qualify macOS, Linux, and native Windows separately in
their real hosts. On native Windows, use the `py -3` or configured-Python CLI
fallback until a native MCP launcher receives its own installation qualification.
If `python3` is unavailable, report the exact probe limitation; this does not
invalidate skill or CLI packaging. Install only an approved,
distinctly versioned package from its recorded commit and start a new task before
claiming host MCP or skill discovery. Add a dated checkpoint for what actually
ran without rewriting earlier evidence; discovery is not a successful tool call.

The documentation check validates repository links, recognized CLI subcommands and
option names, and fenced JSON syntax. It covers selected `python3` Bash examples
for graph, source, profile and MemoryBench scripts. It does not validate required
arguments, values, successful execution or MCP argument arrays. It also does not
query GitHub, compare an installed cache, or verify prose about Git state.
Its temporary-repository tests isolate inherited Git config, templates,
excludes, and repository overrides. Git is a required release-gate dependency;
missing Git or a sandbox that denies fixture writes is a failed/unavailable
verification, not a passing skip.

## Optional source endpoint qualification

The default package declarations still advertise only `tedd-graph`.
`tedd_sources_mcp.py` and its trusted task/turn hooks require separate host setup;
they are not enabled by ordinary installation. Follow
[source setup](sources/setup.md) and preserve the host-pinned reader, provider,
retention and cancellation boundaries when qualifying an installed source endpoint.

Record a source workflow from actual selection through preservation, reviewed
memory binding, evidence retrieval and the applicable viewer. Record cancellation,
later versions, refused access and cleanup separately. An app-server interruption
test is not a Desktop Stop-button test. A prepared path is not an observed
attachment delivery. Keep the acquired bytes bound to the actual delivered copy.

Use [source qualification](sources/qualification.md) for the route matrix and
exact candidate evidence. The existing private staging namespace and synthetic
graph are installation records, not a release recipe for another operator.
Changed documentation inside the package is changed package content; a future
distribution must follow the same version and installed-file rules as other changes.

Image acceptance must include review and acknowledgment before preservation,
then separate memory binding and evidence retrieval. Qualify ordinary and
human-only display separately. Live Google acceptance needs explicitly approved
account/client/credential bindings;
deterministic revocation tests do not establish a live provider result.

Check [source-archive compatibility before rollback](sources/setup.md#choose-the-build-and-preserve-a-rollback-path).
An older snapshot must not restore access to restricted/forgotten evidence or
silently redirect newer graph references. Store schema v4 and projection format
v4 alone do not establish source-archive compatibility.

## Historical checkpoints

Earlier testing and installation evidence is kept in the maintainers' private
release archive, outside the current setup guides.
The recorded findings are preserved and
apply only to their named commits, installations, and checks. A statement such
as "CI pending" describes that checkpoint, not the current branch or latest CI
run. Use the exact proposed commit's tests and CI to judge a new merge.
