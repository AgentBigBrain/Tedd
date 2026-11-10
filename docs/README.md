# Tedd documentation

Start with the [project README](../README.md) for installation and an overview of
Tedd, including a [visual tour](../README.md#see-tedd-in-use). The guides below
help you set up and use Tedd; the later sections collect
development guidance and historical evidence.

## Use and understand Tedd

| Document | Purpose |
| --- | --- |
| [Keep project memory current](MEMORY_WORKFLOW.md) | Saving choices, unfinished-work review, scoped preferences, memory handoffs, receipts, finding older memories, and recovery |
| [Follow saved connections in Obsidian](OBSIDIAN_CONNECTIONS.md) | Read people, projects, memories, decisions, commitments, and evidence in the Obsidian view |
| [Source-backed memory](sources/README.md) | How preserved evidence relates to compact memories |
| [Using saved sources](sources/usage.md) | Preserve and check documents or images, control access, and disconnect Google |
| [Source setup](sources/setup.md) | Connect Codex to the source archive and configure document readers |
| [Local source walkthrough](sources/local-example.md) | Try metadata, preservation, reviewed memory binding, and evidence checking on disposable state |
| [Source reference](sources/reference.md) | Formats, identities, limits, commands, and result meanings |
| [Source qualification](sources/qualification.md) | Current versions, source and installation status, verified routes, and platform limits |
| [Architecture](ARCHITECTURE.md) | How agent instructions, identity, memory, source storage, and generated views fit together |
| [Identity and private profile](IDENTITY.md) | Operator setup, authorship, and account matching |
| [Windows setup](WINDOWS_SETUP.md) | Installed-file verification, interpreter binding, and Windows handoff-hook requirements |
| [Claude Code setup](CLAUDE_CODE_SETUP.md) | Installation, first-time setup, project connections, and a known-memory check |
| [Codex and Claude qualification](CLAUDE_CODE_QUALIFICATION.md) | Host checks for shared memory, preferences, overdue review, and saving |
| [Testing on macOS](MACOS_TESTING.md) | Isolated installation checks, source tests, and optional reader prerequisites |
| [Knowledge-graph scale](SCALE.md) | Measured graph bounds and growth guidance |
| [Memory research](MEMORY_RESEARCH.md) | Retrieval and agent-task measurements, public evaluation tools, and archived study limits |
| [Security policy](../SECURITY.md) | Data boundaries and private vulnerability reporting |

## Develop and maintain Tedd

| Document | Purpose |
| --- | --- |
| [Contributing](../CONTRIBUTING.md) | Engineering workflow, mandatory cross-platform requirements, isolated tests, and quality gates |
| [Maintaining documentation](DOCUMENTATION.md) | Document ownership, writing standards, and verification |
| [Release process](RELEASING.md) | Versioning, source verification, and installation qualification |
| [Changelog](../CHANGELOG.md) | Notable changes by version |
| [Python development skill](../.agents/skills/python-development/SKILL.md) | Repository-only Python guidance: typed transport boundaries, failure categories, bounded parsing and focused rule cards |
| [Graph storage compatibility](KNOWLEDGE_GRAPH_COMPATIBILITY.md) | Current v3/v4 support, explicit upgrades, and rollback boundaries |
| [Memory evaluation protocol](../research/memory-eval-lab/PROTOCOL-V2.md) | Current public retrieval-evaluation method |
| [Project recall protocol](../research/project-recall-lab/PROTOCOL.md) | Synthetic evaluation of reads from projects larger than one context, with its [recorded baseline](../research/project-recall-lab/BASELINE.md) |

The [Tedd skill](../plugins/tedd/skills/tedd/SKILL.md) is the packaged workflow
entry point. It links identity, development, provider, browser, and communication
procedures. The [graph skill](../plugins/tedd/skills/obsidian-knowledge-graph/SKILL.md)
owns graph initialization, scope, capture, recall, projection, and recovery.
It links separate procedures for setup and recovery, recall, capture and
correction, and projection, keeping each operation's prerequisites with its steps.
Its [capture schema](../plugins/tedd/skills/obsidian-knowledge-graph/references/capture-schema.md)
defines the JSON packets used to save graph records. The
[source procedure](../plugins/tedd/skills/tedd/references/source-backed-memory.md)
routes agents to source acquisition, host setup, viewing, and maintenance.
The [orchestration skill](../plugins/tedd/skills/orchestrate-work/SKILL.md)
owns bounded handoffs and coordination. These package references instruct agents; the guides above explain how you can
use and configure Tedd.

## Historical records

The [routing baseline](evaluations/2026-09-03-tedd-routing-baseline.md) records a
synthetic orchestration check. The
[original memory-evaluation protocol](../research/memory-eval-lab/PROTOCOL.md)
remains available for interpreting older studies. Historical findings keep their
original candidates and limitations; they are not proof about a later build.

The dated qualification records, design plans, and evaluation evidence behind
the summaries in these guides are retained in the maintainers' private archive.
They are not part of the public repository.
