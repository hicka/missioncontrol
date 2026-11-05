# Modules

A module is something an agent builds to add to Mission Control while it runs. It is a
folder of Python whose `apply(ctx)` registers what it adds:

- **tools** the agents can call,
- **cards** the apps draw natively, which on the Mac sit on the desktop,
- **timers**, and things to **tell the owner** or **hand to an agent**.

Nothing about what a module is for is built in. Ask for "a card that checks our sites
every five minutes", "a desktop timer I can start and stop", or "today's open PRs at a
glance", and an agent writes the module that does it. A site check, a price tracker and
a build board are the same thing to Mission Control.

The design follows "everything is a plugin" from
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness): whatever a module
registers belongs to it and goes when it stops, so a new version starts from nothing and
a removed module leaves nothing running. This is an independent implementation, with its
own sandbox and trust rules.

## How a module comes to be

1. The owner asks for something. The agent reads `module(action="guide")`, writes the
   code and calls `module(action="create", module="site-health", files={...},
   permissions={...}, config={...})`.
2. The files are checked before anything is written: text files only, paths inside the
   module, Python that parses, at most 40 files and 512 KB.
3. If the owner asked for this work and the Mac sandbox is available, it starts at once.
   The tool waits for it to say it is ready, then for its first timers to draw or fail,
   and answers with its tools, its cards and its first log lines. An error comes back
   with the line in the module's own code, so the agent fixes it and calls `update`.
4. Anything else waits in the Inbox: a version written by standing work such as a job, a
   folder that appeared or changed on disk outside the tool, and every version on a
   system with no sandbox. Approving sends back the exact version that was shown, and a
   different one is refused.

Inferred work, from an outside event nobody asked about, cannot use modules at all.

## What the sandbox allows

Each module runs as its own process (`server/module_host.py`), so a crash takes only
itself down. A crash comes back after 5, 15, 45, 120 and 300 seconds, and a module that
keeps crashing stops and tells the owner. One that fails to load is not retried, since
it would fail the same way.

On macOS the process runs under `sandbox-exec` with a profile written for it
(`modules/.run/<slug>.sb`). It:

- reads nothing under `/Users`, `/Volumes`, temporary folders or the data folder, except
  its own folder, Python and Mission Control's code;
- writes only to its own `data/` folder (also its `TMPDIR`), never to its own code;
- reaches the network only with `permissions.network`;
- gets `.env` values only as `permissions.keys` names them, as environment variables;
- reads other paths only as `permissions.read` names them, and never the data folder;
- sends no Apple events, reads no clipboard or keychain, and cannot signal or inspect
  other processes.

Nothing a module sends is run by the server or the apps. Cards are data drawn natively.

## The context

```python
def apply(ctx):
    ui = ctx.ui
    card = ctx.card("main", title="Sites", symbol="globe", size="medium")

    @ctx.every(300)                     # a timer, from when the module is ready
    def check():
        card.render([ui.status("example.com", tone="ok", detail="200 · 52 ms")], tone="ok")

    @card.on("recheck")                 # a button on the card
    def recheck(event):
        check()

    @ctx.tool("check_now", "Check every site now")
    def check_now(args):                # module(action="call", module=..., tool="check_now")
        check()
        return {"checked": True}
```

| | |
| --- | --- |
| `ctx.config`, `ctx.state`, `ctx.save()` | Its settings, and a dict kept across restarts |
| `ctx.data` | Its writable folder |
| `ctx.tool(name, description, schema)` | A tool for the agents; arguments are checked against the schema |
| `ctx.card(id, title, subtitle, symbol, size)` | A card; `card.render(view, tone=...)` replaces what it shows |
| `card.on(action)` | A button or toggle: the event has `action`, `value` and `values` (the card's fields) |
| `ctx.every(seconds)` | A timer, 5 seconds at least |
| `ctx.effect(start)` | Runs `start()` now, and what it returns when the module stops |
| `ctx.notify(text)` | Tells the owner; one a minute, 20 a day |
| `ctx.ask(text)` | Hands an agent something to look into, as a job would; one every 10 minutes, 12 a day |
| `ctx.log()`, `print()` | Its log, read with `module(action="logs")` or in the app |

## Cards

A card is a tree of typed nodes, checked by `server/module_ui.py` in the module's own
process and again on arrival. A list is a column. Fields a node does not take are refused
rather than dropped.

| Node | Fields |
| --- | --- |
| `stack` | `axis` (v, h), `spacing`, `align`, `children` |
| `grid` | `columns` (1 to 6), `spacing`, `children` |
| `text` | `text`, `style` (title, headline, body, caption, footnote, mono), `tone`, `lines` |
| `stat` | `label`, `value`, `unit`, `detail`, `tone`, `trend` (up, down, flat) |
| `status` | `label`, `tone`, `detail` |
| `badge`, `progress`, `chart` | a pill; a 0 to 1 bar; a line or bars from `values` |
| `symbol`, `table`, `link` | an SF Symbol; `rows` and `columns`; an http(s) address |
| `button`, `field`, `toggle` | an `action`; a text field whose value goes with every action; a switch |
| `divider`, `spacer` | |

Tones are `ok`, `warn`, `bad`, `info`, `muted` and `accent`. A card's own tone colours its
edge. Each card says by itself how long ago it was drawn.

On the Mac each card is a panel at the desktop's level, behind every window and on every
Space. It can be dragged anywhere, and its right-click menu keeps it above windows, takes
it off the desktop, restarts its module or opens it in the app. **View > Hide Desktop
Cards** hides them all. The Modules screen shows every module with its permissions, tools,
cards, code and log.

## For the apps

| | |
| --- | --- |
| `GET /api/modules` | Every module and card |
| `GET /api/modules/{slug}` | One module with its files and log |
| `POST /api/modules/{slug}/{approve,decline,start,stop,restart,remove}` | The owner's controls; approve takes `{"version": <hash>}` |
| `POST /api/modules/{slug}/cards/{id}/action` | A press on a card: `{"action", "value", "values"}` |
| `PUT /api/modules/{slug}/cards/{id}` | `{"desktop": bool}` |

Events on `/ws`: `modules_updated`, `module_card` (a card as drawn, or `removed`), and
`module_cards` (read them again).

## Not yet

- The web workspace and the phone app list nothing yet; the Mac app draws the cards.
- Only Python modules. The protocol is JSON lines over stdin and stdout, so other
  languages could follow.
- There is no sandbox on Linux or Windows, so there every version waits for approval.
- A module's tools are reached through `module(action="call")`, not as tools of their own.
