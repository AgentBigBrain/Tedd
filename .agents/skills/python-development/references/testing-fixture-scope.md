# Keep mutable fixtures independent

Create synthetic mutable state per test. Share expensive setup only when it is
effectively immutable and each test obtains a clean handle/view or rolled-back
transaction. Sharing an explicit state-transition scenario is a different contract.
Never point fixtures at Tedd's configured graph or profile.

Register each cleanup immediately after acquisition, before later setup can fail.
Environment and working-directory changes remain process-global; registered
cleanup does not make them safe under concurrent execution. Build host paths from
native temporary directories and verify fixture paths stay within that directory.

Run affected tests alone, together and in a different order. Inject a failure
after acquisition and verify cleanup and independent mutable state. See
[testing](testing.md) and [shared support](testing-fixture-shared-support.md).
