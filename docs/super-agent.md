# The Super-agent kernel

Mission Control's core follows the design in DeepSeek Harness's article
[Exploring Super-agent Design](https://x.com/deepseekharness/status/2116857257245987236),
with [dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)
as a reference. It is an independent Python implementation, not a port. The tools,
channels and integrations carried over unchanged; what was replaced is everything
that decided who works on what.

To the owner there is one agent, reachable from anywhere, that always knows what is
going on. Sessions are an implementation detail.

```
 iMessage  Telegram  WhatsApp  email  app          GitHub  webhooks  cron  watches  other machines
     \        |         |       |     /               \       |       |      |        /
      owner, always explicit                           admission: explicit | candidate | ignore
              \                                               /
               \                 triggers.py                 /
                `--------> record it, then route it <-------'
                               |               |
              nobody follows   |               |  a worker follows this Thread:
              this thread      v               v  straight through, no decision spent
                        +-------------+     +---------+ +---------+
                        |   Router    |---->| Worker  | | Worker  |  one isolated session per task
                        | no shell    |<----| tools   | | tools   |  slow tools run as background jobs
                        +-------------+     +---------+ +---------+
                         the only voice      report, never speak
                               |
                       the owner's channels                      watchdog.py checks all of it on a schedule
                                                                 bin/mc-watchdog checks the process itself
```

## The parts

| File | What it holds |
| --- | --- |
| `server/triggers.py` | Where every event enters. Admission by source and sender, pass-through to followers, a free noise check, and the record of what became of each event. |
| `server/router.py` | The one long-lived session and the agent's only voice. Reads batches, decides, replies. It has no shell. |
| `server/workers.py` | One isolated session per task, bounded concurrency with a place kept for the owner, reports, stop and restart recovery. |
| `server/session.py` | The loop they share: an asynchronous ReAct loop over a durable transcript. |
| `server/workspace.py` | Which Worker has a repository's shared checkout, and a private copy for anyone else who needs it at the same time. |
| `server/recall.py` | Inline & Spill, and the Chain of Recall built on it. |
| `server/watchdog.py` | The scheduled check that sessions are really getting somewhere, and a session you can question. |
| `bin/mc-watchdog` | A small process outside the app that restarts it if it stops answering. |
| `server/kernel_store.py` | Everything that must survive a restart. |
| `server/orchestrator.py` | The roster, the tool definitions and what each tool does, and getting words to a person. |

Vocabulary: a **Trigger** is one immutable event. A **Thread** is the stream it belongs
to: a **conversation** is a chat with the agent as a whole, which nothing subscribes to;
a **topic** is one piece of the world (a PR, a scheduled job) that a Worker follows. A
**profile** is an entry on the roster: a role prompt, a repo, a model, tool permissions.
A Worker runs as a profile, and several can run as one profile at once. A **task** is
what the Tasks page shows, the `missions` table.

## How an event moves

1. `Intake.ingest` records it, deduplicated, with its thread. Admission runs first, in
   code: the owner is always explicit, and each other source is explicit, candidate or
   ignore (Activity page, or `/policy source name value`).
2. If it is addressed to a session (a watch it armed, a command it asked about), it goes
   there. If a Worker follows its thread, it goes straight to that Worker. Either way the
   Router gets one line in its next batch and spends no decision. What the owner says on a
   thread a Worker only follows for context goes to the Router too, because nobody has been
   given that request yet.
3. An automated event with a known owner (a scheduled job, a heartbeat) starts its own
   short-lived Worker, without routing.
4. Candidates get a free read from Laya, on this machine, which may only ever drop what
   it is sure is noise. It fails open, and nothing here calls a hosted decision model.
5. Everything left goes to the Router, which judges it against the owner's policies.

A source can say more about an event than who it came from. `followers_only` means only a
Worker already on that thread could care: a bot's comment, CI passing, a push, an edit, our
own account talking. Nobody following, it is recorded as ignored at no model cost. `quiet`
means it should not wake a Worker that is not in the middle of something. `needs_decision`
takes it to the Router even with someone following, as with a new PR needing a reviewer
while its author follows it. `closes` ends a finished Worker on that thread without waking it.

Every event ends with a disposition you can see on the Activity page: `routed`, `passed`,
`answered`, `read`, `ignored`, `refused` or `logged`. `answered` means a reply went out;
`read` on the owner's message means none did.

Events a restart catches on the way in are routed when it comes back. GitHub events also
come from a poll with a cursor on comments, reviews and runs, because GitHub does not retry
a failed delivery and the tunnel's address changes when it restarts.

## Reaching the owner

- `Orchestrator.deliver` is the one way out. Everything lands in the app. A phone channel
  gets it through the outbox: written first, marked sent once a channel has taken it, retried by
  the Watchdog for six hours, and a push goes out if the channel failed. Channels mostly
  report failure by returning False, which counts as failure. A message still on its first
  send is left alone by the retry, since a slow channel often delivers after all.
- `reply` takes a channel name or a conversation id, and anything else is an error. The
  Router's prompt lists where the owner can be reached.
- A report goes to wherever the owner last spoke, not where the task started.
- A held command, or a job waiting to start, is said to the owner in code, with a push if
  that did not already reach their phone. Where the code that held it can say why (it reaches
  the network, it touches a file where keys live, it changes something outside this Mac), the
  notice says so. The Router gets a line saying they have been told.
- A report read in the same batch as the owner's message is still news for them: answering
  the message does not leave what is said of the report with nobody to hear it.
- A Worker's question stays on the session until answered: on the Router's board, in
  `/tasks` and in the Inbox, where it can be answered directly.
- Work shows as it is done. `GET /api/work` gives each task a conversation started: who has it,
  what they are doing this moment (thinking, working, fixing after a failed step, waiting and on
  what, or needing the owner), its steps and its finish line. A tool call can carry a `note`, a
  few plain words on what the step is for, and each live `execution` event carries the line a
  person reads. The Mac app draws these as a card in the conversation that fills in step by step
  and folds to one line when the task ends.
- Things that have an address are linked. Workers give the address of what a report names (a
  pull request, an issue, a build, a page) and the Router keeps it as a link on the name. Nothing
  is built from a pattern: an address is only ever one a Worker saw.
- Words the Router writes with nobody waiting are handed back once, because it believes it
  has said them. An empty ending with the owner waiting is asked again, then says so.

## How a task ends

Only a report ends a task. Plain words get one reminder to report, and if it still will not
say, they go to the Router marked as unconfirmed. Direct lines and scheduled runs are the
exceptions: there the plain answer is the whole point. Silence is fine after a notice, but
when someone is owed an answer it gets the same reminder, and then a `failed` report.

Work a person asked for has a finish line: what they asked for, as a few checks in their
words, each done or not, with how it was confirmed. The Worker gives it in `checks`, first
report onwards, and it is kept with the session: a report that leaves a check out leaves it as
it was, so none can go by saying nothing. `ready` is refused until every check is done with how,
and a way of confirming that names an id the Worker never saw is marked unverified. A Worker
that stops with checks still open, after a progress report or plain words, is not filed as
done: it becomes `blocked`, asking the owner whether it should carry on, with what is left.
The Router sees the finish line in each report and its count on the board. Direct lines,
scheduled runs, work done ahead of time and work from outside events have none.

`progress` is not an ending either: it says what comes next. A Worker that stops after one is
reminded once to carry on, and if it stops again it becomes `blocked`, asking whether it
should. Each report the Router reads says when it was written and whether the same Worker
has reported since, and the board is shown again whenever the owner writes.

`waiting` is not an ending: the Worker stays where it is, with its authority, whatever it adds
afterwards. It is only true of something that will wake it (a job, a watch, a held approval, an
event on a thread that can still bring news), and it is told at once when nothing will. If it
stops there anyway, what it is waiting for is a person, so it becomes `blocked` with its last
words as the question. Doing anything further makes the report stale, so the usual rules apply.
A Worker that is asked and will not say keeps waiting if nothing has happened since it said so,
and the Router hears about it once, or again if the owner has spoken since. The first waiting report after the owner speaks wakes the
Router, since it may be their answer; the rest do not. A PR closing ends the following of it.
If the Watchdog has told a Worker twice and it is still stuck on nothing, it becomes `blocked`,
with a question for the owner, rather than sitting on its authority for ever. A held command
counts as something to wait for during its first day; after that the Worker is told not to
count on it. Time the machine spent asleep does not count towards being hung.

A scheduled run ends with any final report, so the next firing starts its own Worker. One
that is blocked waits for its answer without holding up the job.

A PR a Worker opens is bound to it, with its branch, and a Worker that has finished ends
when that PR closes, without being woken to hear about it.

`stop` ends all of it: the task, every process it started including what outlived its
shell, its watches and its held commands. What the owner said that it had not read goes to
the Router. Events on its threads start nothing new for 24 hours unless the owner speaks
there, though anyone else still working on that thread keeps hearing them. Anything a task
leaves running once it is no longer active is killed after six hours.

A tool budget is not an ending either. A Worker at its `max_tool_rounds` or `max_tool_calls`
writes a checkpoint and carries on from it with a fresh allowance, with its authority and with
nobody woken. Each stretch is a new run, so it gives up its place to anyone queueing and the
daily budget applies again, and the Watchdog judges whether it is getting anywhere as usual.
What it did in earlier stretches still counts: the outward actions it must not repeat, the trail
it learns from, and whether the owner spoke. Only the stretch that finishes is scored.

After five stretches on one piece of work it stops and asks, as `blocked` with a question, and
anything else that comes in is kept for when it is answered. Anything the owner says, or a final
report, starts the count again; its own question does not. A Worker that had already asked
something goes back to asking that, whatever else landed meanwhile. One in the middle of a skill
asks straight away, because a skill's contract lasts one run. Work done ahead of time gets one
stretch and is then dropped, since nobody is waiting.

## Authority

The model reading a request is what an attacker would target, so authority is settled in
code, from who sent an event, never from what the model concludes about it.

- **Explicit** work came from the owner's own message. **Standing** work came from
  something the owner set up (an approved job, the heartbeat). **Inferred** work came from
  an event or a policy. A report handed on carries the authority its worker had, no more.
- Authority lasts as long as the task. A Worker that reports ready or handback, or goes idle
  any other way, drops to inferred, and a comment waking it later is not the owner asking it for
  anything. The
  owner's next word gives it back, and so does a reviewer's findings on a task they asked for.
- Inferred work is held back from outward commands and from reaching the network, with at
  most two waiting at a time per task, and from the owner's settings, keys and private data. It cannot use
  `claude_code`, `improve`, `voice_reply`, `cron`, `browser`, `apple`, `whatsapp`,
  `mail_cleanup`, `mcp`, `research`, `generate_image` or `decide`, and it cannot write to
  what the team reads as fact: `remember`, `forget`, `save_skill`, `save_playbook`. It runs
  without the owner's keys, nothing is learned from its trail, and `recall` shows it only its
  own session and reports, and GitHub threads. Credentials are masked in what any tool returns. Reading through `gh` stays open, and so does pushing to the
  branch of a PR it opened itself, which changes nothing until someone merges it.
- Standing work (a job, the heartbeat) has outward commands and the owner's secrets held. A
  pulse once ran a production deploy on its own. It keeps `claude_code`, because jobs use it to
  prepare things, with `git push`, deploys, publishing and network clients disallowed in there.
- A profile can be given repositories to run on its own (`own_repos`, Limits and permissions on
  its profile). There its standing and explicit work pushes branches, opens, reviews and merges
  pull requests, and comments on issues without being held, and it may post even when only the
  review profile otherwise does. This is how a team keeps a repository going between the owner's
  messages. A force push, a push to a shared branch, tags, releases, settings, secrets and
  `gh api` writes stay held, as does any command `github.own_repo_work` cannot read for certain.
  Inferred work gets nothing from it: an outsider's issue never ships code by itself.
- A watch is a shell that runs unattended, so it gets no more than the terminal would allow
  that same work: nothing outward for anyone, no network for inferred work, and the owner's keys
  only for work they asked for. A job's `monitor_cmd` passes the same check.
- Agents run `gh` as their own account (`GITHUB_BOT_TOKEN`), so what happens under the owner's
  login is the owner, with three kinds of exception: merging, which is theirs to decide whoever
  carries it out; secrets and repository settings; and `git push`, which uses this machine's
  git credentials. A merge, close or push caused by one of those says so when GitHub tells us.
  PRs opened by agents come from the bot account, so GitHub refuses approve and request-changes on
  them from that same account: the reviewer comments its verdict instead.
- Only the review profile (`github.review_agent`) posts on GitHub: comments, reviews, new
  issues. Anyone else who tries is refused and hands the text over. `claude_code` has the plain
  posting and merging commands disallowed, and anything else it does through `gh` is under the bot
  account. `github.said_by_owner` treats a comment under the owner's name as theirs, unless a
  command of ours came back with its id. Without a bot token it also goes by timing and by whether
  `claude_code` is running in that repository. It grants that worker the owner's authority, and a
  stranger's comment grants nothing.
- The reviewer may post on GitHub unasked, because it does so as its own account, which the
  terminal stops from merging, changing settings or running PR code.
- Without a message from the owner in the same batch, the Router cannot change a policy, stop
  work, schedule anything, or save or forget a memory. A batch means that one read: a stranger's
  event read later in the same run has no owner behind it, and an old message of theirs,
  dispatched with nothing from them now, grants nothing.
- A standing rule has to come from the owner's own message, and only they can take one back.
- A job only starts by itself if someone they asked set it up. Anything else waits for them.
- A reply can attach only files a Worker reported. The Router cannot post on GitHub.
- An outsider cannot reopen a finished task.
- Self-repair cannot touch the files that hold these gates (`selfimprove.PROTECTED`).
- An agent's proposal to change Mission Control waits for the owner, unless they let agents
  approve each other's (Settings > Improve). Then a second agent approves it, never its
  author, and it lands only as the owner's approval would: tests green, no protected file,
  nothing running, the rollback watchdog armed.

**This is defence in depth, not a sandbox.** Inferred work still runs as the owner's user
on the owner's machine. It can read files the patterns do not name, a determined script
can get past a command pattern, including the one that keeps a shell away from this app's own
controls (`bin/mc`, the send and approval endpoints), which trust anything local. The bot account's token is in its environment. Treat turning a stranger-writable source to `candidate`, with a policy that
dispatches work, as giving strangers a way to suggest work to something that has your
files. The article lists this among its open problems, and it is one here too.

## Departures from the article

- **It runs on this machine, not in the cloud.** State is already separate from the loop,
  so a restart picks up where it left off, but when the Mac sleeps the agent sleeps.
- **No remote shell daemon.** A task for another machine is sent to the Mission Control on
  that machine, which has its own Router and hands. Nothing new on the network can execute
  commands, and an offline machine gets the task when it returns.
- **The Router reads a noise filter first**, using local Laya when it is on. The hosted
  Jev model keeps its existing jobs (terminal guard, learn gate, watch resolution) and is
  not given outside events.
- **Direct lines.** Writing to a specialist by name reaches a long-lived Worker that
  answers in that chat, with no Router in between. It is how you step in.
- **Workers do not spawn their own subagents** except through `claude_code`. A worker that
  needs someone else reports `handback`, and the Router starts them.

## Working copies

One Worker at a time has a repository's shared checkout, with its caches and build products.
Anyone else who needs that repository meanwhile gets a copy-on-write clone under
`~/.mc-workspaces`: their own working tree, clean and on the default branch as GitHub has it,
with dependencies and ignored config carried over and nobody else's local branches. Commands that name the shared path are
pointed at the copy, because skills and prompts name that path. A folder with no remote is not copied: there the
folder is the work. A copy is deleted when its task ends, unless it holds unpushed work, which is kept for 30 days. Uncommitted changes in the
checkout count as someone's, so a Worker that finds them gets a copy rather than working on top.

## Several tasks at once

A profile is a role, not a seat: each dispatch is its own session, with its own transcript and,
where it needs a repository someone else has, its own copy. So someone in the middle of one task
takes another without dropping the first, and the Router is told to hand new work to whoever
suits it rather than whoever is free.

What that isolation hides, `server/awareness.py` shows. Every Worker opens with the other tasks
under way: who has each, where, on which branch, and the files it has changed so far, and reads
them again each time it wakes. After a step that can change files, its working copy is compared
with the others'. Two tasks that have changed the same file are each told once, at their next
step and without being woken, and the Router is told too, since their results have to be merged
one after the other. The Router's board lists what each task is changing. Starting the same
profile on what reads as a task it already has is refused once, with `separate` to say it really
is different work. None of this is enforced: it is what a colleague would know from sitting in
the same room.

File names are whatever the author of a pull request called them, so wherever one is shown it is
quoted and cut short, and for work an outside event started only their number is given. Such work
is itself told who is working where and on which files, and not what the others were asked for.
A finished task whose pull request is still open stays on the list until it closes.

A task's course can be changed while it runs. The owner's message reaches its Worker at its next
step, through the Router's `attach` or straight from the task's card
(`POST /api/kernel/sessions/{id}/message`), and outranks whatever the Worker had planned.

## Memory and cost

- A transcript is compacted by what it holds in text and tool output. Thinking signatures and
  image data do not count. The previous digest goes into the next one whole, and what the task
  is stays pinned word for word above every digest.
- When the Router compacts, it reads what the owner has said since last time for rulings that
  should outlast the conversation, and saves them as decisions. Only the owner's stored
  words go in, so nothing anyone else wrote can become one.
- Providers that support it read the prompt, tools and transcript back from cache. The clock is
  not in any system prompt, for that reason: each batch starts with the time. Cached input is
  counted apart, and counts for a tenth in the daily budget.
- A cut in a tool result says so. `job` and `recall` take an offset to read the rest.
- A session's opening context is chosen for its task. Memory is searched by match, then by what
  kind of note it is (a rule, a decision or a lesson outranks a line of the event log) and its
  age, and one fact in two wordings is shown once. The owner's standing rules go in whole while
  they fit; past that, the ones the task touches go first and the rest are counted out loud.
  Skills the task matches are described; the others are named. A dependency bot's open PRs are a
  count. What is said again each time a Worker wakes is only what can have changed.
- `remember` updates a note that already says nearly the same thing instead of adding another.
  Anything with a number in it has to match exactly, so "#522 merged" never replaces "#523 merged".
- The heartbeat looks less often when there is nothing to find: with no work under way and
  nothing new since the last pulse, pulses come four times further apart (`heartbeat.quiet_stretch`,
  1 to switch that off). Anything happening brings them straight back.
- Learning looks at how a task went and whether it keeps coming back. The trail marks the calls
  that failed, the reflection is told when this profile has done the same kind of task before,
  and a procedure learned a second time revises the first skill instead of sitting beside it.
  Lessons learned from failures have their own places, which the owner's rules do not use up; one
  on trial is not pushed out before it has had its time, an agent's own lessons keep half the
  room in its prompt, and a lesson nobody was shown is not adopted for what it did not prevent.
- Once a day the Watchdog deletes what nothing reads any more: messages sent over a week ago,
  mail taken and transcript rows compacted away over a month ago, and tool-call records over
  two months old. Events are kept, because telling a new one from a repeat rests on them.

## When a provider refuses

Being down is retried, with a stalled Worker said to the owner once. Being down includes a
provider that accepts a request and then says mid-stream that it is too busy, a reply that
stops arriving before it ends, and an error page served as success. A reply that never comes
is waited out twice, not four times. A tool call that arrives garbled is refused in words and
kept in a shape the provider will take back, so one bad call cannot end its task. Refusing is about what
is in the transcript, so retrying gets the same answer for ever. The Router first strips
thinking blocks, which belong to the model that wrote them. Then it sets the batch aside and
tells the owner what it could not read. Then it drops the history, keeping the digest. From
there each batch that is refused is set aside and said, so none can hold up the next. It asks
something harmless first: a provider that refuses that too has a bad model name or the like,
nothing is thrown away, and it waits and says so once, as for an outage.

## Running it

- `/tasks`, `/events`, `/policy`, `/status`: answered in code, with no model call.
- `/pause` and `/unpause`, or the button on the Activity page: stop picking up work without
  stopping the app, which would cost each running task a recovery. Events are still recorded,
  what was already said is still delivered, and a running session stops at its next step.
- `/watchdog why did that stop?`: ask the watchdog anything; the answer lands in the chat.
- Settings in `settings.yaml` under `superagent:`
  - `router_model`, `worker_model`: `{provider, model, effort}`, otherwise the lead's model
    and each profile's own, then the team default.
  - `max_workers`: default 6.
  - `sources`, `ignore_actors`, `reply_caps`.
- The outside watchdog, once: `packaging/macos/install-watchdog`. It leaves a service you
  booted out alone, and when restarts stop helping it tells you on your phone and on this Mac.
  Inside the app, the loops that keep it going (watches, the Watchdog, scheduled jobs, the
  heartbeat, the GitHub monitor, modules) are started again, with a growing wait, if one ends.
  `bin/mc-watchdog --once` answers in its exit code: 0 all is well, 1 it is not answering and a
  restart may help, 2 it is answering but something is wrong that a restart would not fix.
