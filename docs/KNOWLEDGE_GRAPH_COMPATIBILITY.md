# Graph storage compatibility

Tedd uses a local SQLite database for its current graph format, schema v4.
It also retains compatibility with older v3 JSON graphs and an explicit upgrade
command. New graphs should use v4.

## Existing v4 graphs

An ordinary plugin update leaves your graph, private profile, and Obsidian
configuration in place. Use `doctor` to check the configured graph and `validate`
to check its records. Run these commands from the installed
`obsidian-knowledge-graph` skill directory with your supported Python interpreter:

```bash
python3 scripts/knowledge_graph.py doctor
python3 scripts/knowledge_graph.py validate
```

On Windows, use `py -3` or the qualified absolute Python path from your setup.

## Existing v3 graphs

Keep your original graph and a verified backup before an upgrade. The
`upgrade-v4` workflow previews the change, binds it to the source graph's digest,
builds v4 beside the original, and switches configuration only after an explicit
apply. It preserves record identities, history, and mutation order.

Follow [setup and recovery](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#validation-and-recovery)
for the complete procedure. Installation does not run this upgrade.

If a switch is interrupted, use the saved recovery manifest and the documented
recovery command. Preserve an ambiguous state for review rather than replacing
either graph or editing the manifest.

## Backups and source archives

An Obsidian vault is a readable view, not a graph backup. Graph backups and the
optional source archive have separate backup and recovery procedures. Before
rolling back Tedd, check [source-archive compatibility](sources/setup.md#choose-the-build-and-preserve-a-rollback-path)
so older software cannot restore access to restricted or forgotten evidence.

Compatibility changes are announced in the [changelog](../CHANGELOG.md).
