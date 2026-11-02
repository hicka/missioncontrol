---
name: Watch status
description: Monitor an already-running task or service until a clear success or failure condition is reached.
avoid: Starting deployments, mutating services, or scheduling open-ended recurring work.
author: Mission Control
version: '1.0'
allowed_tools: [watch, memory_search, skill]
max_tool_calls: 6
timeout_seconds: 180
inputs:
  type: object
  required: [name, poll, done_when]
  additionalProperties: false
  properties:
    name:
      type: string
      minLength: 1
    poll:
      type: string
      minLength: 1
      description: A read-only shell command or HTTP GET URL.
    done_when:
      type: string
      minLength: 1
      description: A regular expression matching a successful result.
    fail_when:
      type: string
      default: ''
    interval:
      type: integer
      minimum: 20
      maximum: 3600
      default: 30
    max_checks:
      type: integer
      minimum: 1
      maximum: 240
      default: 40
---

1. Confirm that the requested poll observes existing work. Never put a write,
   deployment, message, submission, or destructive command in a repeated poll.
2. List active watches and reuse an existing matching watch rather than creating
   a duplicate. The success regex must distinguish success from an error page;
   use fail_when for a known failed state.
3. Start the watch with the validated inputs. Read and report any tool error.
4. Tell the owner the watch identifier, interval, and maximum check count. The
   watch runs without further model calls and wakes the owning agent on resolution.
5. Complete this skill with the watch identifier as evidence of registration.
   Do not claim the underlying task has succeeded. When woken later, report the
   actual outcome and do not recreate a resolved watch automatically.
