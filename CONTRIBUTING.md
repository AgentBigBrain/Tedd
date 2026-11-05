# Contributing

## Before editing

1. Work in a task branch.
2. Confirm the change belongs to Tedd infrastructure rather than runtime knowledge.
3. Update the Tedd and graph skills together when a shared workflow or graph contract changes. Keep the [`tedd-development` compatibility alias](plugins/tedd/skills/tedd-development/SKILL.md) thin and limited to explicit invocation.
4. Do not copy production graph data into tests or documentation. Build deterministic synthetic fixtures in temporary directories.

Use the [repository-local Python skill](.agents/skills/python-development/SKILL.md)
for Python changes, tests and tooling. It links focused guidance for the work at
hand and is not shipped in the plugin.

Superpowers working notes belong in ignored local directories, not in the
proposed merge: `/docs/superpowers/`, `/.superpowers/`, and `/superpowers/`.
The repository check also rejects files force-added from those directories.
Keep durable contributor instructions in the regular documentation, with no links
that depend on local notes being present.

## Mandatory cross-platform requirements

Changes to code, tests, packaging, and helpers must preserve Tedd's supported
Windows, macOS, and Linux behavior and security guarantees. Follow the
[Python development skill](.agents/skills/python-development/SKILL.md) and its
applicable correctness, testing, and security guidance. Prefer focused changes
and existing platform boundaries, not a new framework or broad refactor.

Run affected shared-contract tests across the supported platforms and use the
verification gates below. Do not weaken security checks or blanket-skip shared
tests to obtain a pass. Before merging or releasing, required cross-platform CI
checks must pass for the exact candidate commit. Report unavailable platforms
and skipped native integrations as unverified, not passed. These requirements
do not grant permission to publish, merge, release, or install a plugin.

## Documentation changes

Follow [Maintaining Tedd documentation](docs/DOCUMENTATION.md). Update the
document that owns the affected behavior and its entry in the
[documentation index](docs/README.md). Keep current usage separate from dated
qualification records. Verify the technical claims, then read the procedure as a
newcomer and check its commands and links.

## Verification

Run the platform wrapper from the repository root.

On macOS or Linux:

```bash
./scripts/verify-release.sh
```

On Windows PowerShell:

```powershell
.\scripts\verify-release.ps1
```

For direct cross-platform execution, run `python scripts/verify_release.py` (or `python3 scripts/verify_release.py` when that is the installed Python 3.10+ command). The Bash wrapper keeps shell syntax validation mandatory; the direct Python and PowerShell paths run it whenever Bash is available.

Repository-only tests live under `tests/`; they are not shipped inside
`plugins/tedd`. To run the same complete discovery used by the release verifier,
use `python -B -m unittest discover -s tests -t . -p "test_*.py"`. Focused suites
use their package name, for example
`python -B -m unittest tests.tooling.test_tedd_mcp_packaging`.

Before opening a pull request that changes Python or developer tooling, also run the repository-local quality gates:

```bash
python -m pip install pip==26.1.2
python -m pip install --group dev
python scripts/verify_release.py --quality
```

The PEP 735 group contains development tools only. Tedd's normal verifier and runtime remain dependency-free beyond the Python standard library.

CI runs on pull requests and pushes to `main`, `fix/**`, and `tedd-*-beta.*`.
Check the run for the exact commit being proposed. A green run on an earlier
commit does not qualify later changes, and CI does not prove desktop discovery
or update an installed plugin.

Automatic CI checks the supported Python 3.10 through 3.14 range. For Python 3.15
compatibility work, start the [manual preview workflow](.github/workflows/python-preview.yml)
on a development branch:

```bash
gh workflow run python-preview.yml --ref <development-branch>
```

The preview workflow skips `main` and tags.

Routine macOS CI uses the standard Intel macOS 26 runner. Apple Silicon host and
source-helper qualification remains a separate native check.

The enforced scope is explicit in `pyproject.toml`: Ruff formatting, Ruff linting, and mypy cover every production Python module, while branch coverage measures the complete isolated production suite. Mypy uses the deterministic `win32` target on every host so the Windows security primitives receive the same strict typeshed treatment in local and CI runs; the release matrix separately executes the runtime on Windows, macOS, and Linux. The release verifier independently discovers production modules, rejects formatter or mypy exclusions, and rejects explicit `Any` in production. Add new production modules to the checked scope in the same change; do not weaken the gate with broad suppressions or exclusions.

Run the private production MemoryBench suite separately when the change affects recall, ranking, scoping, sanitization, subscriptions, context packs, or other read boundaries. See the [graph evaluation requirements](plugins/tedd/skills/obsidian-knowledge-graph/SKILL.md#validation-and-recovery) for its role. Never add that suite or its report to this repository.

Graph architecture, identity, authority, migration, privacy, and projection changes also require full graph validation and a read-only [Graph Steward review](plugins/tedd/skills/obsidian-knowledge-graph/SKILL.md#conditional-graph-steward-review) against an isolated or explicitly authorized store.

## Changelog and release discipline

- Use `Unreleased` for notable changes that have not yet been published. Once a repository-only documentation, skill or tooling update is published on `main`, put it under a dated repository-update heading. Do not bump the plugin version when its package bytes are unchanged.
- For a reviewed package release, move the applicable notes to a dated version and update the plugin version before final verification. An `rc` suffix already identifies a prerelease; describe source publication and installation separately. Ordinary pull requests do not create tags.
- State canonical-store and projection compatibility in release notes.
- Review the staged file list and diff for private data and generated artifacts.
- Verify the repository-local Git email before committing.
- Tag releases as `vMAJOR.MINOR.PATCH` only after CI passes and the reviewed branch is merged.

Installation and upgrades must never initialize, migrate, restore, project, or otherwise mutate a user's live brain automatically.
