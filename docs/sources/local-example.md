# Try a local source from capture to memory

This operator walkthrough preserves a small UTF-8 note, links a compact memory to
it, and checks the saved wording. Use an approved **disposable** graph, archive and
private work directory. Complete [source setup](setup.md) first, including a real
registered consumer, subscription and authorized subject. Do not use production
state as a test fixture.

Check the [private-file and subscription requirements](setup.md#prerequisites)
before starting. In particular, metadata files and their immediate parent must
both be owner-only (`0600`/`0700` on POSIX; verified private ACLs on Windows).

Commands use Bash from the graph skill directory and the variables defined in
[archive setup](setup.md#enable-the-archive-and-scoped-model-access). Replace every
uppercase placeholder with the indicated value. On Windows, use the configured
Python interpreter and PowerShell syntax. This is a CLI exercise; real Codex
attachment selection and cancellation need their separate host checks.

## 1. Prepare the note and metadata

Save this synthetic text as `support-note.txt` in the approved private work
directory, encoded as UTF-8:

```text
Synthetic documentation fixture.
The worker must not stop while any job is queued.
Exception: it may stop after every queued job finishes.
```

Use `doctor` on the selected graph and `consumer-status` for the registered task:

```bash
python3 scripts/knowledge_graph.py --config "$graph_config" doctor
python3 scripts/knowledge_graph.py --config "$graph_config" consumer-status \
  --consumer-id "$consumer_id"
```

`doctor` supplies `graph.store_id`, `graph.generation` and `graph.watermark`.
Check that the existing consumer owner and subscription match the session you
intend to use. Obtain the namespace and subject from the authorized graph records.
Use the actual watermark read when preparing the graph mutation later.

Save the following specimen as private `capture-metadata.json`. Replace its
uppercase placeholders, including the generation **string** and capture time.
The task/run values must match the command session. `interaction_id` identifies
this actual acquisition encounter, and `tool_id` identifies its acquiring tool.
Use a stable local principal already chosen for this scope.

```json
{
  "scope": {
    "store_id": "STORE_ID_FROM_DOCTOR",
    "generation": "GENERATION_FROM_DOCTOR_AS_STRING",
    "namespace": "AUTHORIZED_SUBJECT_NAMESPACE",
    "subject_ids": ["AUTHORIZED_CANONICAL_SUBJECT_ID"]
  },
  "source_id": "local-file",
  "source_version": "snapshot",
  "kind": "text",
  "capture_method": "local_file",
  "tool_id": "ACTUAL_ACQUIRING_TOOL_ID",
  "task_id": "ACTUAL_TASK_ID",
  "run_id": "ACTUAL_RUN_ID",
  "interaction_id": "ACTUAL_ENCOUNTER_ID",
  "captured_at": "ACTUAL_CAPTURE_TIME_ISO8601",
  "observed_at": null,
  "origin": null,
  "authorship": "unknown",
  "coverage": "complete",
  "gaps": [],
  "privacy": "original",
  "identity_known": true,
  "details": {
    "provider": "local",
    "principal": "SCOPED_LOCAL_PRINCIPAL",
    "representation_id": "file",
    "representation": "original",
    "reader_id": "unread",
    "provider_revision": null,
    "title": "Synthetic worker stopping condition",
    "source_time": null,
    "retention": "retained_copy",
    "sensitivity": "ordinary",
    "identity_version": 1,
    "sheet_excerpt": null
  }
}
```

The unknown attribution and times above stay unknown; fill them only when actually
known. `source_id`, `source_version`, `reader_id` and `identity_version` describe the
input before local preparation. Intake derives the canonical path identity and
byte version, sets the acquired origin, and binds its reader. These provisional
values do not assert continuity after a rename.

## 2. Preserve and inspect the original

Hash the actual file that will be acquired. This example retains the whole short
note because both the rule and its exception are needed.

```bash
source_input='/absolute/private/work/support-note.txt'
metadata_file='/absolute/private/work/capture-metadata.json'
acquired_sha256=$(python3 -B -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$source_input")

python3 -B scripts/tedd_sources.py --config "$graph_config" intake-file \
  --input "$source_input" --metadata "$metadata_file" \
  --acquired-sha256 "$acquired_sha256" --workflow-state active \
  --purpose keep \
  --whole-file-reason "Both the rule and its exception are needed for this synthetic example" \
  --consumer-id "$consumer_id" --producer-agent-id "$agent_id" \
  --task-id "$task_id" --producer-run-id "$run_id" \
  --subscription-id "$subscription_id" --subject-id "$subject_id"
```

Expect `status: preserved`, `coverage: complete`, the actual `original_bytes`,
`new_bytes`, a `warnings` list and a `ref` object. Resolve any different result
before continuing. The graph still has no new memory at this point.

Build the source URI from the receipt's `ref.archive_id`, `ref.scope_id`,
`ref.manifest_sha256` and `ref.anchor_id`, in that order:
`tedd-source://<archive_id>/<scope_id>/<manifest_sha256>#<anchor_id>`.
Do not guess a digest or assume an anchor such as `u1`.

```bash
source_uri='URI_ASSEMBLED_FROM_THE_RETURNED_REF'

python3 -B scripts/tedd_sources.py --config "$graph_config" inspect \
  --source-uri "$source_uri" \
  --consumer-id "$consumer_id" --producer-agent-id "$agent_id" \
  --task-id "$task_id" --producer-run-id "$run_id" \
  --subscription-id "$subscription_id" --subject-id "$subject_id"
```

Check that the returned evidence contains both “must not stop” and the exception
about finished jobs. Original-byte hashes identify the saved evidence; they do not
prove the claim made by its author.

## 3. Review and bind a compact memory

Save this synthetic packet as private `memory-packet.json`. Replace the source URI,
subject IDs and timestamps. The `surface_id` identifies the stable place for this
claim and its revisions; choose an unused fixture value so it cannot collide
with another memory.
See the [capture schema](../../plugins/tedd/skills/obsidian-knowledge-graph/references/capture-schema.md)
for production packet semantics and authority ceilings.

```json
{
  "source": {
    "kind": "document",
    "role": "tool",
    "title": "Synthetic worker rule",
    "uri": "URI_ASSEMBLED_FROM_THE_RETURNED_REF",
    "observed_at": "ACTUAL_OBSERVATION_TIME_ISO8601",
    "source_tier": "validated_structured_candidate",
    "sensitive": false
  },
  "claims": [{
    "surface_id": "surface_documentation_worker_rule",
    "family": "documentation_fixture",
    "key": "worker_stop_condition",
    "value": "The synthetic worker must keep running while jobs are queued; it may stop once all jobs finish.",
    "subject_refs": ["AUTHORIZED_CANONICAL_SUBJECT_ID"],
    "entity_refs": ["AUTHORIZED_CANONICAL_SUBJECT_ID"],
    "valid_from": "ACTUAL_CLAIM_TIME_ISO8601",
    "conflict_policy": "preserve",
    "source_tier": "validated_structured_candidate",
    "sensitive": false
  }]
}
```

Keep the source body out of the graph packet. Use the same source scope and actual
graph watermark read for this operation. First create a new private review plan:

```bash
memory_packet='/absolute/private/work/memory-packet.json'
review_plan='/absolute/private/work/reviewed-memory-plan.json'
based_on_watermark='ACTUAL_GRAPH_WATERMARK_READ'

python3 scripts/knowledge_graph.py --config "$graph_config" capture \
  --input "$memory_packet" --consumer-id "$consumer_id" \
  --producer-agent-id "$agent_id" --task-id "$task_id" --producer-run-id "$run_id" \
  --discovery-kind verification --based-on-watermark "$based_on_watermark" \
  --source-subscription-id "$subscription_id" --source-subject-id "$subject_id" \
  --authority-ceiling validated_structured_candidate \
  --dry-run --plan-output "$review_plan"
```

Expect `status: would_capture` and `validation.valid: true`. Review the proposed
claim, source, scope and changes before applying. Keep the default `review_safe`
output. Use the returned fingerprint and exact plan with the unchanged packet:

```bash
fingerprint='FINGERPRINT_FROM_REVIEWED_DRY_RUN'

python3 scripts/knowledge_graph.py --config "$graph_config" capture \
  --input "$memory_packet" --consumer-id "$consumer_id" \
  --producer-agent-id "$agent_id" --task-id "$task_id" --producer-run-id "$run_id" \
  --discovery-kind verification --based-on-watermark "$based_on_watermark" \
  --source-subscription-id "$subscription_id" --source-subject-id "$subject_id" \
  --authority-ceiling validated_structured_candidate \
  --expect-fingerprint "$fingerprint" --expect-plan-file "$review_plan"
```

Expect `status: captured`. Select the claim ID from `claim_outcomes` or the
`changed` entry whose collection is `claims`. If the packet or graph has changed,
follow the returned conflict/replanning instructions; do not bypass the plan check.

## 4. Check the memory and finish

```bash
claim_id='CLAIM_ID_FROM_CAPTURE'

python3 scripts/knowledge_graph.py --config "$graph_config" source-evidence \
  --id "$claim_id" \
  --consumer-id "$consumer_id" --producer-agent-id "$agent_id" \
  --task-id "$task_id" --producer-run-id "$run_id" \
  --subscription-id "$subscription_id" --subject-id "$subject_id"
```

Read the returned claim and evidence. This small example should be complete and
retain both the stopping condition and its exception. Checking is read-only; it
does not correct the stored claim automatically. Remove the used private signed
plan after verifying the result. Dispose of only the synthetic fixtures you
created; use the reviewed [maintenance procedure](../../plugins/tedd/skills/tedd/references/source-viewing-and-maintenance.md)
for an archive that must be retained or contains other sources.

In Codex, `intake_file`, `inspect`, `bind_memory` and `check_memory` use these
operation flags in `argv`, without the CLI subcommand or `--config`. Their endpoint
must already be connected with trusted task-selection/turn hooks. Provider-checked
Google evidence requires that configured source endpoint and its provider session;
the local CLI walkthrough does not qualify that route.
