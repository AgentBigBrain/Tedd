# Graph storage compatibility

New graphs use local SQLite (v4). Older v3 graphs use JSON. Both formats
support `doctor`, `validate`, `get`, `recall`, `capture`, `index-status`, and
`reindex`. Some familiar commands have different options or output on v3.
The legacy CLI also has its own paths for operations such as projection and
JSON schema migration. Shared-task inboxes and per-record revision history
require v4. Use the explicit storage upgrade below to move a v3 graph to v4.

## If the graph file is missing

Run `doctor` to see the configured path. It reports `store_state: missing`
when the file is absent. If you expect an existing graph, check the path in
the configuration and restore the file or let it finish syncing. If this is
your first installation, run `init-v4` to create an empty v4 graph.
Commands that need a graph return `store_missing` with the store and
configuration paths. A refused capture has saved nothing. Retry it after
the graph is available.

## Existing v4 graphs

An ordinary plugin update leaves your graph, private profile, and Obsidian
configuration in place. Use `doctor` to check the configured graph and `validate`
to check its records. Run these commands from the installed
`obsidian-knowledge-graph` skill directory with your supported Python interpreter:

```bash
python3 -B scripts/knowledge_graph.py doctor
python3 -B scripts/knowledge_graph.py validate
```

On Windows, keep `-B` and replace `python3` with `py -3` or the qualified Python path from your setup.

## Existing v3 graphs

The [v3 upgrade procedure](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#upgrade-a-v3-graph)
builds v4 beside the original and switches the configuration after you approve
and apply the preview. Current records keep their IDs and values. V4 retains
the ordered v3 journal headers and creates a baseline revision for each imported
record. `history --id` shows that baseline and subsequent v4 revisions. For
evidence from before the upgrade, consult the exact v3 JSON backup. The importer
cannot reconstruct earlier per-record values from journal headers.

If the upgrade stops after the configuration already points to v4, keep the
database, backup and manifest in place. Follow [Resume an interrupted upgrade](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#resume-an-interrupted-upgrade)
to finish with the original reviewed command.

## Backups and source archives

The Obsidian vault is a readable view of the graph. Keep a [verified graph backup](../plugins/tedd/skills/obsidian-knowledge-graph/references/setup-and-recovery.md#back-up-a-v4-graph)
and back up the optional source archive separately. Before
rolling back Tedd, check [source-archive compatibility](sources/setup.md#choose-the-build-and-preserve-a-rollback-path)
so older software cannot restore access to restricted or forgotten evidence.

Compatibility changes are announced in the [changelog](../CHANGELOG.md).
