# Properties need meaningful attribute behavior

Prefer a plain attribute. A property is useful for cheap deterministic derived
state or invariant-preserving assignment; construction must enforce the same
invariant as later assignment. Give I/O, mutation and expensive work explicit
method names so latency and failure are visible.

A missing setter prevents ordinary assignment through that name, not mutation
of the underlying object. Changing an attribute to a property may affect
`__dict__`, dataclass fields, slots, reflection and subclass assignment.
`cached_property` needs a writable `__dict__` and may compute more than once
under concurrency; synchronization differs across supported Python versions.
Do not cache secrets or values without a clear invalidation lifetime.

Verify construction/assignment boundaries, side effects, invalidation and those
compatibility surfaces when touched. See [dataclasses](coding-data-dataclasses.md)
and [design](design.md).
