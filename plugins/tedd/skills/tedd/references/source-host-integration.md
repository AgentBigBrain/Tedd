# Configure source host integration

Use this reference for the optional source MCP endpoint, browser-export imports
and authenticated Google access. Apply [source preservation](source-preservation.md)
for purpose, selection, metadata and scope. Use the configured graph and exact
registered session values; setup does not create task authority or enable collection.

## Source endpoint and lifecycle hooks

The optional `scripts/tedd_sources_mcp.py` endpoint connects source commands
to trusted task selection, Google and Codex's turn lifecycle. It is separate from
the read-only graph server and is not enabled by ordinary plugin installation.
Local intake also uses the lifecycle hooks below; Google sign-in is optional.
Qualify the actual host route before enabling it.

Start the endpoint with a fixed `--config`. Local historical inspection needs no
Google configuration and must not open sign-in. Local selection uses `intake_file`
with [source preservation](source-preservation.md#select-local-evidence-without-bulk-capture).

For PDF owner viewing, the operator also fixes `--reader-python` and `--renderer`
at endpoint startup to the qualified local binaries. Model tool arguments cannot
select either executable; `owner_view` supplies only the selected `--page`.
Office capture needs only the host-owned reader; it does not require a PDF renderer.
Optional Office layout viewing also requires host-pinned `--office-converter`
and `--office-work-root`, together with the qualified reader and PDF renderer.
The work root must be private and separate from the graph, archive and vault.
Model tool arguments cannot choose these executables or the temporary destination.

Register the endpoint as `tedd_sources` in the explicitly approved host configuration.
Review and trust these exact lifecycle bindings: `PreToolUse` matching
`^mcp__tedd_sources__.*$` calls `begin_turn`; `Interrupt` and `Stop` call `end_turn`.
Each is a synchronous `mcp_tool` hook on server `tedd_sources`, with input
`{"session_id":"${session_id}","turn_id":"${turn_id}"}`. Use a three-second
timeout for end hooks. Local file/attachment selection and reusable Google linked
access both require a synchronous
`UserPromptSubmit` hook calling `link_sources` on the same server with input
`{"session_id":"${session_id}","turn_id":"${turn_id}","prompt":"${prompt}"}`.
Only the trusted hook can supply links; model arguments cannot grant access.
Prompts are reduced to resource IDs in memory, not stored in the source archive.

Check that the service is connected before the first source prompt. Codex hooks
do not start servers: a prompt sent too early can lose its link binding. If that
happens, wait for connection and ask the user to resend the link once. Do not
retry the same denied capture, replay a prompt as a model hook, or widen access.
The endpoint does not recover missing authority from conversation logs.

## Optional isolated Mac reader

Supported PDF, Office and image operations on macOS require a separately prepared
Linux ARM64 reader runtime. Their native Mac workers remain unavailable. Plain
UTF-8 text, Markdown and code use the direct text reader. For the isolated runtime,
the operator pins one of these mutually exclusive options at endpoint startup:

- `--mac-reader-runtime-config`: private enrollment for the already running
  Lima 2.2.0/VZ runtime. Reads recheck its identity but do not start it.
- `--mac-reader-cold-config`: private authorization for the dedicated provisioned
  runtime. Reads can start that enrolled VM after the cold-start checks pass.

Do not combine either option with `--reader-python`, `--renderer`,
`--office-converter`, `--office-work-root`, `--av1-validator`, `--svg-renderer`
or `--svg-font`. These are host choices, never model tool arguments. Invalid
enrollment must stop setup; do not substitute native parsing, edit bindings to
match an unexpected runtime, or loosen filesystem protections. Neither reader
route creates, downloads or rebuilds a VM. Reader containers disable networking.

Retain the qualified cold test's 25 GiB free-host-space prerequisite when
reproducing that setup. Startup shares the read request's deadline.
Changed tools, static configuration or backing files require review. The cold
path validates the expected runtime-generated files and binds their new
identities after startup; warm enrollment alone does not authorize that process.
Qualify the exact installed package and intended synthetic workflow before using
real sources. Reader success does not qualify Google access or private viewing.

The source endpoint has no PDF page-render tool. Review PDF pages through the
separately qualified reader, then supply the exact reviewed original digest and
page count to intake. Do not infer visual review from a successful text reading
or preservation receipt.

## Source MCP operations

Ordinary source operations take one `argv` array containing the existing subcommand flags,
without a command name or `--config`. Reuse the registered session/scope values:

| Source MCP tool | Existing command flags to supply |
| --- | --- |
| `intake_file` | `intake-file`: exact host-selected `--input`, private `--metadata`, acquired digest, purpose/selection and existing session/scope flags. No model-selected reader executable. |
| `import_exports` | `import-exports`: private batch `--input` and session/scope flags. Requires the host-pinned root, reader and real owner confirmation described below. |
| `list_google` | `list-google`: `--folder-id <linked folder or descendant>`, optional returned `--page-token`, and existing session/scope flags. One page of at most 100 names/IDs; no content capture. |
| `capture_google` | `capture-google`: `--metadata <private metadata> --workflow-state active` and capture session/scope flags. No `--input`; the adapter binds the acquired hash itself. |
| `bind_memory` | Graph `capture`: private `--input`, producer/consumer flags, `--source-subscription-id`, `--source-subject-id`, and the normal reviewed dry-run/apply flags. Only an archived `source.uri` is accepted. |
| `check_memory` | Graph `source-evidence`: disputed `--id`, optional `--revision`, and the existing session/scope/budget flags. |
| `lookup` / `inspect` | The existing bounded source lookup/inspection flags. |
| `render_image` | `render-image --source-uri <exact image-unit URI>` and existing session/scope flags. The host pins the qualified image reader; the result is an MCP image, not a text encoding. |
| `render_document` | `render-document --source-uri <saved Office reference> --document-page <number>` and session/scope flags. Requires the separately qualified, host-pinned Office layout helpers. |
| `owner_view` | `owner-view --private-viewer` flags in [viewing and maintenance](source-viewing-and-maintenance.md); no evidence body is returned. |

`acknowledge_review` takes only the returned `review_id`; `disconnect_google` and
`reconnect_google` take empty objects and request their own host confirmation.
Use them only when the loaded endpoint advertises them and the relevant route is
qualified. Standalone-image helpers are fixed at startup with `--reader-python`,
and, when needed, `--av1-validator`, `--svg-renderer` and `--svg-font`. Model
arguments cannot choose executables. The [image procedure](source-preservation.md#review-a-standalone-image-before-keeping-it)
separates rendering from acknowledgment and preservation.

For memory linking, first call `bind_memory` with `--dry-run --plan-output` to a
new private plan file. Review the result, then call it with the same packet/scope,
`--expect-fingerprint` and `--expect-plan-file`. Keep `review_safe`; stdin and
`operator_full` are not accepted by this endpoint. To challenge the Google-backed
memory, use `check_memory` on the same endpoint, not a shell command with no provider
session. Reacquire a later version with `capture_google`, then check the original
memory again. This never rewrites the earlier evidence or corrects a claim automatically.

## Owner-confirmed browser exports

This fallback needs a host-configured `--browser-export-root`, the pinned Office
reader and a working private confirmation window. Keep it unconfigured until its
advertised host passes acceptance. It does not need Tedd OAuth or borrow credentials.
Use only the exact file returned by the authorized Chrome export action; never scan
Downloads for the newest match. Check the selected account, source and visible
download restrictions first. An API denial cannot select this weaker fallback.
Do not export a whole file containing excluded sections or sheets.

For an ordinary, needed batch, write a private JSON array with one object per
export: `path`, `origin`, `account`, `kind` (`docx`, `xlsx`, `pptx`), `sha256`,
`purpose`, `whole_file_reason`, `sensitivity` (`ordinary` only), and
`visual_review_sha256` (null unless the exact original's embedded images were
reviewed). Pass that file as `import_exports`'s `--input`, with the existing scoped
session flags. The host, not tool arguments, chooses the export root and reader.
The batch is limited to eight files and the existing document acquisition ceiling
in aggregate. The native window identifies the proposed files, account, sources,
digests and purpose. Let the owner accept it; do not click for them, simulate an
approval or provide an `approved` flag. Cancellation or no acceptance saves nothing.

Accepted copies retain Google origin with `owner_confirmed_export` provenance.
Their exact exported bytes have a local digest; Google revision and current
permission remain unknown. They support offline historical checking under the
existing scoped local-retention policy. That policy alone cannot grant capture.
Older provider-checked sources keep their current-access requirement, and local
restriction/forget controls still apply. A reader must not describe the owner’s
source/account assertion as independent provider verification.

For a directly copied Google sheet, the proposal may also include
`"sheet_excerpt": {"source_id": "<original-workbook-id>", "sheet_id": "<original-sheet-id>"}`.
The normal `origin` still names the newly created copy, not the original workbook.
Use the actual selected-sheet copy/export handoff; do not infer its scope from an
active browser tab or download excluded sheets and strip them afterward. This
route accepts one literal sheet, without formulas or defined names. If a necessary
dependency cannot be preserved, report link-only evidence instead of a complete
selection. Whole-workbook formula reading remains a separate supported operation.

The owner screen discloses both identities and the shared retention group. The
copy keeps its own resource ID, acquired bytes and digest; its original workbook
and sheet are an owner-asserted relationship, not a verified revision. Saved cell
locations refer to the copy. Restricting or forgetting either member covers the
original workbook and its saved excerpts within this account and scope; forgetting
still requires the normal impact review. Copy-ID exclusions survive erasure, even
if a later proposal omits the excerpt annotation. Reparenting an existing copy or
copying an already saved excerpt is refused, not silently reconciled. These new
records use manifest format 5; formats 1–4 are not rewritten. An original or copy
with an older, format-3 identity is refused rather than silently migrated; its
exclusions remain effective after erasure. New excerpts share the original's
resource-identity group (format 4).

Inspect every returned receipt and cleanup result. Each file commits separately;
`partial` means some receipts exist even though a later file failed. A saved copy
can exist when temporary cleanup is incomplete. Guarded cleanup removes only exact,
unchanged approved exports; changed or unread inputs remain and require attention.
Windows uses guarded deletion. Linux uses the existing recovery mechanism, in
delete-only mode under the host-pinned export root. It removes unchanged exports
only after atomic relocation into private recovery and a lease/digest check.
An already edited file stays in place; one caught changing during relocation may
move to recovery. Neither pending recovery nor cross-filesystem refusal counts as
successful cleanup. Never delete the user's originals. Qualify the exact export
and cleanup route on the intended browser host; evidence from another host does
not establish its behavior.

Linux import results and `status` include a content-free `export_recovery` report.
The private `source-export-recovery` directory sits beside the source configuration,
outside the archive, vault, Git and synced folders. A private configuration key
authenticates scope/export-root-bound recovery intents; it is not an access grant
and is never returned in a report. The original path is hashed in each intent.
Pending, corrupt or differently bound entries block further cleanup, not evidence
reads. A later import can reconcile authenticated unchanged copies; owner edits
remain for deliberate recovery. Use the returned opaque entry ID to locate
`<config-parent>/source-export-recovery/<entry-id>/payload`. Do not read retained
owner edits into the model or delete them just to clear the warning. Private
relocation does not undo the original shared-cache exposure, revoke existing
open handles, or promise that another process made no copy.

## Google host integration

### Disconnect and recover a connection

Do not confuse disconnection with `disable`, which only pauses source collection.
On a qualified endpoint, `disconnect_google` requests confirmation of the
exact account and OAuth client. The warning matters: provider revocation can
affect this user's other grants across the same Google Cloud project, not just
the current task. Saved evidence is not deleted and delivered content cannot be
recalled. Canceling the confirmation leaves the connection unchanged.

Provider-checked saved Google evidence becomes unreadable while the connection
is disabled, even though the archived bytes remain. Ordinary local retained
documents and images keep their separate access rules. The control applies to
the selected account/client/credential connection, not just the requesting task.

Read `local_access_disabled`, `credentials_removed` and `provider_revocation`
separately. Local disable can succeed while credential removal or provider
revocation remains incomplete. An `uncertain` revocation is not confirmed success;
do not silently retry it. The control remains responsive during acquisition and
prevents the disabled connection from publishing or delivering affected evidence.
Replayed old requests cannot revoke or overwrite a replacement connection.

Remain disconnected by default. `reconnect_google` requests separate warned
recovery after the old local sender has stopped. A delayed provider revocation may
still invalidate the replacement grant. `google_recovery_awaiting_consent` means
fresh Google consent is still needed, not that access has resumed or the earlier
revocation was resolved. Recovery never silently opens sign-in.
After separately completing consent for a user-linked source, use scoped
`inspect` on the existing saved reference to check restored access. Do not capture
another version merely to prove recovery, or reinterpret a capture identity
conflict as a reason to weaken the archive's guards.

The CLI equivalents, `disconnect-google` and `reconnect-google`, require the
approved client, account and private credential root; inspect their actual help.
They are operator controls, not a fallback to bypass an endpoint refusal. These
controls require a loaded endpoint that provides them, a qualified Google host
and a protected reusable credential store. Qualification on one host does not
establish support on another.

### Configure authenticated access

Add `--google-client` and `--google-account` together only for the API route;
`--google-credentials` also requires that pair. Explicit API use without
configuration returns `capability_unavailable`. The private desktop-app client
identifies a maintainer-owned Tedd application; ordinary users should not need
to create their own Cloud projects. Do not borrow credentials from another
plugin or print the client configuration or tokens.

For an explicitly approved reusable connection, also pass `--google-credentials`
with an existing private local directory. Keep it separate from canonical graph
storage, the source archive, Git, Obsidian, cloud sync and network shares. Windows
uses current-user DPAPI; Linux requires an available, unlocked Secret Service.
On macOS, storage uses the user's default file-based Keychain with interaction
disabled. It does not create or unlock a keychain or weaken its item access
controls. Qualify live consent and owner confirmation separately; credential
storage alone does not establish a working Google connection.
There is no plaintext fallback. Missing keyring support is a host requirement,
not permission to save a token in a file. A connection grants no task authority.

Before first consent, disclose `drive.readonly`: Google permits view/download
across Drive (not just the linked folder), but no editing or deletion. Tedd restricts
use to resources linked for this task. For an authorized revocation test, a
separate verified test-only Cloud project, Desktop client and private credential
root isolate grants from other projects, not Drive-data access. Use only the
explicitly approved synthetic linked source; do not treat project isolation as
permission to read other account data. Keep client configuration, tokens and
consent callback URLs out of model messages, terminal output and logs.

Tedd opens a local permission-review page
in the browser, showing the exact account and scope. Choose **Continue to Google**
there, then complete Google's consent in the same tab. No separate native
confirmation window is required. Cancel or timeout refuses sign-in; Codex
interruption also stops the listener. The attempt lasts five minutes. An old
`127.0.0.1` callback that refuses to connect cannot be resumed by refreshing it;
start a new sign-in through the source endpoint, not a firewall or proxy change.
PKCE binds the callback, and Drive verifies the account rather than trusting a
login hint. Saved credentials renew an expired bearer without choosing each file
again. Google may still require renewed consent after expiry or revocation;
`reauthentication_required` means the saved grant was rejected, not a lost source.
Google project status and policy can limit refresh-token lifetime. Consult
[Google's refresh-token rules](https://developers.google.com/identity/protocols/oauth2#expiration)
when diagnosing expiry. Do not change publishing status or organization trust
settings silently.

Without `--google-credentials`, the older one-file Picker remains available.
It requests `drive.file`, which can read/create/edit/delete selected files even
though Tedd only reads. That mode keeps tokens in memory and requires selection
again after expiry. Do not silently change between these permission modes.

A direct user link to a folder permits browsing its descendants for that task;
it does not request a bulk archive. Links in Markdown code/quoted blocks do not
grant access. Keep explicit user exclusions in force. Same-task links survive a
follow-up question while the endpoint runs; other tasks do not inherit them.
Restarting the endpoint retains the sign-in, not task links; resend the needed link.

Explicit “use once” or “do not save/retain” instructions also block Google capture
and new memory bindings before provider access. The block follows selected file
IDs across later turns. A blocked linked folder conservatively blocks Google
retention in that task because a saved source cannot establish its folder ancestry.
Historical evidence reads still require their normal permissions; nothing is erased.
Do not restart the endpoint to bypass a retention instruction.

Google access is bound to the task's selected files/folders and exact graph scope.
Current ancestry is checked before and after acquisition; moved-out, denied or
trashed files are refused. Shortcuts do not extend access to outside targets.
An unsupported file type can appear in a listing without becoming a supported
source. Listing a name is not reading its contents and creates no archived copy.
The finite readers preserve the exact
REST response between matching provider-version checks, plus a separate partial
text reading, not original Google storage bytes or visual-layout fidelity.

The host's `provider_check` must check the actual principal, selected resource,
operation, audience, and applicable copy/download restrictions. Its decision binds
the exact source/version, acquired hash and authorized graph scope. Ownership or
sharing metadata alone is insufficient. Unknown access refuses the operation.
`collecting` is a short reservation of the workflow's real current state, not a
second state database or the caller's `--workflow-state active` assertion. A pause,
closure or exclusion during transfer must prevent publication. Provider calls and
conversion happen outside local locks; publication checks the current local state.

The adapter passes the same host to `evidence_command` and passes `source_host`
to `knowledge_graph_v4_capture.execute_capture` when linking the memory. Lookup,
inspection, human-only `owner-view`, and disputed-memory checks recheck provider access, including any
later-version metadata returned. They never infer current permission from a saved
receipt. Model arguments cannot supply callbacks, credentials or access grants.
Only the host reads the private OAuth client configuration; a source metadata JSON
file cannot grant access. Local-documents approval
alone enables none of this; live provider acceptance remains required. An unavailable host means unavailable capture, not permission
to use a different export or manually manufacture a receipt.
