# Membership without changing the contract

Build a set/frozenset once for repeated membership when conversion cost is
justified and values are hashable. Keep the original sequence when ordering,
duplicates, tiny one-off lookups or unhashable values matter.

`list(set(values))` changes ordering and multiplicity. Set iteration must not
enter Tedd's canonical output; define serialization order explicitly. Hash lookup
is average-case O(1), not a worst-case guarantee. Set construction still costs
time and memory. Use order-preserving deduplication only when deduplication is
the actual contract.

Verify duplicate/order semantics, one-shot iterables and bounded large inputs;
measure a performance claim at the relevant scale. See
[performance](performance.md) and [materialization](coding-performance-materialization.md).
