# Agent skill harness

A skill is a procedure in `skills/<slug>/SKILL.md`. It uses the ordinary tool
dispatcher; it does not load arbitrary Python plugins or execute scripts just
because they are present. The owner can inspect and edit skills in Settings →
Memory, or launch them from the Skill library.

## Contract

```yaml
---
name: Verify a release
description: Verify the evidence for an already-built release.
avoid: Publishing or deploying the release.
version: '1.0'
author: owner
agents: [engineer]
platforms: [macos, windows, linux]
allowed_tools: [terminal, memory_search, skill]
max_tool_calls: 12
timeout_seconds: 600
inputs:
  type: object
  required: [repository]
  additionalProperties: false
  properties:
    repository:
      type: string
      minLength: 1
---

1. Inspect the release's test results in the supplied repository.
2. Verify the relevant checks with ordinary tools.
3. Report what passed and what remains uncertain.
4. Complete the skill with the result and observed evidence.
```

`inputs` uses JSON Schema draft 2020-12. References must be local to the schema;
validation never downloads external schemas. Input values are not interpolated
into shell commands by the harness. The model receives them as task data.

Omitting `agents`, `platforms`, or `allowed_tools` leaves that dimension
unrestricted by the skill. Agent settings still apply. Defaults are 20 tool calls
and 600 seconds. Limits range from 1–200 calls and 1–3600 seconds. Hyphenated
spellings such as `allowed-tools` are accepted as aliases. Tool allowlists use
Mission Control tool names, not another agent product's permission syntax.

Existing skills without contracts remain readable and use the default budgets.
Invalid manifests are flagged in the library and cannot be activated. Files must
remain inside the skill directory, must be UTF-8, and are limited to 128 KB.

## Lifecycle

1. **Discover:** agents receive an index of available procedures and their negative
   triggers. `skill(action="list")` retrieves the index.
2. **Inspect:** `skill(action="read", name="verify-a-release")` reads instructions.
   Reading alone does not activate the skill or apply its tool restrictions.
3. **Activate:** `skill(action="activate", name="verify-a-release",
   inputs={"repository":"…"})` checks the contract and starts a durable run.
   `/skill verify-a-release {"repository":"…"}` does this at mission startup.
4. **Execute:** every tool call validates its arguments and checks agent access,
   the active skill's allowlist, call budget, and remaining deadline. Tool errors
   return to the model as errors, with a visible execution record. A skill cannot
   grant tools disabled for the agent. A second skill cannot replace an active
   run to reset its budget.
5. **Finish:** `skill(action="complete", result="…", evidence="…")` records an
   explicit outcome. `skill(action="fail", result="…")` records a blocker.
   Finishing remains available after exhausting the skill's tool budget.

One skill may be active per agent at a time. Ending a turn without an explicit
outcome records `unverified`, rather than silently reporting success. Cancellation
and server restarts produce distinct records. Skill evidence is supplied by the
agent and available for inspection; it is not an independent proof of correctness.

Skill learning preserves existing contract fields. Editing the complete manifest
through the owner's editor can change those fields. Writes and use counts are
atomic, and concurrent reads cannot lose usage increments. Contracts apply at the
tool level: allowing `terminal` is not a filesystem or operating-system sandbox.
The existing terminal guards, action ledger, and model-spend limits still apply.

## Agent limits and records

In an agent's editor, expand **Execution & skill harness**. Blank allowlists use
all available tools or skills, `none` disables them, and comma-separated names
restrict them. Equivalent settings in `agents.local.yaml` are `tools`, `skills`,
`max_tool_calls`, and `max_tool_rounds`.

Tool and skill records share an execution identifier, agent, timestamps, status,
mission identifier, and result. Skill records also include the manifest version
and content hash; tool records link to the active skill run. Audit output is
bounded and redacts configured secret values and common credential fields.
The command deck's live activity inspector exposes these records, including
failed and cancelled calls. Recent records are also available at `GET /api/control`.

Model/provider failures are not proof that external effects did not occur. After
an interrupted outward action, inspect the action ledger and current external
state before deciding to retry. Missions on connected machines retain the
existing fleet messaging behavior; the new participant tracking covers local
agent queues.

## Verification

Run `python -m pytest tests -q`. The regression suite covers invalid tool and
skill inputs, permission intersections, skill budgets and deadlines, cancellation,
durable mission recovery, isolated inboxes, watch races and notification retries,
memory edit replication, and bounded subprocess output.
