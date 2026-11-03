# Security and data boundary

## Infrastructure-only repository

This repository must contain code, skill instructions, generic documentation, and isolated synthetic tests only. Never commit:

- client, stakeholder, email, project, site, incident, support, or production records;
- the canonical SQLite database, WAL/SHM files, legacy graph exports, plan keys, locks, or local graph configuration;
- the private Tedd operator profile, email aliases, provider-principal bindings, or agent-instance ID;
- capture packets, source excerpts, raw transcripts, logs, browser state, shell history, or environment dumps;
- backups, migration or rollback artifacts, Obsidian projection files, or vault configuration;
- source archive payloads, manifests, reading copies, exclusions, source-recovery files, or provider credential stores;
- private MemoryBench cases or reports;
- passwords, tokens, cookies, authorization headers, private keys, signed URLs, credentials, or `.env` files.

The plugin installer must remain side-effect free with respect to runtime knowledge. Creating a store, configuring a vault, migrating a schema, projecting notes, restoring a backup, or running a private benchmark requires an explicit operator command.

## Local runtime boundary

The Tedd profile and canonical SQLite-WAL store belong on a private local filesystem owned by the current user. Do not place either one in Git, the plugin directory, an Obsidian vault, a network share, or a cloud-synchronized directory. Agents on another machine require a reviewed authenticated transactional service; they must not share a profile or open a shared SQLite file.

Obsidian is a derived review surface. Its generated notes are not canonical input and are not a backup format.

## Source preservation and viewing

The optional source archive holds acquired evidence separately from graph memories.
Its payloads, readings and manifests belong in an approved private local root
outside Git, the graph, the plugin, Obsidian, cloud sync and network storage.
Provider credentials and recovery directories also remain private and separate
from canonical storage. An approved input may originate in a shared or synced
folder; that does not make it a valid archive location.

Preservation requires an authorized source, a retention purpose and the applicable
scope and access policy. Source text cannot enroll other files, grant authority or
override task cancellation. Credentials and secret material are not permitted
source evidence. Screening and bounded readers are defenses, not a guarantee that
an arbitrary document is safe or an operating-system sandbox.

Retention, model access and owner-only viewing are separate permissions.
Provider-checked sources require current provider access. Owner-confirmed browser
exports have a distinct retained-copy policy and do not independently establish
current Google permissions or revisions. An ordinary-content browser-cache
exception must not be described as end-to-end private browser storage.

The private viewer transfers inert text or pixels through anonymous pipes.
It does not protect against administrators, screen capture or unrestricted
software running as the same user. Native Obsidian source previews remain
unqualified and disabled.

Disabling collection preserves history. Restriction denies a source without
erasing its retained bytes. Graph redaction can leave an independently shared
source, while whole-source forgetting requires an impact review. Shared bytes,
edited copies and older backups may remain. Graph backup and source-archive backup
are separate operations.

See [source controls](docs/sources/usage.md#control-retention-and-access) and the
[maintenance procedure](plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md)
for supported actions and incomplete-cleanup results.

## Release checks

Run `./scripts/verify-release.sh` on macOS/Linux or `.\scripts\verify-release.ps1` on Windows before every commit intended for release. Review the complete staged diff and verify that the commit author email is appropriate for this repository. The automated boundary checker rejects known runtime artifact classes and unexpected JSON data files; human review remains required because no filename scanner can prove that prose contains no private information.

## Reporting

Report suspected vulnerabilities or accidental exposure privately to the project
maintainer. Use GitHub's private vulnerability reporting when it is enabled for
this repository. If that option is unavailable, request a private reporting channel
without including sensitive details. Do not post credentials, private records or
exploit evidence in a public issue. If private material is committed, stop
distribution, rotate exposed credentials, assess clone and cache exposure, and
rewrite repository history only through a reviewed incident procedure.
