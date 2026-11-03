# Provider and framework routing

Use the `$tedd` skill as the workflow coordinator. Use focused installed skills and current primary documentation for provider-specific commands and semantics.

This table is vendor-neutral. Keep operator-specific hosting, deployment, and
release procedures in a private local skill and use it alongside Tedd when the
task calls for it. Keep private paths, account details, and local skill names out
of the installed package.

## Routing table

| Scope | Preferred route |
|---|---|
| Hosting platforms, content-management systems, and plugin or theme releases | Use the installed skill or the repository's documented release procedure for that platform. Confirm the production branch, version metadata, deployed revision, and rollback point from repository and host evidence before promotion. |
| DNS, CDN, WAF, caching, bot rules, edge functions, or edge deployment | Use an installed provider skill or connector when one matches; otherwise use current primary provider documentation and existing automation. Reverify live edge and origin behavior separately. |
| Deployment platforms and CI/CD | Use the narrow installed skill matching deployment, observability, environment, firewall, cache, or routing work. Prefer the repository's established pipeline. |
| Application frameworks | Use the installed framework skill and any narrower cache, routing, upgrade, or verification skill that matches. |
| Cloud providers | Identify the exact account context, region, service, resource, IaC or CLI workflow, and rollback path. Use current primary documentation and existing repository automation. Do not generalize permissions or behavior across services. |
| Task tracking and project tools | Recall the authorized workspace, view, and task identities from the graph, then inspect current authenticated state read-only by default. Store volatile status, assignee, priority, and dates as timestamped observations; store stable URLs and identities as claims. Do not create, edit, comment, assign, or move work without task-specific authority. |
| Cross-provider incidents | Map DNS, edge, hosting, application, data, monitoring, and ownership boundaries before changing state. |

## Provider rules

1. Prefer the repository's established deployment mechanism over a newly invented path.
2. Use provider APIs, CLIs, or connectors when they safely complete the task and browser interaction was not explicitly requested.
3. Verify the exact account, organization, project, environment, region, and resource before mutation.
4. Treat dashboards and success banners as signals, not completion proof.
5. Verify the failure case, an unaffected control, and the actual live boundary changed.
6. Keep provider statements separate from independently verified facts.
7. Record provider limitations and pending ownership as loops instead of implying resolution.
