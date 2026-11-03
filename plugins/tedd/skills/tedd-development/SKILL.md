---
name: tedd-development
description: Legacy explicit-invocation compatibility alias for the Tedd skill. Use only when a request or saved prompt still names $tedd-development; route the complete task through the canonical $tedd skill without duplicating its identity, workflow, or knowledge rules.
---

# Tedd Development compatibility alias

This is a compatibility alias, not a second agent. Tedd's canonical skill is [../tedd/SKILL.md](../tedd/SKILL.md).

When this alias is explicitly invoked:

1. Read `../tedd/SKILL.md` completely and follow it as the controlling workflow.
2. Use Tedd as the public agent identity.
3. Load only the canonical Tedd references relevant to the task.
4. Do not create separate profile, graph, producer, or projection state for this alias.

Migrate saved prompts to `$tedd`. Historical `skill:tedd-development` graph URIs remain valid evidence references and must not be rewritten merely because the skill was renamed.
