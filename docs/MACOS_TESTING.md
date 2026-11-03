# Test Tedd on a Mac

Use a reviewed build, an empty test graph and synthetic documents to check Tedd
without changing your everyday memory. The graph is Tedd's database of memories
and their relationships. Start with that database and a text file. Image, PDF and
Office tests need a separately prepared Linux ARM64 virtual machine and a private
enrollment file that identifies the approved reader and runtime.

Check [current qualification](sources/qualification.md#current-availability)
before choosing a build. This guide explains the test procedure; it does not
establish that a later package passed earlier checks. The dated Mac reader
report, retained in the maintainers' private archive, preserves the tested
artifacts, results and remaining limits.

For selective saving and reminders, see the
[shared-memory qualification](CLAUDE_CODE_QUALIFICATION.md#rc92-selective-saving)
and the [checks below](#check-selective-saving-and-reminders). Those graph and
hook checks do not need the document-reader virtual machine.

For overdue review, preferences, and MCP benchmarks, see the
[RC10](CLAUDE_CODE_QUALIFICATION.md#rc10-overdue-review) and
[RC11](CLAUDE_CODE_QUALIFICATION.md#rc11-scoped-preferences-and-mcp-memorybench)
host checks and their dated evidence.

## 1. Check the Mac and protect your existing setup

You need Codex with plugin support, Git, and Python 3.10–3.14 with SQLite FTS5.
Record whether the Mac is Apple Silicon or Intel, its macOS version, and the
Python architecture. The optional isolated reader uses ARM64; do not treat its
results as Intel reader qualification. Run these commands in Terminal:

```bash
sw_vers -productVersion
uname -m
codex --version
python3 -c 'import platform, sqlite3, sys; assert (3, 10) <= sys.version_info[:2] <= (3, 14), "Use Python 3.10–3.14"; db = sqlite3.connect(":memory:"); db.execute("CREATE VIRTUAL TABLE probe USING fts5(text)"); db.close(); print(sys.executable, platform.python_version(), platform.machine(), "FTS5 OK")'
codex plugin list --json
codex plugin marketplace list --json
codex mcp list --json
```

Resolve missing commands or a failed Python check before installing. Inspect
`codex plugin --help` if your CLI differs from these examples.

**Already using Tedd on this Mac?** Record its version, installed path, marketplace
pin, endpoint commands and lifecycle hooks. Back up its private Codex configuration
and retain the old package. Do not run the fresh-install commands below over an
existing `tedd` marketplace or endpoint. Have Tedd prepare a separate test package
namespace and review the temporary endpoint changes first. Do not copy another
computer's configuration, credentials or live SQLite files onto the Mac.

Keep test state on a private local disk, outside iCloud, Dropbox, OneDrive and
Obsidian. This is a disposable test graph, not a second production store.

## 2. Install the reviewed package

These commands are for a Mac without an existing Tedd installation, `tedd`
marketplace, or `tedd-graph`/`tedd_sources` endpoint. Use the same Terminal session
throughout and stop at any failure.

Replace `REVIEWED_FULL_COMMIT` with the exact 40-character commit supplied for
the reviewed, versioned build. A branch name or the current development tip is
not a substitute. Authenticate to GitHub if requested.

```bash
umask 077
tedd_reviewed_commit='REVIEWED_FULL_COMMIT'
tedd_test_root="$(mktemp -d "$HOME/.tedd-macos-test.XXXXXX")"
git clone --no-checkout https://github.com/AgentBigBrain/Tedd.git "$tedd_test_root/source"
git -C "$tedd_test_root/source" checkout --detach "$tedd_reviewed_commit"
cd "$tedd_test_root/source"
git rev-parse HEAD
python3 -c 'import json; from pathlib import Path; print(json.loads(Path("plugins/tedd/plugin.json").read_text())["version"])'
codex plugin marketplace add AgentBigBrain/Tedd --ref "$tedd_reviewed_commit" --json
codex plugin add tedd@tedd --json
```

Check that the printed commit matches your selection and the installer reports
the version from that source. Set `tedd_plugin` to the installer's actual
`installedPath`, then compare every installed file with the clean source:

```bash
tedd_plugin='/absolute/installedPath/from/the/installer'
test -f "$tedd_plugin/.codex-plugin/plugin.json"
python3 -B scripts/verify_plugin_install.py --source plugins/tedd --installed "$tedd_plugin"
```

Expect `ok: true`, equal source and installed file counts, and empty `missing`,
`changed` and `unexpected` lists. The expected count comes from the selected
source, so it can change between versions. If comparison fails, investigate;
do not delete unexpected files just to make it pass.

## 3. Create an empty test graph and connect Codex

Use the installed code with an explicit test configuration. The empty `vault`
folder below is a sandbox path; it does not connect your real Obsidian vault or
enable source previews.

```bash
mkdir "$tedd_test_root/state" "$tedd_test_root/vault"
tedd_graph_config="$tedd_test_root/state/config.json"
python3 -B "$tedd_plugin/skills/obsidian-knowledge-graph/scripts/knowledge_graph.py" --config "$tedd_graph_config" configure --vault "$tedd_test_root/vault" --store "$tedd_test_root/state/graph.sqlite3"
python3 -B "$tedd_plugin/skills/obsidian-knowledge-graph/scripts/knowledge_graph.py" --config "$tedd_graph_config" init-v4
python3 -B "$tedd_plugin/skills/obsidian-knowledge-graph/scripts/knowledge_graph.py" --config "$tedd_graph_config" doctor
python3 -B "$tedd_plugin/skills/obsidian-knowledge-graph/scripts/knowledge_graph.py" --config "$tedd_graph_config" validate --full
```

Expect a safe v4 store and successful validation. Record its store ID and test
configuration path privately. A path or privacy failure needs correction before
continuing; do not loosen protections or initialize the default graph.

An endpoint is the local service through which Codex calls Tedd's tools. The
bundled graph endpoint uses the default configuration. Disable **only that
bundled endpoint** in Codex's plugin MCP settings, leaving Tedd's skills enabled.
The [launcher guide](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#optional-mcp-and-cli-fallback)
shows the equivalent setting. For this fresh installation, add an explicit
endpoint pointing at the test graph:

```bash
tedd_python="$(python3 -c 'import sys; print(sys.executable)')"
codex mcp add tedd-graph -- "$tedd_python" -B "$tedd_plugin/skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py" --config "$tedd_graph_config"
```

Restart Codex and start a fresh task. Invoke `$tedd` and follow
[identity setup](IDENTITY.md) if needed. Give Tedd the test configuration path and
state that the default graph must not be initialized or written.

Verify that the active endpoint uses the installed code and explicit test
configuration, then call native `brain_status`. It returns a public health flag;
it does not expose the store ID or prove which configuration was selected.
Use the endpoint configuration and the CLI diagnostic above to verify that
binding. Listing the tool or launching its Python script is not a successful
Desktop tool call.

The basic check passes when the expected skills load, the native graph call
succeeds, and your existing configuration and data remain unchanged apart from
the reviewed test bindings.

## Check selective saving and reminders

Use a separately reviewed RC9.2 candidate, the synthetic graph, and approved
test-project bindings. RC8 does not include the handoff commands or hooks.
Complete the scoped graph read before testing saving. The approved test profile
and project binding must already exist in the selected process home; a test
graph path alone does not isolate profile, binding, or receipt storage.

### Add Codex hooks

The tested Codex CLI 0.163.0 does not discover Tedd's bundled handoff hooks.
Keep the portable package intact. User-level hooks need setting up once;
project hooks are an alternative for one workspace. For this test, use only an
isolated Codex profile for user hooks pointing at the synthetic graph. If your
user home also serves real projects, choose the project-only file instead. With the
verified `tedd_python`, `tedd_plugin`, and `tedd_graph_config` values from the
earlier steps, print the definitions:

```bash
"$tedd_python" -B - "$tedd_plugin" "$tedd_graph_config" <<'PY'
import json
from pathlib import Path
import shlex
import sys

plugin, config = (Path(value).resolve(strict=True) for value in sys.argv[1:])
checker = plugin / "skills/tedd/scripts/tedd_handoff_hook.py"
if not checker.is_file():
    raise SystemExit("The selected installation is missing the RC9.2 checker.")
command = shlex.join([
    sys.executable, "-B", str(checker), "--host", "codex", "--config", str(config)
])
hooks = {
    event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
    for event in ("SessionStart", "UserPromptSubmit", "Stop")
}
print(json.dumps({"hooks": hooks}, indent=2))
PY
```

This selects the graph for each hook command. The MCP endpoint's graph setting
does not configure hooks; no `CLAUDE_PLUGIN_ROOT` environment variable is needed.

Select the user hook file for that Codex profile:

```bash
tedd_hook_file="${CODEX_HOME:-$HOME/.codex}/hooks.json"
```

For project-only setup, start from the approved project root and use
`tedd_hook_file='.codex/hooks.json'` instead. Choose one location; hooks in both
locations would run twice. Review the printed paths and save the JSON in the
selected file, creating the parent directory if needed. If the file
already exists, make a private backup and merge the handlers into its event
lists. Preserve other fields and handlers, including source hooks. Update any
existing Tedd handoff handlers instead of duplicating them, and check user and
project hook files and inline hooks in `config.toml` too. Keep machine-specific
paths local.

Validate the saved file and start Codex from that project:

```bash
"$tedd_python" -B -m json.tool "$tedd_hook_file" && codex
```

Continue only if validation succeeds. Follow normal project trust, then use
`/hooks` to review and trust the three definitions. Confirm they each appear
once for Tedd as enabled, trusted hooks from the chosen user or project source.
Never add trust hashes yourself.
The [Codex hook guide](https://learn.chatgpt.com/docs/hooks) describes configuration
and trust. If an upgrade changes the installed path, update these commands,
restart Codex, and review the definitions again.

### Check saving in the host

For Claude, check that the plugin's `graph_config` option points to the synthetic
configuration and that its handoff hooks loaded. Do not continue with an
unconfirmed graph path in either host.

1. Ask Tedd to save a synthetic decision under explicit test-only authorization.
   Check the closing memory report, verify the receipt, and recall the decision
   in a fresh task with the same approved scope.
2. Repeat the check through native host controls with these synthetic cases:

   - Correct a saved decision, then ask a fresh task for the current decision.
   - Retry the same save and check that it creates no duplicate records.
   - Interrupt a turn, resume it, and check that pending work stays visible.
   - Use a second test project and verify that its task cannot read the first.
   - Fork, clear, and compact the session; check each session's own identity and
     approved scope before reading or saving.
   - Exercise "don't save," read-only, and no-file requests separately. Compare
     the graph and local receipt state with what each request permits.

   Check the fresh scoped answer and receipt for each case. The
   [saving opt-outs](MEMORY_WORKFLOW.md#choose-what-may-be-saved) explain the
   expected boundaries. Set `TEDD_NO_LOCAL_WRITES=1` before launching the host
   for a strict no-file check. Replaying hook JSON tests the checker, not host
   delivery.
3. Repeat the installed-file comparison after the host run. Extra bytecode files
   count as unexpected files even when every shipped file still matches. Keep
   that result alongside the native behavior checks.

Record the exact candidate, host versions, hook route, and results privately.
The October 17 Mac record
is CLI/TUI evidence; it does not qualify Desktop GUI hooks or an installation
with a different file tree. Continue below only if source-reader testing is
also in scope.

## 4. Try text-backed memory

Ask Tedd to follow [source setup](sources/setup.md) using the same test graph.
It needs a separate `tedd_sources` endpoint and trusted lifecycle hooks, the
notifications Codex sends about selections and turn boundaries. Register the
task for a test subject, such as a synthetic project. The registration is its
consumer; its subscription selects the records it may read. Let Tedd obtain the
actual identifiers. Keep the private archive and work directory outside the
graph directory and vault.
No Google configuration or optional document reader is needed for this check.

Complete the [local text walkthrough](sources/local-example.md) with synthetic
text. Confirm that Tedd saves the source, links a separately reviewed memory, and
retrieves the original condition. Resolve a failure here before adding other
formats. A saved source alone is not a graph memory.

## 5. Test an enrolled document reader

Continue only with the separately reviewed ARM64 runtime and the appropriate
[warm or cold reader configuration](sources/setup.md#optional-isolated-mac-reader).
These options connect an existing runtime; they are not a provisioning recipe
for a clean Mac. Native PDF, Office and image workers remain disabled, even when
codecs or LibreOffice are installed. The basic text check above uses neither
reader option.

Start with a synthetic PNG containing “Do not ship until the backup is verified.”

1. Connect the source endpoint before attaching the image. Resend a selection
   supplied before connection, and ask Tedd to save it only in the test archive.
2. Follow the [image review procedure](../plugins/tedd/skills/tedd/references/source-preservation.md#review-a-standalone-image-before-keeping-it):
   inspect the overview and any needed detail, acknowledge the exact views,
   preserve the original, and review a separate memory write. Confirm that the
   saved original's hash matches the selected file.
3. In a fresh task authorized for the same test subject, ask whether shipping is
   unconditional. The answer should use the saved evidence and retain the
   “until the backup is verified” condition.
4. For a cold-reader test, begin with the enrolled VM stopped and verify startup
   and the read within the configured bounds. A successful warm request does not
   test startup.

Test cancellation on a separate disposable intake. Interrupt it in Codex before
it saves anything, then verify that it stops without later source or memory
publication. Record anything committed before the interrupt separately. Use the
actual Interrupt/Stop hooks; a CLI call cannot establish Desktop cancellation.
Check recovery in a new authorized turn.

Expand the check only to the routes you need:

| Route | Additional check |
| --- | --- |
| PDF | Review the rendered pages and supply the exact reviewed digest and page count to intake. Page rendering is separate from the source MCP endpoint. |
| DOCX | Check structured reading, embedded-image coverage and selected rendered pages separately. A readable page is not proof of Microsoft Office layout fidelity. |
| Other images or Office formats | Confirm the exact format and helper prerequisites in [current qualification](sources/qualification.md#current-availability); a PNG or DOCX pass does not cover them. |
| Google access | Check credential storage, consent and owner-confirmation support separately. Keychain unit tests do not establish a live Google connection. |
| Private owner viewing | Requires separate host qualification; installing Tk or passing model-facing reads does not enable it. |
| Native Obsidian source previews | Keep disabled; graph notes and the private viewer are separate features. |

Mark each check passed, failed or not run. On `capability_unavailable`,
`scope_denied` or `privacy_rejected`, identify the missing capability, permission
or private-path prerequisite before continuing. Do not substitute private
documents or change the enrolled runtime to work around a refusal.

## Record the result and restore your setup

Keep a short private result note: macOS and Codex versions, hardware/Python
architecture, runtime commit, installed-file comparison, helper versions, and
each attempted check. Include a minimal synthetic reproduction for failures.
Share a sanitized report without credentials, callback URLs, source text or
configuration dumps.

For repository verification, run `python3 -B scripts/verify_release.py` from the
clean source checkout and follow [Releasing Tedd](RELEASING.md). Record skips;
a passing suite does not replace the Desktop, reader or viewer checks.

Stop the test tasks, remove only test-specific bindings and hooks, and restore
the recorded settings. Stop any dedicated test runtime through its approved
shutdown procedure. Close the test Terminal session to clear any test-only
environment settings. Restart Codex with your normal environment and verify the
intended configuration. Keep the test folder until no endpoint, helper, hook, or
marketplace depends on it; then move that exact folder to Trash if no longer needed.

Never delete a shared Codex, graph or plugin-cache directory as cleanup. For a
real installation rollback, follow the
[archive compatibility warning](sources/setup.md#choose-the-build-and-preserve-a-rollback-path):
RC1 cannot read image-kind archives, and current access restrictions and forgetting
exclusions must survive.
