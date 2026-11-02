---
name: Research brief
description: Investigate a question and deliver a concise brief with sources, uncertainty, and a recommendation.
avoid: Running deployments, modifying systems, or answering from unverifiable memory.
author: Mission Control
version: '1.0'
allowed_tools: [research, memory_search, remember, skill]
max_tool_calls: 12
timeout_seconds: 600
inputs:
  type: object
  required: [question]
  additionalProperties: false
  properties:
    question:
      type: string
      minLength: 3
    context:
      type: string
      default: ''
---

1. Search shared memory for relevant context and earlier decisions. Treat these
   as historical context rather than proof of current facts.
2. Use research with the question and context. Narrow a follow-up query if the
   first result leaves a material uncertainty. Prefer primary sources.
3. Compare the evidence and distinguish source statements from your inference.
   Preserve source URLs. Report conflicting evidence instead of hiding it.
4. Produce a brief with the answer, the evidence, uncertainties, and an actionable
   next step. Do not invent citations. If research is unavailable, fail the skill
   with the actual error and describe what evidence is missing.
5. Complete the skill with a short result and evidence containing source URLs and
   what they establish. Save only durable facts with attribution to team memory.
