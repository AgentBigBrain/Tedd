# Clear collection transformations

Use a comprehension for a simple transformation/filter without side effects.
Use a loop when validation, diagnostics, recovery or intermediate names explain
behavior; do not compress those responsibilities into nested clauses.

- A comprehension materializes its result. Prefer an iterator only when a
  single-pass consumer and resource lifetime permit it.
- Comprehensions are not simply executed in C; benchmark the actual workload
  before claiming a speed improvement.
- Verify ordering, filtering, failure behavior and bounded memory. A side-effect
  comprehension adds an unused allocation and obscures the action.

See [materialization](coding-performance-materialization.md) and
[boundary validation](coding-validation-boundary-data.md) for those risks.
