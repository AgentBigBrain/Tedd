# Use Reviewed Dependency Management

Category policy: [Tooling](tooling.md).

## Tedd rule and invariant

Tedd currently uses an isolated virtual environment, `pip==26.1.2`, and the
checked-in PEP 735 `dev` group. Do not install or invoke uv, `uvx`, a remote
installer, or an editable project workflow unless Tedd explicitly adopts an exact
reviewed uv version and documents its lock, group, and sync semantics.

## Applies when

- Someone proposes uv for environment, lock, tool, or CI management.
- Reviewing commands that mix extras, dependency groups, `uv pip`, and uv project mode.
- Reproducing Tedd's approved development environment.

## Does not apply when

- Speed claims are the only adoption evidence.
- The checkout, installer, package version, or resolved artifacts are unreviewed.
- A one-off `uvx` invocation is presented as harmless because it is temporary.

## Bad example

```text
# Non-executable anti-patterns:
<remote-installer> | <shell>
<ephemeral-runner> <unpinned-package> <repository>
<environment-sync> <unreviewed-lock-or-config>
```

These commands obtain executable code and may resolve, install, auto-sync, or mutate
the environment without Tedd's reviewed pin and policy.

## Corrected Tedd example

```powershell
# Only after checkout inspection, explicit install authority, and venv isolation:
python -m pip install --disable-pip-version-check pip==26.1.2
python -m pip install --disable-pip-version-check --group dev
python scripts/verify_release.py --quality
```

The group itself contains exact reviewed pins. Prefer the repository wrapper so
local and CI checks use the same scope.

## Caveats and edge cases

- `uv sync` includes the default development group unless options exclude it; it is
  not inherently a runtime-only install.
- Package extras, PEP 735 groups, `uv pip` commands, and uv's project workflow have
  different selection and lock semantics.
- `uv run` can synchronize before execution. `--locked` checks freshness, while
  `--frozen` intentionally trusts an existing lock; neither distinction matters
  until uv is formally adopted.
- Never default to `curl | sh`, `irm | iex`, global tool installation, or an
  unpinned `uvx` package.

## Verification

- Confirm the approved environment reports pip `26.1.2` and every group package
  matches `pyproject.toml`.
- Ensure no `uv.lock`, uv command, or installer appears accidentally in the diff.
- If adoption is proposed, test exact semantics on all supported platforms in an
  isolated branch before changing the documented workflow.
