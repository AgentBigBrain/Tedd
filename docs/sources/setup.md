# Set up source-backed memory

Configure source operations in your local Tedd installation with this guide.
Once setup is complete, users can [ask Tedd to save and check sources](usage.md);
the agent handles metadata and session identifiers for them.

Source collection is opt-in. Start with the existing Tedd installation and private
graph, the database that stores memories and their relationships. The source
archive holds the selected evidence separately. The default plugin provides
the read-only `tedd-graph` server; source operations use a separate local service
named `tedd_sources`. This service is an MCP endpoint: Codex calls its tools
through Model Context Protocol.

## Choose the build and preserve a rollback path

Check [qualification and availability](qualification.md) before changing an
installation. Choose a reviewed build that provides the required tools and
tested readers and helpers for your computer. A reader extracts text or renders
pages and images; some formats need additional helper programs. Check the loaded
endpoint's tool list and command help; a guide describing a feature does not make it available in an
older installation. Follow the [release process](../RELEASING.md) when installing
or distributing a build.

Before enabling image preservation, preserve a compatible rollback snapshot.
Version `1.2.0-rc.1` cannot read an archive containing the `image` source kind.
Restoring an older snapshot also requires current restriction and forgetting
exclusions to remain enforced; newer graph references must become unavailable, never point
to substitute older evidence. If the older runtime cannot enforce those denials,
do not activate the rollback. The [maintenance procedure](../../plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md#pause-forget-and-recover-deliberately)
covers backup, restore and exclusions.

## Prerequisites

A **consumer** identifies a registered agent task. Its **subscription** selects
the graph records that task can read. A **subject** is a graph entity, such as a
person or project, to which the source belongs. Tedd obtains these identifiers
from the existing task registration; display names are not substitutes for them.

1. Complete [Tedd identity setup](../IDENTITY.md) and check the approved graph with
   its `doctor` command. Reuse the profile and graph if they already exist.
2. Resume the task's registered consumer and subscription using the graph skill's
   [multi-agent coordination procedure](../../plugins/tedd/skills/obsidian-knowledge-graph/SKILL.md#multi-agent-coordination).
   Register a new consumer only when needed. Its owner is the private profile's
   opaque agent-instance ID; task and run values come from the actual task.
3. Use the saved subjects authorized for that task. Read the actual store ID,
   generation, namespace and subscription from the graph/session state. Do not
   invent a client, substitute a display name, or use global scope for convenience.
4. Select a private local archive outside the repository, canonical graph storage,
   Obsidian vault, synced folders and network storage. Inputs can come from a
   synced location; the archive cannot. Keep private metadata and work files out
   of those public or shared locations too.
5. Use a working Python 3.10–3.14 interpreter. Windows operators should follow
   [explicit interpreter binding](../WINDOWS_SETUP.md); a `python3` Store alias is
   not proof that Python works. Verify any optional host helpers separately.

The source endpoint reads the same approved graph configuration. Default graph
setup uses `~/.codex/knowledge-graph/config.json`; an existing installation may
have selected another location. Do not overwrite it with a generic example.

A subscription that works for ordinary recall may be too broad for sources. Its
`entity_ids` must equal the command's subject set exactly; optional `record_ids`
may name only those subjects. Global, collection and discovery-kind selectors
are not accepted. Subjects must be current, active, nonsensitive entities in one
namespace. Resume a matching authorized subscription, or explicitly create one
within the approved task scope; do not silently widen or replace an existing one.

Private metadata requires both an owner-only file and an owner-only immediate
parent directory. On POSIX, use modes `0600` for files and `0700` for directories;
on Windows, verify private current-user ACLs. A private directory containing a
world-readable metadata file still fails. Set permissions only on the intended
new work files and directory, not recursively on an existing home or workspace.

## Enable the archive and scoped model access

The examples use Bash from the installed graph skill directory,
`plugins/tedd/skills/obsidian-knowledge-graph` in a source checkout. Replace the
values below with the approved paths and registered session values. On Windows,
adapt the commands to your configured interpreter and PowerShell argument syntax.

```bash
graph_config='/absolute/private/graph/config.json'
archive_root='/absolute/private/source-archive'
consumer_id='REGISTERED_CONSUMER_ID'
agent_id='CONSUMER_OWNER_AGENT_ID'
task_id='ACTUAL_TASK_ID'
run_id='ACTUAL_RUN_ID'
subscription_id='RETURNED_SUBSCRIPTION_ID'
subject_id='AUTHORIZED_CANONICAL_SUBJECT_ID'

python3 -B scripts/tedd_sources.py --config "$graph_config" status

python3 -B scripts/tedd_sources.py --config "$graph_config" init \
  --enable --archive-root "$archive_root" \
  --consumer-id "$consumer_id" --producer-agent-id "$agent_id" \
  --task-id "$task_id" --producer-run-id "$run_id" \
  --subscription-id "$subscription_id" --subject-id "$subject_id"

python3 -B scripts/tedd_sources.py --config "$graph_config" configure \
  --allow-local-documents \
  --consumer-id "$consumer_id" --producer-agent-id "$agent_id" \
  --task-id "$task_id" --producer-run-id "$run_id" \
  --subscription-id "$subscription_id" --subject-id "$subject_id"
```

`init` should report `enabled`. It enables explicit capture without starting a
background collector. The separate `configure` operation grants model access to
ordinary retained local documents for that scope. Retention, model access and
owner viewing are separate permissions; enable only the approved operations.
Repeat `--subject-id` for each authorized subject when more than one is needed.

The initial total quota is 5 GiB. To choose a lower quota, add `--max-bytes` to the
first `init`; this option cannot change an existing archive's quota. The
[reference](reference.md#limits) lists separate per-input ceilings. No automatic
eviction makes room when a limit is reached.

To verify the local command path before configuring Codex integration, follow the
[disposable local walkthrough](local-example.md). It includes the metadata and
graph packet, exact commands, receipt fields and expected evidence.

## Connect the Codex source endpoint

Configure a separate stdio server named `tedd_sources` in the approved Codex
configuration. It communicates through standard input and output rather than a
network listener. Its command is the verified Python executable. Its arguments
start with `-B`, the installed graph skill's `scripts/tedd_sources_mcp.py`, and
`--config` followed by the approved absolute configuration path. Keep `tedd-graph`
available for ordinary graph reads.

The host pins executable and work-directory choices. With a non-default
`CODEX_HOME`, forward that exact value in the endpoint environment so selection
checks recognize Codex-managed attachments. Do not add Google configuration for
a local-only workflow.

Lifecycle hooks are notifications Codex sends when you submit a selection or a
turn starts, stops or is interrupted. They establish which sources the current
turn may use and when that access ends. Configure and trust these synchronous
hooks on the same server:

| Event | Tool | Input |
| --- | --- | --- |
| `UserPromptSubmit` | `link_sources` | Trusted `session_id`, `turn_id` and `prompt` |
| `PreToolUse`, matching `^mcp__tedd_sources__.*$` | `begin_turn` | Trusted `session_id` and `turn_id` |
| `Interrupt` and `Stop` | `end_turn` | Trusted `session_id` and `turn_id`; three-second timeout |

Use the exact hook inputs and integration rules in
[source host integration](../../plugins/tedd/skills/tedd/references/source-host-integration.md#source-endpoint-and-lifecycle-hooks).
These hooks also apply to local intake. The notifications must come from the
Codex host; a model-generated call cannot grant source access. Hooks do not start
a disconnected server. Wait for connection before supplying the source, and
resend the selection once after a restart. Reconstructing it from logs does not
restore authorization.

## First local source

Choose a small, ordinary UTF-8 file that the user explicitly authorized for the
task. For example, a disposable support note can contain “Do not stop the worker
while jobs remain queued.” Use an exact byte range, or state why the whole file
is needed. “Use once” prevents retention.

The agent prepares a private metadata file describing the actual scope, provenance,
representation and coverage; [the metadata contract](#capture-metadata) explains
its fields. It hashes the exact bytes selected for acquisition. The digest is not
a hash of an earlier preparation copy or a model reconstruction.

For Codex, call source MCP `intake_file` with operation flags only. This example
shows the argument shape; replace every uppercase placeholder with verified
session values, actual private paths and the acquired SHA-256 digest.

```json
{
  "argv": [
    "--input", "/absolute/selected/support-note.txt",
    "--metadata", "/absolute/private/capture-metadata.json",
    "--acquired-sha256", "ACQUIRED_SHA256",
    "--workflow-state", "active",
    "--purpose", "keep",
    "--whole-file-reason", "Keep the complete short exchange and its stopping condition",
    "--consumer-id", "REGISTERED_CONSUMER_ID",
    "--producer-agent-id", "CONSUMER_OWNER_AGENT_ID",
    "--task-id", "ACTUAL_TASK_ID",
    "--producer-run-id", "ACTUAL_RUN_ID",
    "--subscription-id", "RETURNED_SUBSCRIPTION_ID",
    "--subject-id", "AUTHORIZED_CANONICAL_SUBJECT_ID"
  ]
}
```

Do not include `intake-file`, `--config` or helper executable flags in MCP `argv`.
CLI equivalents use the subcommand and configuration; an operator CLI check is
not a substitute for the real Codex lifecycle check.

Check the receipt's status, coverage and warnings before continuing. Assemble its
source URI from the returned `ref` identities as shown in the
[local walkthrough](local-example.md#2-preserve-and-inspect-the-original), or use
scoped `lookup` to discover saved references. Use `inspect` to check the passage.
Repeating the same capture can return `duplicate` without adding bytes. Follow the
[memory-binding procedure](../../plugins/tedd/skills/tedd/references/source-preservation.md#capture-a-local-document)
to create a separate compact graph packet, review a dry-run plan and apply that
same packet with its returned fingerprint and plan file. Use `check_memory` on
the returned graph claim to verify the link. Capture alone does not create memory.

## Capture metadata

The metadata JSON accepts only the fields defined by `SourceScope`,
`SourceMetadata` and `SourceDetails` in the graph skill's
[source contracts](../../plugins/tedd/skills/obsidian-knowledge-graph/scripts/tedd_knowledge_graph/source_contracts.py).
It contains provenance and coverage, not the document body or permission grants.

The scope uses the actual store ID, generation **as a string**, namespace and
authorized subject IDs. Provenance uses actual task/run/interaction identifiers
and capture time. Preserve unknown attribution and source time as unknown.
Local intake requires provider `local`, a stable local principal and
`retained_copy` policy. It derives canonical source identity from the selected
path and the version from acquired bytes, replacing provisional identity values.

The [complete metadata specimen](local-example.md#1-prepare-the-note-and-metadata)
shows every field needed for a small ordinary text source and where to obtain its
values. The same example shows how to save the source, link a memory and check it.

## Optional readers, viewing and Google access

| Operation | Additional host setup |
| --- | --- |
| PDF acquisition and selected page viewing | Qualified reader; viewing also needs the pinned PDF renderer |
| Office structured readings and embedded images | Qualified Office reader, with image codecs for the requested formats |
| Office layout viewing | Pinned LibreOffice converter, private work root, reader and PDF renderer |
| Standalone images | Qualified image reader and the format-specific helpers below; review acknowledgment on the source endpoint |
| Human-only private viewing | Qualified Windows/Linux desktop with Tk and separate owner-view approval |
| Ordinary browser exports | Host-pinned export root, Office reader and actual owner batch-confirmation window |
| Authenticated Google API | Approved client/account, disclosed OAuth scope, trusted selection hooks; reusable sign-in also needs protected credential storage |

Core runtime remains standard-library-only. Optional helpers are not silently
installed or qualified by setting their paths. Consult
[host integration](../../plugins/tedd/skills/tedd/references/source-host-integration.md)
for exact flags and consent boundaries, and
[viewing and maintenance](../../plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md)
for owner access and recovery. Supported PDF, Office and image operations on a
Mac need the isolated reader described below; plain UTF-8 text, Markdown and
code do not. Native Obsidian source previews remain disabled.

Before using a newly configured host with real material, exercise its intended
route on disposable content: actual task delivery, preservation, reviewed memory
binding, evidence checking, applicable viewing and cancellation. Record the exact
runtime and any gaps. Repository tests and a prepared file path alone do not
qualify an attachment-delivery or native-viewer route.

### Optional isolated Mac reader

Supported PDF, Office and image operations on macOS use a separately prepared
Linux ARM64 runtime under Lima/VZ, which runs Linux in a virtual machine on the
Mac. Their native Mac workers remain disabled; plain-text reading does not use
this runtime. Before connecting the reader, an operator must prepare the VM and
review a private enrollment file. That file records the approved configuration,
tools, reader image, storage and ownership so Tedd can check them on each use.
Installing the plugin or image codecs does not prepare the VM. This section
connects an already enrolled reader; it does not explain how to provision a new VM.

Choose one host-startup option:

| Option | Runtime requirement |
| --- | --- |
| `--mac-reader-runtime-config` | A private enrollment for an already running Lima 2.2.0/VZ runtime. Reads recheck that runtime but do not start it. |
| `--mac-reader-cold-config` | A private startup authorization for the dedicated, already provisioned runtime. A read can start the enrolled stopped VM after validating its tools, storage and ownership. |

These options are mutually exclusive and cannot be combined with native reader,
renderer, SVG, AV1 or Office helper flags. They belong in endpoint configuration,
not model tool arguments. Invalid enrollment fails startup; there is no fallback
to native parsing. Neither option creates, downloads or rebuilds a VM. Reader
containers have networking disabled.

The qualified cold-start test required 25 GiB of free host space; keep that
prerequisite when reproducing it. Startup shares the read request's deadline.
Changed tools, static configuration or backing files need review before reuse.
Some runtime files regenerate during startup; the cold path validates their
expected contents and binds their new identities to the active session. Do not
rewrite an enrollment or loosen permissions to get past a refusal.

Use [the Mac testing guide](../MACOS_TESTING.md) for isolated checks and
[current qualification](qualification.md#current-availability) for tested
formats and remaining limits. The source MCP endpoint has no PDF page-render
tool. The tested PDF workflow renders and reviews pages through the qualified
reader separately, then supplies the exact reviewed digest and page count to
native intake. An intake receipt alone does not prove visual review.

### Standalone-image helpers

Pin the image reader at endpoint startup with `--reader-python`. Its optional
Pillow environment must provide the codecs qualified for the intended formats;
HEIC/HEIF also needs the qualified pillow-heif/libheif dependencies. AVIF requires
the separately qualified strict AV1 helper, pinned with `--av1-validator`.
Restricted static SVG requires the qualified renderer and fixed font, pinned
with `--svg-renderer` and `--svg-font`. Check the tested platforms and formats in
[current qualification](qualification.md#current-availability), then use the
disposable image check below for your selected helpers. Record the interpreter,
codec, helper versions, and file digests with your private installation notes.
An installed codec or a supported file extension alone does not qualify a route.

The source endpoint owns these paths; model tool arguments cannot choose helper
executables. On Linux, the native-helper path requires a non-root process,
unreadable sealed in-memory executable and kernel `suid_dumpable` policy of zero.
It refuses incompatible or unknown prerequisites without changing system settings.
Shared time and memory limits are described in the [reference](reference.md#supported-image-formats).
Human-only review also needs the private viewer and separately approved owner
access. Configuring helpers does not grant retention or model access.

For a disposable image check, use an ordinary synthetic screenshot containing a
short statement and a condition. Exercise actual task delivery, overview and
readable detail, review acknowledgment, preservation, reviewed memory binding,
and a fresh-task evidence check. Test human-only review and cancellation separately
if that route is needed. Use the [image procedure](../../plugins/tedd/skills/tedd/references/source-preservation.md#review-a-standalone-image-before-keeping-it)
for the exact calls; the text-only local walkthrough does not qualify this path.

### Google access and connection controls

Configure `--google-client` and `--google-account` together. Reusable sign-in also
needs `--google-credentials`, pointing to an existing private local credential
directory separate from the graph, archive, vault and repository. Windows uses
current-user DPAPI; Linux requires an unlocked Secret Service; macOS uses the
user's default file-based Keychain. There is no plaintext token fallback. Keep
client configuration, tokens and consent callback URLs out of terminal output,
model messages and logs.

The Mac backend binds credentials to the configured root, account and client.
It preserves the system's item access controls, disables interaction for each
operation, and neither creates nor unlocks a keychain. If Keychain access is
denied or unavailable, resolve that condition before signing in. Check
[current qualification](qualification.md#current-availability) for live Google
and owner-confirmation support on the chosen host; credential storage tests
alone do not establish either.

Before consent, disclose that reusable sign-in requests `drive.readonly`: Google
permits Drive-wide reading and downloading, while Tedd restricts actual use to
task-linked resources. The older one-file Picker uses a different permission
model; follow [host integration](../../plugins/tedd/skills/tedd/references/source-host-integration.md#configure-authenticated-access)
and do not switch modes silently.

Google disconnect/recovery controls identify the configured account and
OAuth client and request owner confirmation. A separate test-only Cloud project,
Desktop client and credential root isolate revocation testing from grants in
other projects; they do not isolate the account's Drive data. Qualify live controls
only against an explicitly approved account and synthetic linked document.
Check Cancel, disconnect, denied saved-Google reads, unaffected local evidence,
warned recovery and restored access to the saved source. Deterministic provider
tests do not replace that host check. See [using Google controls](usage.md#disconnect-and-recover-google-access)
for the expected results and remaining revocation uncertainty.
