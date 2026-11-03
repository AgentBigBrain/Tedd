# Smallest sufficient design

Choose the simplest complete solution that satisfies current behavior, privacy,
durability, compatibility and tested growth requirements. Similar-looking code
alone does not justify shared flags, a pipeline or an extension point. A real
second consumer or known migration requirement is evidence; no fixed occurrence
count proves that an abstraction is useful.

Centralize safety-critical knowledge such as permission and atomic-publication
policy when divergence would break an invariant. Keep versioned behaviors separate
when their semantics differ. Simplicity includes callers, tests, failure handling
and maintenance, not only production line count. A serializer still needs an
accepted domain shape and a format-aware encoder; a loose object mapping is not
proof of JSON compatibility.

Verify that each abstraction meets a present requirement, tests remain readable,
and removing speculative options does not break a documented contract. See
[responsibility seams](coding-design-responsibility-seams.md),
[extension points](coding-design-extension-points.md) and [design](design.md).
