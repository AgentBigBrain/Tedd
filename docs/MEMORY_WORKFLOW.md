# Keep project memory current

Tedd saves project knowledge in a private database called the graph. Agents can
look up those records in later tasks, and an optional Obsidian view displays them
as linked notes. Saving a record is called capture; looking it up is recall.

Installing the plugin does not turn every conversation into shared memory. A task
can know a person from its chat history while other tasks know nothing about
them. Start with [Codex setup](../README.md#quick-start) or
[Claude Code setup](CLAUDE_CODE_SETUP.md), then choose when Tedd should save useful
knowledge. Both hosts can use the same private profile and graph on one computer.
Each task still needs its own approved project scope and reading position.

## Make Tedd part of everyday work

Invoke Tedd in the host you use, then ask it to save relevant project knowledge
when something lasting happens, such as a decision, completed fix, or new
obligation. To make that your default, add a standing instruction to the
appropriate file:

| Host | Start Tedd | Personal instructions | Project instructions |
| --- | --- | --- | --- |
| Codex | `$tedd` | `~/.codex/AGENTS.md` | `AGENTS.md` in the project |
| Claude Code CLI or the Claude desktop app's Code tab in Local mode | Type `/tedd:tedd` directly | `~/.claude/CLAUDE.md` | `CLAUDE.md` or `.claude/CLAUDE.md` in the project |

Here `~` is your home directory: `/Users/<you>` on macOS or
`C:\Users\<you>` on Windows. A custom `CODEX_HOME` or `CLAUDE_CONFIG_DIR` changes
the respective personal location. Use personal instructions for defaults across
your projects, or project instructions for that repository alone. This is an
explicit choice; installation does not add it. Merge with existing instructions
rather than replacing them.

For example:

```text
Use the installed Tedd skill by default for local project work, including follow-up
turns. Use the existing shared graph with exact project scope. Save concise,
evidence-backed milestones: relevant people and relationships, decisions and labeled proposals,
material constraints, verified outcomes, and concrete obligations. Follow Tedd's
reviewed capture procedure, verify the saved records, and refresh the configured
review-safe Obsidian view. At handoff, say if important knowledge remains unsaved.
Honor read-only, no-capture, use-once, and don't-save instructions. Do not save full
chats or secrets, collect unrelated history, or enable source retention. If Tedd
is unavailable, report it instead of pretending memory is working.
```

Start a fresh session after editing instructions and check that the intended
files loaded. Existing sessions need the instruction supplied explicitly; a file
edit alone does not establish that they adopted it. Codex override files and more
specific project instructions can affect the result. See the official
[Codex instruction guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
and [Claude instruction guide](https://code.claude.com/docs/en/memory#where-claudemd-files-live).

In Claude, enter `/tedd:tedd` directly at the start and after a fork, clear, or
compaction so Tedd can check the current session's identity and approved binding.
Standing instructions do not replace that check. A task with an unresolved
identity or project scope should report memory as pending.

In both hosts, Tedd previews the proposed records, reviews them against the
evidence, and applies the changes within your saving instructions. Each change
retains its author, task, and revision history. If you use both hosts, put your
chosen saving policy in both instruction files. Neither file grants access to
unrelated projects or authorizes new source retention.

This remains an agent workflow, not an automatic transcript collector or a
guarantee that every model will follow it. Tedd's optional graph tool connection
provides reads only. The separate source service controls access to selected
evidence; it does not extract people, decisions, or obligations from every
conversation and save them as memories.

## Review unfinished work at task start

RC10's overdue-review feature helps you notice unfinished commitments in
the task's approved project scope. Check [current availability](sources/qualification.md#current-availability)
before relying on it in your installation.

At task start, Tedd can show confirmed or proposed commitments whose next
checkpoint has passed. A checkpoint is the saved date for checking progress.
Items without one become eligible more than 30 days after they were first
observed. Editing a note does not reset that age. Proposals remain proposals,
and missing owners or dates stay unknown.

Tedd gives one short summary, such as "3 overdue items for this project", then
continues your request. It asks what to do only when an item affects the work.
For example, an overdue request for a QA receipt can affect a release decision;
it need not interrupt an unrelated copy edit. You can choose to resolve the item,
reschedule it, hand it off, withdraw it, or record a blocker. Tedd may recommend
a choice from the evidence, but reading the list does not change any commitment.

The summary holds up to five items and 1,500 characters. Recent memory records
get space first within the overall context limit, so fewer items may fit.
When a list is shortened, Tedd says "at least" rather than presenting a total.
An omitted summary does not mean there is no overdue work.

For a more focused check, ask: "Review overdue commitments in this task's
existing scope. Show their recorded status and next action without changing
anything." The read-only `loops-review` command returns up to 20 items in that
same scope. The [command reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/recall.md#review-overdue-items)
explains its inputs, result fields, and limits.

When saving a new commitment, include its next checkpoint if one is known.
An unknown date is allowed; Tedd flags the missing checkpoint for review
instead of inventing one.

## See what is true now

The three sections that follow describe features newer than the 1.2.0-rc.12
package. Check [current availability](sources/qualification.md#current-availability)
before relying on them in your installation.

At task start, Tedd can show a short fact sheet for the task's approved project
scope. Each line is a current fact: something with one value at a time, such as
a site's hosting provider or a client's main contact. When the value changes,
Tedd saves the new one and keeps the earlier one in history.

The sheet shows when each fact took effect and when it was last confirmed. A
fact is confirmed when evidence Tedd verified, or your own statement, restates
the same value. That date tells you how fresh the fact is. It does not prove
the fact still holds, so Tedd rechecks anything that may have changed before
acting on it. When two values for one fact are both current, the sheet flags
the conflict instead of choosing one.

The sheet holds up to 12 facts and uses at most a quarter of the task's context
limit, ahead of recent records. A shortened or missing sheet does not mean
nothing is known.

Facts are not only for code. Ask Tedd to save a person's role, how someone
prefers to be contacted, what a client agreed to, or a renewal date. Tedd saves
only what your statement or the evidence supports, and leaves an unknown role
or date unsaved.

## Answer what is waiting

Beside the fact sheet, Tedd reports how many commitments are unfinished, how
many of those are overdue, and how many proposals nobody has ruled on. Ask:
"Show me what is waiting in this project."

Tedd lists commitments with overdue ones first, then proposals with the oldest
first. Each commitment is marked as unassigned, held by an agent, or held by a
named person, so you can tell what waits on you from what waits on someone else.

Answer in plain words, several at once. For example: "The report is done, it
went out Tuesday. Drop the audit, the client cancelled. Move the renewal to
Friday. Hand the contract to Sam. Accept the caching proposal and reject the
rewrite."

| Your answer | What Tedd saves |
| --- | --- |
| Done, with what happened | The commitment, closed with that outcome |
| Drop it, with a reason | The commitment, closed as withdrawn with your reason |
| Later, with a date | A new checkpoint for the commitment |
| Hand it to someone | That person as the commitment's owner |
| Accept | The proposal as an accepted decision or an open commitment |
| Reject | The proposal as a rejected decision |
| Replaced by another decision | The earlier decision marked replaced and linked to the later one |

A decision can only be replaced by one that has been accepted. You can accept
the later one in the same answer. Tedd refuses to replace a decision with a
proposal nobody ruled on, or with one that was rejected.

Tedd reviews the batch and saves it as one change, with your statement as its
evidence. Only your answer settles an item: Tedd does not answer for you or
read agreement into silence. Nothing is deleted, and each record keeps its
history. The
[agent procedure](../plugins/tedd/skills/obsidian-knowledge-graph/references/capture-and-correction.md#answer-the-review-queue)
lists the exact answers and limits.

## Fold old notes into current facts

A long-running project collects many dated notes. Ask: "Fold this project's
notes into current facts." Tedd lists the notes that no fact cites yet, grouped
by topic. It drafts a small set of facts, each citing the notes it rests on,
and shows you one batch at a time.

After you approve a batch, those facts appear on the fact sheet. The cited
notes leave the recent-records list for tasks that can read the facts. The
notes themselves are unchanged and still appear in search.

A summary can drop a condition. Check that each drafted fact keeps words such
as "not" and "only when" from its notes. When notes disagree, Tedd records the
disagreement instead of choosing a winner. A note that is uncertain, or that no
fact fully states, stays unfolded. A summary Tedd only inferred cannot fold
notes: that save is refused and hides nothing.

## Keep preferences scoped and current

A saved preference is a default you have stated, with a condition and a reason.
It can apply to one project, one client, or be a personal default shared across
projects you choose. That last option is called a global preference scope and
requires approval for each project. Tedd does not infer preferences from your
habits. Check [current availability](sources/qualification.md#current-availability)
before using this feature in your installation.

To save a default, name the project or client and when it should start, then
give all three parts. For example, ask Tedd to save this for the Alpha project
from today:

```text
Prefer: short bullet lists.
When: writing project status updates.
Because: they make next actions easier to scan.
```

`Prefer` says what to do, `When` limits where it applies, and `Because` records
your reason. Tedd reviews and saves the statement with its subject and date.
A task can use it only when that subject is in its approved memory scope.
A preference for one client does not become a default for other clients.

Your current request takes precedence. "Use numbered steps for this task only"
overrides a saved bullet-list default without changing that preference. Tedd can explain
the difference and offer a standing update. Only when you confirm that update
does it save a replacement and retain the earlier value in history.

Dates determine which value applies. If you ask for a new default from
2026-11-01, the earlier default remains effective until then. When multiple
values for the same preference are effective at once, the summary flags a
conflict. Tedd follows your current request and asks which default should stand
when needed.
The summary can be shortened by its size limit, so missing detail does not
mean no preference exists. The [recall reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/recall.md#stated-preferences)
explains the fields and limits.

To share a personal default with another project, ask Tedd to preview adding
your global preference scope there. The [setup procedure](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#optional-global-preferences)
checks whether that scope exists. If it is missing, you review and approve
the proposed scope record before Tedd saves it through the capture workflow.
Setup itself never saves graph records. You then approve the project's updated
memory settings. Existing tasks keep their subscriptions, and client knowledge
keeps its own scope.

Obsidian's Current and Historical groups follow saved record status, while task
context uses effective dates. A scheduled replacement can appear under Current
before its start date; check both its start and ending dates. This feature does
not change those Obsidian groups or their counts.

## Finish with a memory handoff

RC9.2 adds checked receipts and reminders to the saving
workflow below. See [host qualification](CLAUDE_CODE_QUALIFICATION.md#rc92-selective-saving)
before relying on reminders in your installation. Codex needs explicit trusted
hooks: follow the [Windows setup](WINDOWS_SETUP.md#check-handoff-hooks-in-an-rc92-test-installation)
or [Mac test setup](MACOS_TESTING.md#check-selective-saving-and-reminders).
Installing the portable plugin alone does not enable these reminders.

Before finishing work that used memory, Tedd selects lasting outcomes:

- Decisions, marked accepted or proposed.
- Approvals, attributed to the person who gave them.
- Verified results.
- Reusable fixes, with the symptom, cause, fix, verification, and when to use it.
- Concrete obligations with a known owner or date.
- Corrected facts, retaining the earlier record's history.

Tedd saves compatible outcomes together after reviewing the proposed records.
Your standing saving instruction covers ordinary milestones; it does not require
another approval each time. Uncertain items stay in the supported candidate
categories, with their uncertainty visible. Routine edits, repeated tests, and
conversation do not need saving.

The closing report uses this form:

```text
Memory: saved N (ids), candidates M (ids), not saved: <reason or none>
```

The counts refer to meaningful outcomes, not every supporting record created
alongside them. A saved proposal is still a proposal. "Nothing durable to save"
is a valid result; a failed or unverified save must be reported as pending.

When the handoff hooks are loaded and trusted, Claude can pause a normal ending
once so Tedd can finish that report. An honest pending result can end the turn;
the reminder cannot require a forbidden write. Codex warns without forcing a
continuation. Neither hook saves knowledge or reads a transcript.

An interrupted turn can leave one reminder for the next request. Tedd handles
the new request first and honors any new limits on saving. Resume and compaction
retain pending reminders; a fork or clear starts without the parent's reminder
and needs its own checked scope. A reminder is never permission to save.

### Choose what may be saved

You can set these limits in your request; no configuration flag is needed for
Tedd to honor them.

| Your instruction | What Tedd does |
| --- | --- |
| "Save useful project milestones." | Reviews and saves supported outcomes within the approved project scope, then checks them. |
| "Don't save this" or "use this once." | Excludes that content from memory. A private receipt may record that nothing was saved, without repeating the excluded detail. |
| "Keep this task read-only." | Reads approved records without changing graph memory. A private operational receipt or task tally is still permitted. |
| "Do not write any files." | Writes no memories, receipts, tally entries, or session state itself. Reports the missing receipt as pending because of your instruction. |

Hooks run before Tedd reads your prompt. To also prevent their automatic local
state writes, set `TEDD_NO_LOCAL_WRITES=1` in the host's environment before
starting it. For a terminal session on macOS or Linux:

```bash
TEDD_NO_LOCAL_WRITES=1 codex
```

Use `claude` instead of `codex` for Claude Code. In PowerShell, set the variable
first, then start either host from that window:

```powershell
$env:TEDD_NO_LOCAL_WRITES = '1'
codex
```

The PowerShell setting lasts for that window. Close it, or remove the variable
before starting a later session with `Remove-Item Env:TEDD_NO_LOCAL_WRITES`.
An already running desktop app needs to be restarted with the intended environment.
This opt-out silences Tedd's handoff hooks and disables its receipt commands;
it is not a filesystem restriction on the host or other tools.

## Find an older memory

Use the optional graph connection for memory search. Check that the connected
server advertises `search_memory` before following this procedure.

Recent context is a starting point, not a complete project history. Ask:
"Find the recorded logo approval for this project." Tedd can
search for `logo approved` within the task's existing subscription, which selects
the records that task may read. Search does not expand that selection; a
deliberately broad subscription remains broad.

Text searches match saved words without regard to case; similar meaning alone
is not a match. Search first looks for records that contain every word. For
`logo approved`, a record with both words is a full match. An exact record ID
takes priority; other full matches appear by most recent update, then ID.

When no record contains every word, Tedd returns partial matches instead of an
empty list. It sets aside common words such as `what` and `the`, then lists the
records that contain the most of the remaining words first. The result is
marked `match_mode: any_terms`, and each record shows the words it matched.
Treat a partial match as a lead to check: a record that matched `logo` but not
`approved` does not show that the logo was approved. To accept full matches
only, ask Tedd to search with `match` set to `all`.

Partial matches are newer than the 1.2.0-rc.12 package, which returns an empty
list when a word is missing. Check [current availability](sources/qualification.md#current-availability)
before relying on them in your installation.

Search covers compact fields such as names, titles, aliases, summaries, and
values. It does not read archived documents or entire conversations. Only the
latest saved revision of an eligible record is searched.

Words are compared whole, so `approved` does not match `approval`. If word
endings explain a missing match, Tedd can retry once with an explicit prefix:
`logo approv*` finds both `logo approved` and `logo approval`. Use at least four
ASCII letters or digits after normalizing case, followed by one `*`, without
quotes or punctuation in that prefix. A full match still needs every other
word. `harbor*` could find both `harborstudio` and `harborside`; that does not
make them the same client. Try another relevant literal query if needed, while
preserving the project's scope and the meaning of the question.

Check each result's status and evidence before acting. A record can reliably
describe a proposed decision without establishing that anyone approved it.
The search result preserves that distinction. Sensitive records, records removed
by privacy controls, and records whose current content is unavailable are excluded;
the remaining output follows the graph's review-safe disclosure rules.

By default, a response contains at most 10 records and 12,000 characters of
result-list JSON. `result_count` is the number returned. `truncated: true` means
the count or character limit left out matching results; a very small character
budget can leave the list empty. Ask Tedd to check those limits or try fewer or
different words when a result is missing. Even an empty, untruncated result does
not prove that a memory never existed.

Search does not save memories or mark inbox changes as read. The
[agent search reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/recall.md#on-demand-mcp-search)
contains the request example, exact limits and result fields. Use
[saved-source checks](sources/usage.md#check-a-disputed-memory) when a
remembered claim is disputed.

## Check that memory reached the graph

After a milestone, ask Tedd to show which records it saved and to recall a
distinctive person, decision, or outcome from a separate task. A useful check
verifies the record's meaning and evidence, not just a successful command.

If the record is missing, distinguish these cases:

| What you find | What to check next |
| --- | --- |
| Present in chat, absent from saved memory | Was Tedd active? Was a capture attempted, refused, or left pending? Recover only supported facts from the relevant evidence. |
| Present in saved memory, absent from the Obsidian view | Check the selected vault, the graph revision used for the view, and whether privacy rules or the selected records exclude it. Ask Tedd to refresh the view if needed. |
| Present in notes, absent from Obsidian's graph display | Check the open vault, Obsidian search and graph filters, and note links. |

Check that the saved records contain the outcome itself, including important
conclusions. An archived source, an up-to-date Obsidian view, or a delivery
receipt can help you check those records, but does not replace them.

### Check a capture receipt

A handoff receipt is a private record of which graph changes a prompt saved.
It lives in `~/.codex/tedd/handoffs/`, outside the graph and Obsidian, and contains
identifiers and revisions rather than conversation text. Tedd creates it from
the capture journal, the graph's history of committed changes.

Ask Tedd to check the receipt for the current task and prompt:

| Result | Meaning |
| --- | --- |
| `verified` | The saved revisions and their original authorship match the graph's journal. Read the records too, to check that the important knowledge is there. |
| `agent_asserted` | Tedd reported nothing to save. This is an explanation, not proof of a graph write. |
| `pending` | The receipt is missing or could not be published. Check whether the memory itself was saved before attempting recovery. |
| `unverified` | The receipt does not match the journal or its ownership. Do not report it as proof of saving. |

If memory was saved but the receipt failed, repair the receipt from the existing
journal entry. Do not save the same knowledge again just to create a receipt.
A duplicate retry retains the original saving run and does not count as another
save. Receipt checking does not prove that Tedd selected every important outcome.

For manual checks, use the [receipt command reference](../plugins/tedd/skills/obsidian-knowledge-graph/references/capture-and-correction.md#optional-prompt-handoff-receipts).
It explains `capture --handoff`, `handoff --check`, and explicit reconstruction,
including the exact session, prompt, consumer, and agent identifiers they need.

## Recover a missed milestone

Ask Tedd to review the affected task and save the missing durable context. It should
reuse existing project and person identities, retain source references, distinguish
reported facts from independently verified state, and keep proposals separate from
accepted decisions. Repeated mentions alone do not establish a person's job title
or authority. Do not import an entire transcript or silently close old obligations.

Tedd should review the proposed capture before applying it, verify exact saved
records, validate the graph, and refresh the configured Obsidian view. Close or
navigate away from generated Obsidian tables, called Bases, before a refresh so
an open table cannot overwrite the new file with an older version. Larger
recoveries also require the independent review defined in the graph skill.

The exact procedures live in the [graph skill](../plugins/tedd/skills/obsidian-knowledge-graph/SKILL.md)
and [memory lifecycle](../plugins/tedd/skills/tedd/references/knowledge-graph-lifecycle.md).
