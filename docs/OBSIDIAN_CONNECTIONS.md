# Follow Tedd's saved connections in Obsidian

Tedd stores memory in a local database called the knowledge graph. Its Obsidian
view is a generated copy of permitted records, called a projection. Start with
a person or project, follow its linked records, and inspect the evidence
behind them. Changes to memory go through Tedd; editing these generated notes
does not change the database.

## Start with a person or project

After Tedd has [generated the view](../plugins/tedd/skills/obsidian-knowledge-graph/references/projection.md#projection-workflow),
open its configured Obsidian vault, the folder that holds your notes. Open
`00 Dashboard.md` inside the generated folder, then choose
**People and projects**. New configuration defaults to `Tedd Knowledge`; use your
configured folder if it differs.

Existing `Codex Knowledge` and custom folder names remain unchanged on upgrade.
Setup does not move generated notes or rename your live folder.

Each person or project note groups the records Tedd has connected to it:

- **What Tedd remembers** separates current statements, items that need checking,
  and historical or replaced statements. If none are selected, the page says so
  without implying that the person or project has no other saved records.
- **Relationships** explains the direction and status of each saved connection,
  how it was recorded, and its evidence. Tedd does not infer the reverse connection.
- **Decisions** separates accepted decisions from proposals and historical items.
- **Open commitments** shows the recorded owner and next action for active work,
  a proposed action for candidate work, and the outcome and completion date for
  resolved work. Earlier actions remain in the technical details as history.
- **Recorded observations** links directly to wording captured about the subject.
  An observation records what was reported; it does not automatically become a
  current claim.
- **Recent activity** links events and verification receipts.
- **Evidence used** links the compact evidence notes permitted in this view.

Follow a linked statement, decision, commitment, event, receipt, or observation.
Each note links back to the saved people, projects and other subjects, and to the
evidence available under the current privacy mode. Tables of these notes are
called **Bases** in Obsidian.

The dashboard links to people and projects, active work, proposals that need
attention, recent decisions, and recently recorded evidence. Expand the details
below those sections for snapshot information and record counts. Active and
completed work open the commitment table.

### A short example

Suppose the selected snapshot records that Mira is working on Harbor. Harbor's
page can link to the exact observation “Mira will review the draft,” an accepted
decision to review it, a still-open review task, and an earlier task whose recorded
outcome says the draft was archived. The proposal to add another reviewer remains
under **Proposed or needs checking**. These links make the history inspectable;
they do not turn the proposal into approval or prove that the observation is true.

## Read the record type before trusting the wording

The notes preserve distinctions that matter:

| Record | What it means |
| --- | --- |
| Observation | Wording or a value recorded from a source; it is not automatically current or accepted. |
| Remembered statement | Tedd's compact claim. Its lifecycle says whether it is current, uncertain, or replaced. |
| Citation | The saved evidence lineage for a record; it does not prove Tedd interpreted it correctly. |
| Proposed decision | A suggested choice awaiting approval. |
| Accepted decision | A decision stored with accepted authority. |
| Resolved commitment | A closed loop with its recorded resolution; it remains part of history. |

When wording matters, ask Tedd, “Are you sure that is what the source says?” Tedd
should check the selected memory and its exact saved evidence. A challenge does
not silently rewrite memory; a supported correction uses the normal reviewed
capture workflow.

## Understand what may be absent

`review_safe` is the normal projection mode. It withholds or redacts sensitive
content. A hidden record cannot gain a new link or table entry through a visible
person, project or relationship. Existing sanitized notes may remain, and the
dashboard's totals can still count records that are not displayed.

The dashboard reports the saved database revision, snapshot date, privacy mode,
and whether selection was complete or bounded. A bounded snapshot includes the
selected records and the permitted records they reference. Other records that
point back to them are not automatically added.

Entity note summaries show at most 25 permitted linked notes per section. When a
summary has more than five selected rows, expand its **More…** callout to see the
rest. A shortened-summary notice stays outside the fold. Every omitted permitted child note still
links to the entity, so backlinks and the local graph can reach it. Matching
entities, claims, observations, events, decisions, and open loops also remain in
their Bases. Receipts do not currently have a dedicated Base; omitted receipt
notes remain reachable through backlinks and the local graph. Selected
relationships are not shortened.

## Use the tables as questions

Each Base has one clickable wording column and up to four useful details.
Its columns include Name, Kind, Context and
State for people/projects; Statement or Recorded wording with About, date and
Source for memories; and Owner, Next step, Check-in, Due or Outcome for
commitments. Source describes how the record was saved; it does not establish truth.
Candidate authority and conflicts continue to say that they need checking.

Two people can have the same displayed name and still remain separate linked
notes. Context is shown only when it is ordinary text or a selected, permitted
entity. Otherwise the table says **Context not shown** instead of exposing an
internal identifier or guessing.

Captured wording that looks like an Obsidian `[[link]]` or `![[embed]]` is shown
with spaces in table display fields so it cannot create an unrelated graph edge.
The note body keeps the permitted recorded wording, and canonical memory is
unchanged.

## Optional quieter Reading view

Generated notes carry the `tedd-reading` class. Their content and metadata remain
usable without styling. The packaged
[projection procedure](../plugins/tedd/skills/obsidian-knowledge-graph/references/projection.md#optional-local-reading-style)
contains a small, optional CSS starting point that hides only the redundant inline
filename and properties block in Reading view. Enable or disable snippets through
Obsidian's normal **Appearance → CSS snippets** control. Tedd does not write
`.obsidian`, install the snippet, or change your theme. Source mode and the
Properties sidebar remain available.

The selector was checked in a disposable vault on Windows with Obsidian 1.13.7,
its built-in dark appearance and an approximately 1024 × 800 window. Other themes
and hosts may differ; the notes and tables work without the snippet.

## Focus the native graph without changing memory

For a quieter visual graph, open a person or project note and choose Obsidian's
local graph. Set a small link depth first. If receipts or the dashboard dominate
that particular view, add filters like these and replace `<generated root>` with
the folder shown by your configured dashboard:

```text
-path:"<generated root>/00 Dashboard.md" -path:"<generated root>/21 Receipts"
```

Those filters hide nodes only from the chosen Obsidian graph view. They do not
delete records, alter recall, change privacy rules, or rewrite Tedd's generated
files. Tedd does not modify `.obsidian` settings, saved groups, or plugins when it
refreshes the projection.

## Troubleshoot the right layer

If expected knowledge is missing, identify the failing layer before changing
anything:

1. **Capture:** Ask Tedd whether the person, relationship, decision, or milestone
   exists in canonical memory. Projection cannot display a record that was never
   saved. If someone is repeatedly involved, check that their contribution was
   recorded without importing a transcript or inferring their role.
2. **Projection:** Confirm the dashboard revision matches the intended graph and
   refresh through Tedd's normal `review_safe` projection procedure. A bounded
   selection or privacy rule can intentionally omit a record.
3. **Obsidian:** Clear filters and open the generated note or Base again. Before a
   refresh, navigate away from generated `.base` tabs so Obsidian cannot write a
   stale in-memory view over the new file.

Generated notes may be rebuilt. Editing them does not correct canonical memory;
make corrections through Tedd. Native source previews are a separate capability
and are not enabled by these links. Evidence notes contain only the compact graph
evidence permitted in the generated view. They do not contain private archive originals.
