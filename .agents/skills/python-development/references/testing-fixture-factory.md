# Factories for varied synthetic data

Use a small factory only when valid defaults have real shared consumers and
explicit variations make tests clearer. Return fresh mutable members each time;
never draw fixtures from Tedd's live graph, profile, keys or projection.

Use deterministic non-secret defaults, keyword arguments and an explicit ID when
cross-process identity matters. Distinguish `None` from an intentionally empty
value; truthy fallback expressions can erase the case under test. Reset generated
sequences per test so IDs do not depend on execution order. Keep malformed values
available for rejection tests instead of reproducing production validation inside
the factory. Use an existing typed record contract when a loose mapping hides it.

Register cleanup as soon as resources are acquired. Verify fresh state, default,
empty, malformed and boundary cases, including cleanup after assertions fail.
A literal may be clearer; no factory library or new dependency follows from this
advice. See [shared support](testing-fixture-shared-support.md) and
[fixture lifetime](testing-fixture-scope.md).
