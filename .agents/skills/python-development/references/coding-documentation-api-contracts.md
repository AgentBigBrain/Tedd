# Document Public Behavioral Contracts

## Invariant

Documentation for a public module, class, function, command, or exception must
explain behavior that its signature cannot: invariants, side effects, durability,
privacy boundaries, return meaning, and expected failures.

## Applies when

- A symbol is consumed outside its module or is a stable test/extension seam.
- A function mutates storage, publishes a file, changes permissions, or emits CLI
  output.
- Parameters carry units, bounds, ownership, or trust assumptions.

## Does not apply when

- A private helper's name and precise type signature make its local behavior obvious.
- A docstring would merely restate syntax or parameter types.
- Generated or repeated prose would become more likely to drift than clarify.

## Bad example

The docstring omits no-clobber behavior, durability, path policy, return meaning,
and expected failures.

```python
def publish_backup(source: Path, destination: Path, overwrite: bool) -> Path:
    """Publish a backup."""
    ...
```

## Good example

```python
from pathlib import Path


def publish_backup(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Publish a verified backup at a private local destination.

    The source remains unchanged. When `overwrite` is false, publication is
    atomic and fails if any destination entry already exists.

    Args:
        source: Existing private backup staged on the destination filesystem.
        destination: Final file path; its parent must already be trusted.
        overwrite: Whether an existing regular destination may be replaced.

    Returns:
        The resolved final destination.

    Raises:
        FileExistsError: If no-clobber publication loses a race.
        OSError: If publication or required durability synchronization fails.
        PathSafetyError: If a path violates the local-filesystem policy.
    """
    ...
```

## Caveats and compatibility

- Google-style sections are readable, but Tedd should enforce one repository style
  only if that policy is explicit. Behavioral completeness matters more than labels.
- Do not expose secret values, private paths, tokens, or internal tracebacks in
  examples, exception text, or generated documentation.
- Keep compatibility and CLI examples executable against the supported Python floor.
- Update documentation in the same change as behavior; stale guarantees are defects.
- Docstrings are available on all supported versions and add no dependency.

## Verification

- [ ] Public storage and CLI APIs state their side effects and failure modes.
- [ ] Units, bounds, overwrite semantics, and ownership assumptions are explicit.
- [ ] Types are not redundantly paraphrased.
- [ ] Documentation examples use Python 3.10 syntax and current command names.
- [ ] Repository documentation checks pass.

## Related guidance

- [Public typing contracts](coding-typing-public-contracts.md)
- [Design and maintainability](design.md)
- [Security](security.md)
