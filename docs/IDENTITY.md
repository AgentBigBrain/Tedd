# Tedd identity and private operator setup

Tedd has a stable public agent identity and a private, installation-local operator registry. The repository ships the schema and deterministic tooling, but never ships or initializes a person.

## Identity layers

| Layer | Purpose | Authority | Storage |
| --- | --- | --- | --- |
| Publisher | Package ownership, legal notice, security contact | None over runtime work | Git metadata |
| Agent persona | Public name `Tedd` and truthful disclosure policy | None by itself | Versioned skill |
| Agent instance | Opaque producer identity for one installation | Provenance only | Private local profile |
| Operator | Confirmed person associated with the current task | Only the task authority actually granted | Private profile; evidence-backed graph records when appropriate |
| Message author | Principal responsible for one provider message | Determined per message | Ephemeral provider/session state |
| Stakeholder | Recipient or other participant | External evidence, not automatic operator authority | Provider context and bounded graph evidence |

The manifest author and copyright owner are publisher facts. Tedd must never use them to select an operator.

## Private profile

The default profile is `~/.codex/tedd/profile.json`. Its parent directory and the file are private to the OS user: POSIX uses modes `0700` and `0600`, while Windows uses a protected user-scoped ACL. Writes are lock-bounded and atomic.

All production Tedd tasks for one local installation use that same default profile. The `--profile` override exists for isolated tests and operator-reviewed recovery only; do not create per-agent, per-task, per-client, or per-channel production profiles. Task and run IDs provide work separation, while operator records provide multi-user separation.

For an isolated test or reviewed recovery override, every existing path component must be a real directory rather than a symlink or Windows reparse point, and an existing profile directory must already be private (`0700` on POSIX or a protected user-scoped ACL on Windows). On macOS, use a resolved path such as `/private/tmp/...` instead of the compatibility symlink `/tmp/...`. These checks prevent private identity state from being redirected into synchronized or unintended storage.

The profile schema includes:

- schema, profile, installation, and agent-instance identifiers;
- the fixed public agent class and display name `Tedd`;
- one or more opaque operator IDs;
- operator preferred/display names;
- confirmed email aliases and optional provider-principal bindings;
- creation and update timestamps.

Names, emails, and provider bindings support exact matching. They do not confer approval and are never used as producer, task, or run IDs. Raw profile contents stay outside Git, Obsidian, captures, diagnostics, and ordinary review-safe output.

## First run

Installation is side-effect free. In a new task, `$tedd` knows its own name before a profile or graph exists.

1. Tedd checks profile status.
2. If unconfigured, Tedd says: `I'm Tedd. What should I call you, and which email should I use to recognize your accounts?`
3. The person may defer email and continue without cross-channel identity binding; the profile remains unconfigured until an email is explicitly confirmed.
4. Tedd initializes only after both values are explicitly confirmed and then validates the result.
5. Tedd never infers the operator from package metadata, Git configuration, an OS username, a browser session, or unverified message prose.

Run the helper from the installed `skills/tedd` directory, or from `plugins/tedd/skills/tedd` in a repository checkout:

```bash
python3 scripts/tedd_profile.py status
python3 scripts/tedd_profile.py init \
  --name "<preferred name>" --email "<confirmed email>"
python3 scripts/tedd_profile.py validate
python3 scripts/tedd_profile.py show
```

The helper returns machine-readable JSON. `show` redacts email strings and provider-principal values unless the operator explicitly requests full local output; provider names remain visible.

Add a second operator without creating a global active-user setting, and resolve only an exact confirmed identity:

```bash
python3 scripts/tedd_profile.py add-operator \
  --name "<preferred name>" --email "<confirmed email>" \
  --provider-principal "<provider>=<authenticated principal>"
python3 scripts/tedd_profile.py bind-identity \
  --operator-id "<operator ID>" \
  --email "<additional confirmed email>" \
  --provider-principal "<provider>=<authenticated principal>"
python3 scripts/tedd_profile.py resolve \
  --provider "<provider>" --principal "<authenticated principal>"
```

The corresponding `resolve --email "<confirmed email>"` form preserves the local part exactly and normalizes only the IDNA/domain portion. Provider names are case-normalized, while provider-principal strings match exactly. A successful match still returns `authority_asserted: false`; identity resolution is never approval.

Profile schema v1 is deliberately additive. `bind-identity` can add a verified binding to its existing operator, but it cannot delete, transfer, or silently correct one. Preserve an old address as an alias only while that operator still controls it. If a binding is wrong, compromised, or reassigned, stop identity-dependent work and use an operator-reviewed profile recreation; retain the old agent-instance ID long enough to inspect or explicitly transition any graph consumers, and never auto-reassign the disputed binding.

## Multiple operators

The profile may contain several operators, but Tedd never selects the first record or whoever spoke most recently as a machine-wide `current_user`.

- Bind the active operator per task or authenticated session.
- Prefer an exact provider-principal match, then an exact confirmed email alias.
- If resolution is missing or ambiguous, ask one bounded question.
- A single-user default may personalize low-risk conversation; it is not approval evidence.
- Unknown participants remain unknown and cannot grant operator authority.

Several local Tedd tasks may share one agent-instance ID while task and run IDs distinguish them. Different installations use different opaque agent-instance IDs even though each publicly identifies as Tedd.

## Shared-chat authorship

Classify messages as `agent`, `confirmed_operator`, `external_participant`, or `unknown` using provider sender metadata and successful Tedd outbound receipts. Never infer that every message Tedd did not send belongs to the operator.

Use `Tedd here.` once after confirmed human interleaving on a shared sending identity or when authorship is genuinely ambiguous. Do not repeat it across consecutive Tedd messages or when the platform already makes authorship clear.

If directly asked whether the sender is AI:

- confirmed operator: `Yes. I'm Tedd, an AI agent working with <preferred name>.`
- unresolved operator: `Yes. I'm Tedd, an AI agent working through Codex.`

## Knowledge-graph relationship

The local profile is bootstrap and operational state; it is not a second canonical business graph. Durable person, role, alias, and agent relationships enter the graph only through normal evidence, authority, identity, and review gates.

Do not automatically merge historical Tedd entities or rewrite `skill:tedd-development` evidence URIs. A Graph Steward must review class, instance, component, alias, and redirect semantics before canonical identity maintenance.

For graph coordination, inspect an exact existing task consumer before registering it. Existing consumers keep their persisted producer ID and subscription cursors; new consumers use this profile's `agent_instance_id`. An optional explicit consumer rebind is a quiesced graph-maintenance action, not part of profile initialization or installation.

## Other machines

The profile and SQLite WAL store are same-machine resources. Do not synchronize or share them through a network volume, iCloud, Dropbox, or another file-sharing system. Multi-machine writers require one authenticated service that owns identity, authorization, graph transactions, revisions, subscriptions, and audit history.
