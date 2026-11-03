# Give Shared State an Explicit Owner

## Contents

- [Invariant](#invariant)
- [Applicability](#applies-when)
- [Exclusions](#does-not-apply-when)
- [Examples](#bad-example)
- [Caveats](#caveats-and-compatibility)
- [Verification](#verification)
- [Related guidance](#related-guidance)

## Invariant

Mutable services, configuration, stores, and resource handles must have an explicit
owner and lifecycle. Construct them at the process boundary and pass them to the
code that uses them instead of exposing lazy module-level singletons.

## Applies when

- A database connection, profile, key provider, or cache is shared by commands.
- Tests currently reset or monkeypatch module globals.
- Different stores, profiles, or platform implementations may coexist.

## Does not apply when

- The value is an immutable constant.
- A module-level logger names a logging category but owns no application state.
- Framework startup wiring is the documented composition root and handlers still
  receive explicit dependencies.

## Bad example

Domain code silently chooses configuration and shares one immortal store across all
callers and tests.

```python
import os


_store: GraphStore | None = None


def get_store() -> GraphStore:
    global _store
    if _store is None:
        _store = GraphStore(os.environ["TEDD_STORE"])
    return _store


def recall(query: str) -> list[str]:
    return get_store().recall(query)
```

## Good example

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppContext:
    store: GraphStore

    def close(self) -> None:
        self.store.close()


def build_context(store_path: Path) -> AppContext:
    return AppContext(store=GraphStore(store_path))


def recall(context: AppContext, query: str) -> list[str]:
    return context.store.recall(query)


def main(store_path: Path, query: str) -> int:
    context = build_context(store_path)
    try:
        for item in recall(context, query):
            print(item)
    finally:
        context.close()
    return 0
```

## Caveats and compatibility

- Sharing is not itself wrong. Hidden ownership, implicit configuration, immortal
  resources, and untestable teardown are the problems.
- Read-only settings may be loaded once, but pass the resulting value inward rather
  than having domain code repeatedly reach into the environment.
- A task supervisor may own a set of live tasks; it should be an explicit lifecycle
  object, not a contradictory "no globals ever" exception.
- Consider process forks, threads, and multiple event loops before caching handles.
- The pattern uses only Python 3.10 standard-library features.

## Verification

- [ ] Construction and teardown happen at one named boundary.
- [ ] Two contexts can be created in one process without cross-talk.
- [ ] Tests inject a private temporary store without resetting module state.
- [ ] Environment variables are read at startup, not deep inside domain functions.
- [ ] Resource cleanup is verified on success and failure.

## Related guidance

- [Dependency injection](coding-design-dependency-injection.md)
- [Own every async task](coding-async-task-ownership.md)
- [Security](security.md)
