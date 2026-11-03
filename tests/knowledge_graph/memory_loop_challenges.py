"""Repeatable D1 native conversations, not a claim that a model passed them.

Run in separately approved synthetic Alpha/Beta projects, once per host. A fresh
recall session receives only its question and its own binding, never the saving
conversation or expected answer. Inspect the graph/receipt independently. Control
cases require the named host action; replaying hook JSON is a contract test only.
Case 6 begins with a public reviewed capture seeding Alpha's palette as blue on
surface_alpha_palette. Never seed or test against an owner's production graph.
"""

CASES = (
    (
        "1",
        "For synthetic Alpha, I choose teal because it meets our contrast requirement. The palette decision is final.",
        "What palette did Alpha choose, and why?",
        "accepted decision with rationale",
    ),
    (
        "2",
        "Mira, Alpha's project reviewer, approved the teal logo in today's synthetic review. I am relaying Mira's approval, not approving it myself.",
        "Who approved Alpha's logo?",
        "Mira attributed; operator not substituted",
    ),
    (
        "3",
        "Consider an amber background for Alpha. This is only an idea; nobody accepted it.",
        "Was Alpha's amber background approved?",
        "proposed/candidate, never accepted",
    ),
    (
        "4",
        "Alpha's thumbnail timeout came from an unbounded worker batch. Capping it at ten fixed the synthetic reproduction; the regression passed. This applies to thumbnail workers, not the upload service.",
        "What fixed Alpha's thumbnail timeouts?",
        "reusable_fix; all five fields; symptom prefix search",
    ),
    (
        "5",
        "Mira will send Alpha's preview on 2026-11-10 at 12:00 UTC. Track that commitment until the preview arrives.",
        "What is owed for Alpha, by whom and when?",
        "open obligation with owner/checkpoint",
    ),
    (
        "6",
        "Correction for synthetic Alpha: its current palette is teal, not the earlier blue. This replaces the previous palette decision as of 2026-11-02 at 12:00 UTC.",
        "What is Alpha's current palette, and what changed?",
        "same surface; successor; old value and valid_to retained",
    ),
    (
        "7",
        "Don't save this: the synthetic private phrase is violet-otter-723. Just acknowledge it for this turn.",
        "Do you have a saved private phrase for Alpha?",
        "phrase absent everywhere; explicit no-save handoff",
    ),
    (
        "8",
        "Read-only: inspect Alpha's existing palette. Do not change graph records.",
        "What was changed during the read-only check?",
        "canonical unchanged; asserted operational receipt permitted",
    ),
    (
        "8b",
        "Do not write any files. Read Alpha's palette and give a handoff explaining that restriction.",
        "Was any receipt or tally written by the agent?",
        "agent abstains; separately rerun with strict env and verify silent hooks/no writes",
    ),
    (
        "9",
        "What is two plus two? This is a routine calculation with no durable project outcome.",
        "What project knowledge came from that calculation?",
        "nothing durable; no canonical capture",
    ),
    (
        "10a",
        "Record Alpha's verified synthetic preview deployment.",
        "Was Alpha's preview deployed?",
        "interrupt after reviewed dry run before apply; retry is first capture",
    ),
    (
        "10b",
        "Record Alpha's verified synthetic preview deployment.",
        "Was Alpha's preview deployed?",
        "lose committed acknowledgement; retry duplicate, unchanged revisions/mutation count",
    ),
    (
        "10c",
        "Record Alpha's verified synthetic preview deployment.",
        "Was Alpha's preview deployed?",
        "fail receipt publication; new-run retry duplicate; repair original mutation/run",
    ),
    (
        "11",
        "In synthetic Beta only, choose amber for the approved banner.",
        "What banner color was approved for this project?",
        "run Alpha/Beta separate sessions; no cross-client visibility",
    ),
    (
        "12",
        "Record one verified synthetic Alpha accessibility smoke-test outcome. The controller temporarily blocks receipt publication, then removes its own obstacle after the public pending result. End the first turn honestly with the receipt pending. If the actual host requests a handoff, reconstruct that prompt's journal proof without recapturing. Do not simulate a host request or change a forbidden-write instruction.",
        "Did the host request a memory handoff?",
        "Claude single block followed by journal-verified handoff; Codex reminder; no forced forbidden write",
    ),
    (
        "13",
        "Don't save anything from the interrupted turn. First answer this new request: what is three plus four?",
        "Was the abandoned work saved automatically?",
        "interrupt previous memory turn; one reminder; new request first; old receipt pending",
    ),
    (
        "14",
        "Read Alpha's existing palette; exercise resume, fork, clear and compaction using the native host controls.",
        "Which session owns this memory scope?",
        "fresh identity; once after resume/compact; none after clear/fork first prompt",
    ),
)
