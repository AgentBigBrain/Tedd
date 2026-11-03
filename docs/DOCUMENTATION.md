# Maintaining Tedd documentation

Update documentation with the behavior it describes. A reader should be able to
understand the current system, find the right procedure, and recognize the limits
of the evidence without reading the development conversation.

This guide applies to the README, contributor and security guides, `docs/`,
and the prose shipped with Tedd's skills. The [documentation index](README.md)
is the catalog of those documents and their purpose.

## Choose the document by its job

| Kind | Contains | Keep elsewhere |
| --- | --- | --- |
| README | Product purpose, requirements, first steps and links | Full command reference and development history |
| User guide | A task, its prerequisites, steps, expected result and useful recovery | Implementation chronology |
| Setup guide | Installation, configuration, dependencies and a first successful check | Private operator settings or credentials |
| Reference | Formats, flags, limits, result codes and compatibility contracts | Instructions for executing an old plan |
| Architecture | Responsibilities, data flow, trust boundaries and consequential design choices | A directory listing with no explanation |
| Skill/reference | Instructions an agent needs for a specific workflow | Product marketing or a running work journal |
| Qualification report | Exact candidate, host, method, results and limitations | Claims about an untested later version |
| Plan or historical record | Decisions, tasks and evidence at its named checkpoint | Unlabeled assertions of current availability |
| Changelog | Notable changes grouped by version or dated repository update | Every commit, review round or test rerun |

Give a new document one primary audience and one clear job. Extend an existing
document when it already serves that job. Split when readers need different
prerequisites or workflows, or when unrelated subjects hide important steps.
Length alone is not a reason to create another file.

The package under `plugins/tedd` must remain usable on its own. Shipped skill
references may link to sibling package files; they must not require a README or
guide that exists only in the repository. Repository guides can link into the
package for the exact agent procedure.

## Establish what is true

Before editing, check the current branch, relevant code, command help and latest
qualification record. Confirm unclear behavior with a maintainer and the supporting
source. Treat older handoffs as historical context.

Keep these claims distinct:

| Claim | Evidence needed |
| --- | --- |
| Implemented | The named source contains the behavior and relevant checks |
| Qualified | The named workflow passed on the stated runtime and host |
| Installed | The loaded artifact matches the stated source |
| Enabled | The actual configuration permits that operation in the stated scope |
| Released | A published version and its release record |

Put changing route status in [source qualification](sources/qualification.md).
Guides describe how to check the loaded build and its prerequisites, rather than
repeating dated installation assertions. Link to the exact evidence section for
a qualification claim. Keep hashes and long test inventories in its manifest.

Do not convert a design proposal into a statement of implemented behavior.
Document an intentional design change as a decision, and preserve the earlier
record's context.

## Write for someone new to the project

Lead with the task or behavior. Define a necessary term when it first appears,
then use it consistently. Prefer short paragraphs, concrete verbs and examples
that show what the reader can do.

For source memory, use these terms consistently:

- **Graph memory:** a compact claim, decision or other durable record.
- **Archived source:** preserved bytes and metadata in the private archive.
- **Reading copy:** text or structure derived from those bytes.
- **Source reference:** the exact archive version and location a memory cites.
- **Private viewer:** the separate human display for permitted evidence.
- **Obsidian projection:** the generated graph view; source previews are a
  separate, currently unqualified route.

Remove repetition, vague assurances and template leftovers. Put permission and
failure boundaries beside the actions they govern. User documentation should
explain what to do and what happens; keep worker assignments, review coordination
and resolved approval requests out of it. Agent procedures belong in skills and
their references, where Tedd's workflow guidance governs the use of optional skills.

On a copy review, look for sentences addressed to an agent preparing an answer
rather than to the document's reader. Replace process jargon with the action or
result it describes: “reviewed graph mutation” can be “review the change before
saving the memory” in a user guide. Keep precise terms in command references, and
retain repeated cautions when a reader needs them to choose or complete an action.

## Make examples usable

State the working directory, interpreter and required setup before commands.
Use one complete example for the common path; put variations beside the step
they change. Clearly label command fragments.

Use obvious placeholders such as `<registered-consumer-id>`, and explain where
each value comes from. Do not use an invented ID as if it were an authorized
session. Show the metadata contract when an example requires a metadata file.
Keep shell quoting valid, and distinguish Bash continuations from PowerShell.

Explain the expected success fields and the failures a reader can reasonably
recover from. A parseable command is not proof that the workflow is complete.
Examples involving storage or graph writes should use disposable synthetic
state during verification. Never publish private paths, task identifiers,
tokens, signed capture plans, source bodies or operator configuration.

## Use structure deliberately

Keep headings descriptive and stable. Link directly to the document or section
that answers the reader's next question. Add new documents to the index, and
update incoming links when moving or renaming content.

Use tables for routes, formats, limits and other parallel information. Use a
Mermaid diagram only when relationships or a branching flow are clearer visually.
Label each node with a meaningful concept; keep commands out of diagrams.
A diagram must agree with both the code and the surrounding prose.

Preserve useful historical reports and their artifact bindings. Put a historical
banner and a link to current guidance at the top. Distinguish similarly named
reports by their role and date. Do not rewrite an old failure as a pass or change
its measured candidate after a later repair.

Keep one current status summary that links to the evidence. Organize historical
records by milestone.
Remove a redundant document only after its unique information and inbound links
have been accounted for.

## Review and verify the change

1. Identify the affected user task and the documents that own its facts.
2. Read the changed prose against current source, defaults and qualification.
3. Read it again as a newcomer: can the reader find the path, obtain required
   values, understand the result, and act on a supported failure?
4. Check changed commands and metadata against their actual parsers/contracts.
   Exercise a new or materially changed walkthrough with disposable data.
5. Run the checks below from the repository root, then review the diff for private
   information, broken navigation and unintended scope changes.

```bash
python3 -B scripts/check_documentation.py
python3 -B scripts/check_repository_boundary.py
git diff --check
```

On Windows use a qualified Python 3.10–3.14 interpreter, such as `py -3.10 -B`,
in place of `python3 -B`. The documentation checker validates local Markdown
links, recognized CLI subcommands/options and fenced JSON syntax. CLI recognition
covers selected `python3` Bash examples for graph, source, profile and MemoryBench
scripts; it does not validate required arguments, values or successful execution.
It also does not verify external websites, private installations, natural-language
claims, MCP argument arrays or every shell language.
Report the checks actually run and their limits.

For changed skill references, check that the plugin still contains every linked
procedure and that its entry point routes readers to the necessary rules.
The repository boundary checker follows local Markdown references within the
package, including intermediate indexes. It checks documented handoff fields and
status vocabulary; it does not establish correct agent decisions from prose.
Use focused scenarios to review authority, routing, interruption and evidence
handling, and state whether these were simulated decisions or live executions.
For changes to verification tooling, run its regression tests and the applicable
quality gates. Follow [Contributing](../CONTRIBUTING.md) and the
[release process](RELEASING.md) for the broader requirements of a release.

When the change alters behavior, update the guide, reference, qualification
status and the appropriate changelog entry as applicable. Documentation-only clarification
does not create a new runtime qualification or update an installed package.
