"""A tiny MCP server for the tests, over stdio or as an httpx handler.

Four tools cover the gate's cases: one read-only, one ordinary write, one the
server marks destructive, and one with no annotations at all (which the
protocol says to treat as destructive).
"""
import json
import sys

TOOLS = [
    {"name": "lookup", "description": "Find a record. Read only.",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "Add a record.",
     "inputSchema": {"type": "object", "properties": {"amount": {"type": "string"}}, "required": ["amount"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "wipe", "description": "Delete everything.",
     "inputSchema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}},
     "annotations": {"destructiveHint": True}},
    {"name": "plain", "description": "Does something unannotated.",
     "inputSchema": {"type": "object", "properties": {}}},
]
CALLS = []


def handle(message: dict) -> dict | None:
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "title": "Fake Books", "version": "9.9"},
                  "instructions": "Amounts are strings."}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        CALLS.append((name, args))
        if name == "lookup" and args.get("q") == "missing":
            result = {"content": [{"type": "text", "text": "nothing matches"}], "isError": True}
        elif name == "lookup" and args.get("q") == "picture":
            result = {"content": [{"type": "text", "text": "a receipt"},
                                  {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}]}
        else:
            result = {"content": [{"type": "text", "text": f"{name} ok {json.dumps(args, sort_keys=True)}"}],
                      "structuredContent": {"tool": name, "arguments": args}}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


if __name__ == "__main__":
    # Ask the client something first, as real servers may: it has to answer.
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"}) + "\n")
    sys.stdout.write("this is a log line a careless server printed\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") == "srv-1":
            continue                                   # the client's answer to our ping
        reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
