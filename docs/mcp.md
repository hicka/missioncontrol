# MCP servers

Mission Control is an MCP client. Connect a Model Context Protocol server and your
agents can use its tools: a database, a ticket tracker, anything that speaks MCP.

## Adding a server

Settings → Integrations → **MCP servers**. Paste the `mcpServers` JSON that the other
app shows and press **Add**. The
same block works for Claude Desktop, Cursor and VS Code style configs:

```json
{
  "mcpServers": {
    "tracker": {
      "type": "http",
      "url": "http://127.0.0.1:9417/mcp",
      "headers": { "Authorization": "Bearer tk_…" }
    },
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Shared"],
      "env": { "SOME_KEY": "…" }
    }
  }
}
```

- **http** servers use the Streamable HTTP transport, with plain JSON or SSE replies and
  sessions. The older SSE-only transport is not supported.
- **stdio** servers are local commands. The **MCP servers** runtime service starts them,
  restarts them when they exit, and stops them when Mission Control quits. Runtime
  Manager lists the service.

Secrets never reach `settings.yaml`. A bearer token goes to `.env` as `MCP_<NAME>_TOKEN`,
a local command's `env` values become `MCP_<NAME>_ENV_<KEY>`, and any other header that
looks like a credential becomes `MCP_<NAME>_HEADER_<HEADER>`. Removing a server deletes
its secrets. Adding or changing servers is only possible from this Mac: these endpoints
refuse remote callers and cross-origin pages, because a stdio server is a command that
runs on your machine.

## How agents use it

Agents get one `mcp` tool instead of one tool per server tool. A bookkeeping app alone
brings two dozen tools, and offering all of them on every turn would crowd out the
work, especially for small local models. Each agent's context lists the servers it can
use with each tool's required inputs:

```
MCP SERVERS (…): tracker (Tracker): list_projects, add_ticket(title, kind), get_summary, …
```

- `mcp action=list` shows each server, its own instructions and every tool, marked
  read-only or needing approval.
- `mcp action=describe server=… tool=…` returns the full input schema.
- `mcp action=call server=… tool=… arguments={…}` runs it.

Arguments are validated against the tool's JSON Schema before anything is sent. Tool
errors come back as `error: …`, which the harness records as a failed call. Images
(for example a screenshot a tool returns) are passed to models that can
see them.

## The gate

- **Who.** An agent needs the `mcp` tool (blank tool allowlist, or `mcp` in its list).
  Each server can also be limited to some agent ids in the server row.
- **Destructive calls wait for you.** By default, a tool the server marks
  `destructiveHint`, or does not annotate at all (the protocol says to treat that as
  destructive), is held in the Inbox like a risky shell command. Approving sends exactly
  the arguments shown. Per server you can choose instead to be asked before any change,
  or never.
- **No accidental repeats.** A call that changes data and isn't marked idempotent is
  recorded in the action ledger. The identical call within the hour is refused unless
  the agent passes `again=true`.
- `server/mcp_servers.py` holds these rules and is on the self-improvement protected
  list, so no agent can propose loosening them.

Annotations are claims made by the server, not guarantees. Connect servers you trust.

## Not yet supported

OAuth sign-in flows, server-initiated sampling and elicitation, and exposing a server's
resources and prompts to agents.
