# Use Tedd with Claude Code

Connect Claude Code to Tedd's graph, a private local database of project knowledge,
then check that you can retrieve a known project record. Codex and Claude Code can
share this graph on one computer. Each task has its own selection of approved
records and tracks which updates it has received.

## Before you start

You need Git, the [Claude Code CLI](https://code.claude.com/docs/en/setup),
and Python 3.10 through 3.14 with SQLite FTS5. Use an interpreter in a stable
location, not a temporary environment.

The shell steps below work for the Claude Code CLI and prepare the
same project for the Claude desktop app's Code tab in Local mode. Follow the
connection check in whichever host you use. See
[Codex and Claude qualification](CLAUDE_CODE_QUALIFICATION.md) for tested host
versions and remaining limits.

Keep the graph and private profile on a local disk outside Git, cloud-synced
folders, and the Obsidian vault. If you already use Tedd in Codex, reuse that
profile and graph. Do not create another database for Claude.

## Install the package

Choose a stable directory for a dedicated copy of the published source. From
that directory, run these commands in Bash or PowerShell:

```bash
git clone --no-checkout https://github.com/AgentBigBrain/Tedd.git tedd-source
git -C tedd-source checkout --detach v1.2.0-rc.12
git -C tedd-source rev-parse HEAD
claude plugin marketplace add ./tedd-source
```

Record the reported commit with your installation; it is the revision the
release tag resolves to. Keep this checkout unchanged and in place: Claude can
load a local marketplace plugin directly from it. Do development work in another
checkout.

Next, change into the project where you want memory and install for that project:

```bash
cd "<absolute-project-directory>"
claude plugin install tedd@tedd --scope local
```

Replace `<absolute-project-directory>` with your existing project directory.
`local` enables the plugin for you in this repository. `project` would write
shared repository settings, and `user` would enable it across projects. See
Claude's [installation scopes](https://code.claude.com/docs/en/discover-plugins#choose-an-install-scope).

The shell installer leaves two required settings, `python` and `graph_config`,
for the [configuration step](#configure-the-plugin). Installation itself creates
no private profile, graph, project records, or credentials.

## Prepare the private profile and graph

If Tedd already works in Codex, use its existing paths and skip initialization.
The defaults are shared by both hosts:

| File | Default path |
| --- | --- |
| Private profile | `~/.codex/tedd/profile.json` |
| Graph configuration | `~/.codex/knowledge-graph/config.json` |

Here `~` means your home directory, such as `/Users/<you>` on macOS or
`C:\Users\<you>` on Windows. The configuration points to the graph; it is not the database file itself. An Obsidian vault is optional.

For a first installation, change into `plugins/tedd` inside the dedicated source
checkout. The commands in this section run from that package directory. On
macOS, use `python3`; on Windows, replace it with a supported launcher such as
`py -3.10`. Choose a version installed on your computer.

Check for an existing profile:

```bash
python3 skills/tedd/scripts/tedd_profile.py status
```

Only if no profile exists, initialize it with the name and email you choose for
account recognition, then validate it:

```bash
python3 skills/tedd/scripts/tedd_profile.py init --name "<your preferred name>" --email "<your email>"
python3 skills/tedd/scripts/tedd_profile.py validate
```

If you defer email, leave memory setup pending and continue other work that does
not need it. See [private identity](IDENTITY.md#first-run) for the profile's role.

Check the graph before creating anything:

```bash
python3 skills/obsidian-knowledge-graph/scripts/knowledge_graph.py doctor
```

Only if the diagnostic confirms that no graph database exists, create the empty
graph and validate it:

```bash
python3 skills/obsidian-knowledge-graph/scripts/knowledge_graph.py init-v4
python3 skills/obsidian-knowledge-graph/scripts/knowledge_graph.py validate --full
```

An inaccessible or unhealthy graph needs diagnosis, not initialization. An
existing v3 graph needs the separate reviewed migration procedure. Initialization
creates no example projects or saved conversations.

## Configure the plugin

Tedd needs two file options in Claude:

| Option | Value |
| --- | --- |
| `python` | The absolute path of your supported Python executable |
| `graph_config` | The absolute path of the existing graph configuration above |

To find the interpreter path, run `python3 -c "import sys; print(sys.executable)"`
on macOS, or `py -3.10 -c "import sys; print(sys.executable)"` on Windows. Use the
reported executable path, not a Windows Store alias.

Start `claude` in your project, enter `/plugin`, then choose
**Installed → Tedd → Configure options**. Set both absolute paths. The shell
alternative is `claude plugin configure tedd@tedd --values-stdin`, with a JSON
object containing those two options on standard input. Restart the session after
configuration. See Claude's [configuration reference](https://code.claude.com/docs/en/plugins/cli-reference#plugin-configure).

In the Claude desktop app, open the same project in the **Code** tab using
**Local** mode. The desktop app and the CLI
[share local settings](https://code.claude.com/docs/en/desktop#shared-configuration).
Check **+ → Plugins → Manage plugins** for Tedd; the desktop app does not use the
terminal `/plugin` panel. If Tedd is already listed and enabled, continue below
to choose the project records this session may read.

## Connect your project

The connection needs exact saved IDs. A project name or directory alone is not
enough to select graph records.

For an existing project, get its exact ID from a connected Tedd task or the
project note's `kg_id` property in your Obsidian view. In Claude, invoke
`/tedd:tedd` and ask Tedd to verify that exact record through its CLI. Confirm the
repository identity, namespace, and client before using it.

If you cannot find the ID, you can explicitly authorize Tedd to search entity
records by a distinctive project name or alias for initial setup. This search
does not require Codex or an existing subscription, but it can return similarly
named projects. Check the exact candidate record before choosing its ID. Missing
or ambiguous results do not prove that the project is absent; resolve them
before creating another record. This setup search is not a workaround for a
refused scope or identity check.

For a new graph or a project that has no record yet, enter `/tedd:tedd` directly
in Claude and ask for first-time project setup:

```text
Set up Tedd memory for this project. Show me the project name and repository
identity you propose to save. Use my existing private profile and graph, review
the capture before applying it, and show the saved project record ID. Then
preview the connection for this directory. Keep memory reads pending until
the approved binding and this session's subscription are in place.
```

For this setup, Tedd registers your task and saves the project record through
its CLI. It previews the proposed record, reviews the evidence, and applies the
change within your authorization. Check that the record identifies your repository
and that the proposed scope matches your project. If setup stops, ask Tedd which
step remains unfinished; keep memory reads pending until setup succeeds.

Tedd can run the connection preview for you. To run it manually, change back
into `plugins/tedd/skills/tedd` in the dedicated source checkout. Replace each
placeholder with the existing path or saved ID you just checked:

```bash
python3 scripts/tedd_setup.py --host claude --graph-config "<absolute-config-path>" --project "<absolute-project-directory>" --record-id "<saved-project-id>" --entity-id "<saved-project-id>"
```

On PowerShell, use the interpreter path reported above:

```powershell
& "<absolute-python-executable>" scripts\tedd_setup.py --host claude --graph-config "<absolute-config-path>" --project "<absolute-project-directory>" --record-id "<saved-project-id>" --entity-id "<saved-project-id>"
```

`--record-id` includes the project record itself; `--entity-id` includes records
that refer to it. Review that selection. Add other exact records only when they
belong in the task's approved scope. Child directories are excluded unless
`--include-descendants` is separately previewed and approved. Select
`--operator-id` if the private profile contains multiple operators.

Check the preview's directory, graph identity, interpreter, and selectors. Repeat
the same command with `--apply --fingerprint "<fingerprint-from-preview>"` after
reviewing them. Claude setup writes the private project binding and a recovery
backup. It does not write Claude options or create or migrate the graph. If
state changed since the preview, review a fresh one.

## Begin a scoped task and check a memory

Start a fresh Claude session in the approved project and type:

```text
/tedd:tedd
```

Tedd checks the current session's identity against your saved project connection.
When needed, it registers the session as a task and creates its subscription,
which selects the records that task may read. Repeat `/tedd:tedd` directly after
a fork, clear, or compaction so the check uses the current session rather than
inherited or cached text.

Ask Tedd to confirm its approved project, then find the project record by the
exact ID you checked above. If the project already has a known decision or
obligation, retrieve that too and compare its meaning, status, and evidence with
what you expected. A new graph may contain only the project record so far.

Confirm that the session has one Tedd graph endpoint. Its four read-only tools
are `brain_status`, `recall_context`, `search_memory`, and `pull_changes`. Approve
only the intended calls if the host prompts. Existing permissions and host modes
affect those prompts; do not disable permission checks to make a read succeed.

Restart the host, open the same project, invoke `/tedd:tedd` again, and repeat the
known-record check.

Once this works, use [the everyday memory workflow](MEMORY_WORKFLOW.md) to choose
when Tedd should save knowledge and to verify that it did.

### Handoff reminders in RC9.2

Installations at RC8 or earlier have no handoff hooks or receipt commands. For
RC9.2 or later, check the hooks separately after the project read works. With active, trusted hooks, Claude can pause once at a normal ending to
request a memory handoff and remind Tedd about a pending interrupted turn.
A working graph connection alone does not establish that these hooks ran.

The [RC9.2 qualification record](CLAUDE_CODE_QUALIFICATION.md#rc92-selective-saving)
covers native CLI/TUI sessions, not the desktop app's Code tab. Use
[saving limits](MEMORY_WORKFLOW.md#choose-what-may-be-saved) for read-only,
no-save, no-file, and strict hook opt-out instructions. A missing receipt must
remain pending; do not weaken those limits to clear a reminder.

## Troubleshooting and limits

| Symptom | What to check |
| --- | --- |
| Setup reports `connected: false` or `not_run` | Setup has not run the connection check in your host. Complete the known-record read above and repeat it after restarting the host. |
| Tedd is absent in the session | Check `claude plugin list --json`, installation scope, and the current project. Restart after enabling or updating the plugin. |
| Skills load but memory tools are absent | Check both file options and the configured interpreter. The Claude desktop app's connector panel can show `${user_config.*}` placeholders; that alone does not prove a launch failure. |
| CLI connects but the Claude desktop app on Windows reports a missing `python` option | Check for the specific option-identity mismatch described below. Do not add another graph endpoint. |
| Scope or identity check is refused | Confirm the exact project, graph, and freshly invoked session. A changed project, owner, or graph needs review; it does not authorize global recall. |
| Preview or apply is refused | Resolve the reported path, interpreter, profile, or graph issue, then preview again. Keep the private backup for recovery. |

The tested Claude desktop app on Windows with embedded Code 2.1.288 looked for
options under `tedd@inline` while installation saved them under `tedd@tedd`. For that diagnosed
failure, the [packaged recovery procedure](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#guided-existing-brain-setup)
explains how to back up user settings and copy only the two reviewed paths to the
inline entry, preserving unrelated settings. Keep both path entries consistent
on later changes; remove only the added alias to undo it. This workaround is
host-specific, not a default setup step. Repeat the known-record check afterward.

Interactive permission approval and denial in the Claude desktop app on Windows
remain unverified. The [qualification summary](CLAUDE_CODE_QUALIFICATION.md)
records this and the other host test limits.

In Claude Code, Tedd supports compact graph memory and reviewed CLI saving, with
the same authorship, revision history, and “don't save” rules as Codex. This route
adds no source server, Google connection, transcript reader, or automatic source
retention.
