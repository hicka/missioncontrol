---
name: Mission brief
description: Break a substantial objective into independent pieces and hand them back for the right specialists.
avoid: Anything one worker can simply do; do it instead of planning it.
author: Mission Control
version: '1.0'
allowed_tools: [memory_search, remember, research, watch, skill]
max_tool_calls: 20
timeout_seconds: 900
inputs:
  type: object
  required: [objective]
  additionalProperties: false
  properties:
    objective:
      type: string
      minLength: 5
      description: The concrete outcome the owner wants.
    constraints:
      type: string
      default: ''
---

1. Restate the objective as an observable deliverable. Search memory for relevant
   decisions and constraints; verify any facts that may have changed.
2. Identify the pieces that can really run independently, and who on the roster
   suits each. Write each as a self-contained task with its context, constraints,
   required evidence, and the result to return. Do the piece that is yours yourself.
3. Report handback with those assignments, one per specialist. The Router starts
   them and each reports on its own. Use a watch when a specific external task is
   already running. Never poll in a loop of model calls.
4. A handback is not a finished assignment. Say which pieces are underway and which
   outcomes are still open; do not claim what has not come back.
5. Complete this skill with the assignments as evidence. Its deliverable is a sound
   breakdown, not a claim that the downstream work is done. If no suitable specialist
   or tool exists, fail with the specific missing capability.
