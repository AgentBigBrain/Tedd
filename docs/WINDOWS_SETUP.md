# Windows installation and verification

Tedd's supported Python versions are 3.10 through 3.14. Its optional
packaged MCP server uses `python3`; on some Windows computers that name is a
Microsoft Store shortcut, not Python. A successful plugin installation does
not prove that the memory server can start.

## Install the reviewed package

Use the commit-pinned marketplace commands in the [README](../README.md).
Do not install from a working folder containing developer dependencies or
caches. Preserve the previous package and its source commit before an upgrade;
never replace an already distributed version with different bytes.

If `codex plugin add` is unavailable, check the CLI PowerShell is using:

```powershell
codex --version
codex plugin --help
Get-Command codex -All
```

The help output must list `add`. `Get-Command` lists matching commands in the
order PowerShell resolves them; an older CLI can take precedence even when the
Codex desktop app is newer. [Update the CLI](https://learn.chatgpt.com/docs/codex/cli),
open a new terminal, and repeat these checks.

After installation, identify the actual cache directory used by Codex. The
loaded Tedd skill path points inside that directory. A marketplace source path
or repository checkout is not proof of the installed location.

From a clean checkout of the selected source commit, compare the complete
package, including unexpected files:

```powershell
py -3 -B scripts/verify_plugin_install.py --source plugins/tedd --installed 'C:\path\to\installed\tedd'
```

Exit code 0 means every file and hash matches; 1 means differences; 2 means the
comparison could not be performed. Stop concurrent updates during comparison.
The command is read-only and checks bytes, not the trustworthiness of the chosen
source. It does not ignore bytecode, old `node_modules`, or type-checker caches.
If differences appear, preserve the exact old installation privately and use
Codex's supported reinstall workflow. Do not delete a broad cache directory.

## Bind a working Windows interpreter

The guided setup candidate can preview and apply an existing-brain connection
using Codex's own configuration writer. From the selected package's `skills/tedd`
directory, use a qualified absolute interpreter:

```powershell
& 'C:\path\to\python.exe' -B scripts/tedd_setup.py --host codex --graph-config 'C:\private\graph\config.json' --project 'C:\work\example-project' --entity-id entity_example_project
```

Review the directory, selectors and command, then repeat with `--apply
--fingerprint PREVIEW_FINGERPRINT` for the approved change. The procedure keeps
other server arguments and settings, including a separately configured source
endpoint. Codex can normalize TOML formatting and comments. Setup keeps private
backups and refuses changed configuration; it does not create a brain or turn on
sources. See the
[packaged setup reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#guided-existing-brain-setup)
for prerequisites and recovery. Host discovery and a scoped native read after
restart remain separate checks. The manual route below also remains available.

First inspect the existing `tedd-graph` entry with `codex mcp get tedd-graph
--json`. Keep any existing configuration privately for rollback. If this is a
different server or an unexpected customization, stop and review it before
replacement.

Resolve an installed interpreter and the approved package path:

```powershell
$teddLauncher = (Get-Command py -CommandType Application -ErrorAction Stop).Source
$teddPython = & $teddLauncher -3 -I -S -c "import sys; sys.exit(1) if sys.version_info < (3, 10) else print(sys.executable)"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($teddPython)) { throw 'Install or select Python 3.10+ before continuing.' }
$teddPluginRoot = (Resolve-Path -LiteralPath 'C:\path\to\installed\tedd').Path
$teddServer = Join-Path $teddPluginRoot 'skills\obsidian-knowledge-graph\scripts\tedd_graph_mcp.py'
if (-not (Test-Path -LiteralPath $teddServer -PathType Leaf)) { throw 'The installed Tedd server is missing.' }
codex mcp add tedd-graph -- $teddPython -B $teddServer
if ($LASTEXITCODE -ne 0) { throw 'Codex could not save the server configuration.' }
codex mcp get tedd-graph --json
```

This creates an explicit user-level server binding. It does not rewrite the
packaged transport declaration. Disable the bundled server through Codex's
plugin MCP settings so the host does not also start the `python3` declaration;
keep the plugin and its skills enabled. The corresponding setting in the
private Codex configuration is:

```toml
[plugins."tedd@tedd".mcp_servers.tedd-graph]
enabled = false
```

Do not disable `[mcp_servers.tedd-graph]`, which is the explicit binding above.
See the official [Codex MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
for the distinction between user servers and plugin-server settings. These
commands were checked against Codex CLI 0.153.1; inspect `codex mcp --help` if
your version differs.

Start a fresh Codex task and request `brain_status` through the native tool.
Confirm that only one intended Tedd server is available. Tool discovery alone
is not a successful call; running the Python server directly is not desktop
integration proof. Test scoped reads only with an existing, authorized
subscription; never invent scope identifiers or broaden access to make a test pass.

## Check handoff hooks in an RC9.2 test installation

RC9.2 adds handoff receipts and reminders; RC8 has neither. In the tested Codex
CLI 0.163.0, installing Tedd's portable package does not load its bundled hooks.
Keep the package intact and configure the three hooks below. User-level hooks
need setting up once; project hooks are an alternative for one workspace. See the
[qualification record](CLAUDE_CODE_QUALIFICATION.md#codex-hook-setup) for the host limit.

First verify the installed RC9.2 package and complete a scoped graph read in
the approved test project. The test profile and project binding must already
exist in the selected process home. Selecting a graph file alone does not
isolate profile, binding, or receipt storage.
Use only an isolated test profile for user-level hooks pointing at a test graph.
If your Codex user home also serves real projects, choose the project-only file
for this test instead. This guide prints definitions; it does not enable hooks.

Use PowerShell 5.1 or later. The packaged adapter uses `py.exe -3`, so check
that exact interpreter for Python 3.10–3.14 and SQLite FTS5 support:

```powershell
$teddLauncher = (Get-Command py.exe -CommandType Application -ErrorAction Stop).Source
& $teddLauncher -3 -B -c @'
import sqlite3
import sys
if not (3, 10) <= sys.version_info[:2] <= (3, 14):
    raise SystemExit('Select Python 3.10 through 3.14 for py.exe -3.')
with sqlite3.connect(':memory:') as db:
    db.execute('CREATE VIRTUAL TABLE check_fts USING fts5(body)')
print(sys.executable)
'@
if ($LASTEXITCODE -ne 0) { throw 'The hook interpreter check failed.' }
```

In the same window, enter the installed package directory and the approved
test graph's configuration file when prompted. This prints JSON for review;
it does not change Codex settings.

```powershell
$teddPluginRoot = (Resolve-Path -LiteralPath (Read-Host 'Absolute installed RC9.2 Tedd directory') -ErrorAction Stop).Path
$teddGraphConfig = (Resolve-Path -LiteralPath (Read-Host 'Absolute approved test graph config.json') -ErrorAction Stop).Path
$teddAdapter = Join-Path $teddPluginRoot 'hooks/codex_handoff.ps1'
$teddChecker = Join-Path $teddPluginRoot 'skills/tedd/scripts/tedd_handoff_hook.py'
if (-not (Test-Path -LiteralPath $teddAdapter -PathType Leaf) -or
    -not (Test-Path -LiteralPath $teddChecker -PathType Leaf)) {
    throw 'The selected installation is missing the RC9.2 handoff files.'
}
$teddConfigLiteral = $teddGraphConfig.Replace("'", "''")
$teddAdapterLiteral = $teddAdapter.Replace("'", "''")
$teddHookCommand = '$env:TEDD_GRAPH_CONFIG = ''{0}''; & ''{1}''' -f $teddConfigLiteral, $teddAdapterLiteral
$teddHook = @{ type = 'command'; command = $teddHookCommand; timeout = 10 }
$teddHookEvents = [ordered]@{}
foreach ($teddEvent in @('SessionStart', 'UserPromptSubmit', 'Stop')) {
    $teddHookEvents[$teddEvent] = @(@{ hooks = @($teddHook) })
}
@{ hooks = $teddHookEvents } | ConvertTo-Json -Depth 8
```

The command selects the graph for each hook process and calls the installed
adapter directly. It needs no `CLAUDE_PLUGIN_ROOT` setting. The adapter preserves
Codex's input pipe when starting Python. Custom Windows command shells need a
separate check.

Choose the user hook file for the selected Codex profile:

```powershell
$teddCodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$teddHookFile = Join-Path $teddCodexHome 'hooks.json'
```

For project-only setup, start from the approved project root and use
`$teddHookFile = '.codex/hooks.json'` instead. Choose one location; hooks in
both locations would run twice. From that project:

1. Review the printed paths. If the selected hook file does not exist, create its
   parent directory and save the JSON there. If it exists, keep a private backup
   and merge the three handlers into its existing event lists. Preserve other
   fields and handlers, including source hooks. Update an existing Tedd handoff
   handler instead of adding a duplicate; check user and project hook files and
   inline hooks in `config.toml` too.
2. Keep these machine-specific paths local. Validate the saved JSON with
   `py -3 -B -m json.tool $teddHookFile`, then start `codex` from this project
   in the same PowerShell window. Follow normal project trust, then use `/hooks`
   to review and trust each definition. Do not write trust hashes yourself.
3. Confirm SessionStart, UserPromptSubmit, and Stop each appear once for Tedd
   as enabled, trusted hooks from the chosen user or project source. In the
   approved test scope, ask Tedd to
   save a synthetic decision, check the closing memory report and receipt, and
   recall the decision in a fresh task. A hook listing alone does not prove saving.

[Codex's hook guide](https://learn.chatgpt.com/docs/hooks) explains project files,
merged configuration, and trust. After an upgrade changes the installed path,
update these commands, restart Codex, and review the definitions again.

Use the [receipt reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/capture-and-correction.md#optional-prompt-handoff-receipts)
for exact checks and [saving limits](MEMORY_WORKFLOW.md#choose-what-may-be-saved)
for read-only or no-file work. `TEDD_NO_LOCAL_WRITES=1` must be set before
starting Codex to silence these hooks and prevent their local state writes.
An unbound task is silent; do not expand its scope to trigger a reminder.
Handoff hooks do not enable source retention.

## Optional source operations

The `tedd-graph` binding above supplies graph reads only. Source acquisition,
reviewed memory binding and private evidence viewing use the separately configured
`tedd_sources` endpoint and trusted task/turn hooks.

Follow [source setup](sources/setup.md) for its graph configuration, scoped
permissions, endpoint arguments and first-use check. Local text needs no Google
sign-in or Office converter. Add qualified document helpers only for the formats
that need them. If Codex uses a non-default `CODEX_HOME`, explicitly pass the same
value to the source server so it can recognize selected managed attachments.

After a package update, both explicit endpoint paths may need rebinding. Starting
a fresh task and checking a graph tool alone does not verify the source endpoint.
Confirm it is connected before the user supplies a source selection. The
[qualification page](sources/qualification.md) distinguishes the recorded Windows
staging checks from default installation behavior.

## Keep installation separate from graph maintenance

Run the installed graph CLI's `doctor` with the same interpreter and `-B`.
An existing graph must not be reinitialized to fix a launcher problem. An
internal schema-version refusal needs its own verified backup and upgrade
rehearsal. Do not confuse SQLite's internal schema version with Tedd's canonical
v4 graph format, and do not run the v3-to-v4 migration against an existing v4
database. Installation never upgrades graph state automatically.

After a later approved package update, repeat the file comparison and rebind
the explicit server to the new cache path. Then repeat fresh-task acceptance.
For rollback, restore the recorded previous binding and bundled-server setting;
removing the explicit entry with `codex mcp remove tedd-graph` does not restore
the old settings for you. Keep configuration backups private.
