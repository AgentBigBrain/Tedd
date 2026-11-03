# Source-backed memory

Tedd can remember a support recommendation and keep the exchange it came from.
If you question that memory later, it can check the original wording, surrounding
context and any later versions it has saved.

The optional source archive stores that evidence privately on your computer.
When Tedd retrieves a memory, it returns the concise record and leaves the full
source in the archive until it is needed.

## Start here

| I want to… | Read |
| --- | --- |
| Use source memory in an already configured installation | [Using saved sources](usage.md) |
| Connect Codex to the source service and document readers | [Source setup](setup.md) |
| Try a complete local example with test data | [Local source walkthrough](local-example.md) |
| Check formats, limits or command meanings | [Source reference](reference.md) |
| Check tested platforms and available features | [Qualification and availability](qualification.md) |
| Understand how memories and sources fit together | [Tedd architecture](../ARCHITECTURE.md) |

Check [current availability](qualification.md#current-availability) for the
features and tested routes in your build. Supported PDF, Office and image
operations on a Mac require the optional isolated reader. Plain UTF-8 text,
Markdown and code do not. Collection requires explicit setup; installing Tedd
does not enable it.

## What can be preserved

Tedd can save selected local text, Markdown and code, plus PDF and Office files.
Other options include supported chat exports, selected text from a page, and
separately configured Google access or browser exports that you review and approve.
Desktop attachments use the exact readable file delivered with the task.

What Tedd can read depends on the format and how it was acquired. Selected text
captures only that passage. A spreadsheet reading may include a formula and its
last saved value without reproducing the workbook's appearance. Embedded Office
images and standalone images have different format limits.

With qualified image helpers, Tedd can review and preserve screenshots,
photographs and other [supported images](reference.md#supported-image-formats).
It keeps the exact original, including permitted metadata, and uses separate PNG
views for review. Multi-frame or multi-page files need review of every retained
part; small wording or numbers may need a detail view. A screenshot visible in a
conversation still needs an accessible original file before it can be preserved.
See [using images](usage.md#preserve-and-check-an-image) for the steps.

Tedd retains useful evidence and the context needed to interpret it. Reading a
folder, opening a tab or receiving incidental tool output is not a request to save
everything encountered.

## What stays separate

- The **graph** is the database of memories, decisions and relationships.
- The **archive** holds preserved source bytes, versions and derived readings.
- The **private viewer** displays permitted evidence to the owner.
- The **Obsidian projection** is a generated copy of permitted graph records as
  linked notes and tables in the Obsidian notes app. Editing it does not change
  saved memory.

The private viewer supports qualified Windows/Linux text and image workflows,
including selected PDF and rendered Office pages. Native Obsidian source previews
remain disabled. Saving a source, displaying it to a person and exposing it to a
model are separate permissions.

Google API sources also need current authorized provider access when read from
the archive. [Disconnecting Google](usage.md#disconnect-and-recover-google-access)
keeps the saved bytes but blocks those reads until access is restored. Ordinary
local evidence remains governed by its own permissions.

A saved copy may remain available after the original moves or disappears,
subject to its access policy. The source can still be wrong, incomplete or out of
date. Checking it can correct the current answer; changing the stored memory
requires a separate reviewed correction.

For agent instructions, start with the
[packaged source procedure](../../plugins/tedd/skills/tedd/references/source-backed-memory.md).
