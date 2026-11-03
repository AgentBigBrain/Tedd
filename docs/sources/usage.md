# Using saved sources

Once [source setup](setup.md) is complete, you can ask Tedd to save useful evidence
and check it later. Collection is opt-in. See [qualification](qualification.md)
for tested formats, platforms and installation requirements.

Tedd stores a concise memory in its graph database and keeps the selected
evidence in a separate private archive. The configured source service connects
Codex to that archive. You can ask for evidence to be saved, a memory to be
recorded, or both; these are separate operations.

## Preserve useful evidence

Give Tedd the file, attachment, selected text or supported source link needed for
the task. State any exclusions and whether the material should be kept. For example:

> Use this support transcript to resolve the retry issue. Keep the answer and the
> surrounding exchange so we can check the advice later. Exclude billing details.

Tedd saves evidence that supports a fact, supplies needed context, or fulfills
your request to keep it. Access to a folder lets Tedd find relevant files without
archiving the whole folder. Say “Use this once” when you want the material used
for the task but not saved.

Tedd can save selected text or a whole text file when it is needed. PDF and Office
files are saved in full, even when Tedd reads only part of them. A partial reading
does not remove anything from the saved original. If the file contains material
you have excluded and the acquisition method cannot leave it out, do not retain
the file.

For Desktop attachments, Tedd uses the file delivered with the task, whether it is
the original or a copy managed by Codex. The file must be readable; Tedd will not
guess a replacement or search private caches. If the source service restarts,
wait for it to reconnect and resend the selection once. Previously saved sources
can remain available, but the service loses the task's current selection.

A successful preservation response includes a receipt identifying the exact
saved copy. It also reports coverage: which parts Tedd read or reviewed, and
which parts remain unexamined.
An exact repeat can reuse an existing copy. If Tedd reports a refusal or incomplete
result, check it before relying on the saved source. Reading a file successfully
does not mean it was preserved. The
[result reference](reference.md#results-and-recovery) explains the common outcomes.

## Preserve and check an image

Image preservation requires the source service and image-reading helpers tested
for your computer; check [availability](qualification.md#current-availability)
for your build.
Supply the exact readable image file or attachment. An image visible
in the conversation may have no accessible original. In that case, Tedd should
explain what it could see and what it could not save, without searching private
caches or treating a similar file as the original.

For an ordinary screenshot, a useful request is:

> Read the retry setting in this screenshot, including the note beneath it.
> Keep the original so we can check the setting later, and remember the setting
> together with that condition.

Tedd first reviews an overview, then requests readable detail for small words,
numbers or symbols. It reviews every retained frame, page, image item or icon
variant, including any required uncropped view. It acknowledges the views it
actually inspected before saving the whole original. If the file changes, the
new bytes need fresh review. Cropping a view does not remove excluded material
from the saved original; do not retain a file whose full contents are not permitted.

For human-only material, use the separately approved private viewer to review
and confirm it yourself. That confirmation records your review; saving still
requires retention permission. It does not grant the model permission to see the
pixels or read their contents. If the private viewer is unavailable, model display
cannot replace it. Canceling review prevents that preservation attempt.

Check what actually completed:

| Outcome | What you can rely on |
| --- | --- |
| Referenced | Tedd has a path or link; this alone proves neither reading nor preservation |
| Read or reviewed | The stated views were inspected, with any unreadable detail or missing context identified |
| Saved | A receipt identifies the exact original kept in the archive |
| Memory-linked | Tedd saved a separate memory after reviewing it, with a link to that source |

If the memory write fails after preservation, the saved source remains available.
Later, ask Tedd to check the saved image and the relevant detail against the memory.
Saving an image does not make its contents searchable as extracted text or by
visual similarity. Previews also have
[color, timing and encoded-byte limits](reference.md#supported-image-formats);
keeping the original is not proof that every byte or detail was inspected.

## Connect the source to a memory

Preserving evidence and recording a memory are separate steps. Tedd creates a
concise memory and links it to the exact saved source version and location.
It reviews the proposed change before saving the memory. When Tedd recalls it
for a later task, it returns the summary; the full document stays in the archive.

For example, a source might say, “Do not stop the worker while jobs remain queued.”
A useful memory retains that condition. The saved passage and surrounding context
allow a later reader to catch a summary that accidentally drops “not.”

The source link identifies the bytes Tedd saved. It does not establish that the
author was correct or that the advice still applies. Tedd identifies missing
context, incomplete readings and unknown authorship rather than filling in the gaps.

## Check a disputed memory

Ask a direct question, such as:

> Are you sure support said to stop the worker? Show me what the saved exchange says.

Tedd compares the memory with the saved source, nearby context and relevant later
versions it has captured. Look for an answer that explains whether the evidence
supports, contradicts or leaves the claim unresolved, and what context is missing.

The default evidence response allows 12,000 characters and
five source versions. If that is insufficient, Tedd explains the gap before
broadening the check. A denied or unavailable source is not evidence that the
memory is false.

Checking can correct the current answer. Updating the stored memory requires a
separate reviewed correction. Tedd can also inspect saved sources that have no
linked memory.

## View evidence yourself

Ask Tedd to open the saved evidence in the private viewer. Viewing requires your
approval and a supported Windows/Linux desktop. With the appropriate helpers,
the viewer can show text, standalone images and detail views, embedded Office
images, or selected PDF and rendered Office pages. Opening a saved view does not
itself confirm that someone read it.

The viewer can show evidence to you without granting the model access or returning
the displayed content to it. It cannot protect against screen recording,
administrators or unrestricted software running as the same user.

Obsidian's generated graph notes remain separate. Native Obsidian source previews
are disabled because they did not pass the required privacy checks.

## Control retention and access

| Request | Effect |
| --- | --- |
| Use this once | Use permitted content for the task without preserving it |
| Disable source collection | Stop new capture; keep saved history |
| Disconnect Google | Disable that API connection and attempt credential removal and provider revocation; keep archive bytes |
| Deny model access to retained local documents | Keep the archive while preventing the model from reading those documents within the selected scope |
| Restrict this source | Deny access across the logical source's versions; there is no unrestrict command |
| Forget this source | Review the affected memories and source versions, then confirm that deletion |
| Allow me to view this evidence | Approve viewing for yourself without granting model access |

Other copies can remain after forgetting, including bytes shared by retained
sources, earlier backups and originals outside Tedd. Removing one memory also
does not necessarily remove evidence used by other memories.

Cleanup does not delete user originals or Codex-owned attachments. If an exported
copy changed, or recovery is pending, Tedd preserves the affected bytes and reports
the unfinished cleanup. The operator follows the
[maintenance procedure](../../plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md#pause-forget-and-recover-deliberately).

## Google sources

There are two setup options. The authenticated API reads structured content from
Google and checks your current permission for the selected resource. The browser
export option saves an ordinary Office copy after you review and approve the batch.
For an export, you confirm the origin and account; Tedd does not independently
verify its Google revision or current permissions.

Reusable API sign-in requests Google's `drive.readonly` scope, which permits
Drive-wide reading and downloading. Tedd limits its actual use to the files or
folders linked for the task. A dedicated Cloud project can isolate which grants
revocation affects; it does not narrow that Drive permission. Review the account
and requested scope before consenting.

Browser exports pass through the browser's storage before Tedd imports them
privately. That storage may be shared, so this option is limited to ordinary
content. Do not use it for sensitive material or to work around an API denial.
See [host integration](../../plugins/tedd/skills/tedd/references/source-host-integration.md)
for consent, selection, exclusions and cleanup.

### Disconnect and recover Google access

The configured source service must provide `disconnect_google` and
`reconnect_google`, with a tested Google connection and protected reusable sign-in.
Ask Tedd to disconnect Google, then review the account and OAuth client, the
application requesting access, in the confirmation. Revocation can affect your
other grants across that application's Google Cloud project. Canceling the
confirmation leaves the connection unchanged.

After confirmation, Tedd disables local access, removes the saved credential and
attempts provider revocation. Read the result for each step: local access can be
disabled even if credential removal or revocation is incomplete. An uncertain
revocation stays uncertain; it is not silently retried.

Disconnecting keeps archive bytes. Provider-checked saved Google sources become
unreadable until current authorized access is restored. Ordinary local images and
documents keep their separate access rules. Disabling source collection instead
only stops new capture; it does not disconnect Google or delete history.

To restore access, request recovery and review its separate warning. Tedd first
checks that the old local revocation sender has stopped. A delayed revocation at
Google may still invalidate a replacement grant. Agreeing to recovery prepares
a new sign-in; it does not complete consent or resolve earlier uncertainty.
Complete fresh Google consent for an explicitly linked source, then check the
saved source through the configured source service. Recapturing it is not required to
verify restored access. If access remains denied, keep the saved reference and
resolve the connection rather than choosing a weaker acquisition method.
