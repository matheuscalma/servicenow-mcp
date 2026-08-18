# servicenow-mcp

An [MCP](https://modelcontextprotocol.io) server that bridges AI coding agents (OpenHands,
Claude Code, Claude Desktop, …) to ServiceNow ITSM. It exposes three tools — create, query and
update incidents — over the ServiceNow Table API, so an agent that just shipped a fix can
also leave the change trail where ops teams expect it: an incident with work notes and a
state. Single file ([`server.py`](server.py)), official Python `mcp` package, `httpx`,
`python-dotenv`. Built and verified against a live ServiceNow instance (Australia release)
over both **stdio** and **streamable HTTP** transports.

## Verified end-to-end

An OpenHands agent picked up
[todo-api issue #2](https://github.com/matheuscalma/todo-api/issues/2) in the demo repo
([matheuscalma/todo-api](https://github.com/matheuscalma/todo-api)), fixed the code on a
feature branch, ran the full test suite (24/24 passing) and opened
[todo-api PR #4](https://github.com/matheuscalma/todo-api/pull/4). It then used this
server to create incident **INC0010002** documenting the change, attached the PR link as a
work note, and moved the incident to *In Progress*. A human reviewed and merged the PR and
resolved the incident — code change and ITSM record closed together, no manual ticketing.
(All issue/PR references in this section are in the todo-api demo repo, not this one.)

| Step | Actor | Tool / artifact |
|------|-------|-----------------|
| Pick up [todo-api issue #2](https://github.com/matheuscalma/todo-api/issues/2), fix on a feature branch | OpenHands agent | git |
| Run test suite → 24/24 | OpenHands agent | pytest |
| Open [todo-api PR #4](https://github.com/matheuscalma/todo-api/pull/4) | OpenHands agent | GitHub |
| Create incident INC0010002 describing the change | OpenHands agent | `create_incident` |
| Attach PR link as a work note | OpenHands agent | `update_incident(work_notes=…)` |
| Move incident to In Progress | OpenHands agent | `update_incident(state="In Progress")` |
| Review + merge PR, resolve incident | Human | GitHub / ServiceNow UI |

Artifact trail: [todo-api issue #2](https://github.com/matheuscalma/todo-api/issues/2)
→ branch → [todo-api PR #4](https://github.com/matheuscalma/todo-api/pull/4) → INC0010002
(work note with PR URL, state New → In Progress → Resolved).

## Architecture

```
┌──────────────────────┐   MCP (stdio  or   ┌───────────────────┐   HTTPS / basic auth   ┌────────────────────┐
│  Agent               │   streamable HTTP) │  server.py        │   Table API (JSON)     │  ServiceNow        │
│  OpenHands / Claude  │ ─────────────────▶ │  create_incident  │ ─────────────────────▶ │  /api/now/table/   │
│                      │ ◀───────────────── │  query_incidents  │ ◀───────────────────── │      incident      │
│                      │   tool results     │  update_incident  │   records / errors     │                    │
└──────────────────────┘                    └───────────────────┘                        └────────────────────┘
                                                     │
                                                     └── ServiceNowClient (httpx.AsyncClient, timeouts,
                                                         readable errors; swappable for tests)
```

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create a `.env` next to `server.py` (it is git-ignored) with basic-auth credentials for
your instance — a [Personal Developer Instance](https://developer.servicenow.com/) works fine:

```dotenv
SNOW_INSTANCE_URL=https://devXXXXXX.service-now.com
SNOW_USERNAME=admin
SNOW_PASSWORD=your-password
# optional, default 15
SNOW_TIMEOUT_SECONDS=15
```

## Running

Default is stdio (the MCP client launches the server as a subprocess; nothing to see):

```bash
uv run server.py
```

**Run as HTTP** (streamable HTTP on `http://0.0.0.0:8765/mcp` instead of stdio): `uv run server.py --http`, or set `SNOW_MCP_TRANSPORT=http`.

### Registering with an MCP client

Point the client at `uv` with the project directory so the `.venv` and `.env` are picked up:

```json
{
  "mcpServers": {
    "servicenow": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/servicenow-mcp", "run", "server.py"]
    }
  }
}
```

For Claude Code: `claude mcp add servicenow -- uv --directory /absolute/path/to/servicenow-mcp run server.py`.
For an HTTP client, start with `--http` and point it at `http://<host>:8765/mcp`.

Environment variables already set in the client's environment take precedence over `.env`.

## Tools

| Tool | Parameters | What it does | Returns |
|------|------------|--------------|---------|
| `create_incident` | `short_description: str` (required) · `description: str = ""` · `urgency: str = "3"` (`"1"` High, `"2"` Medium, `"3"` Low) | Creates a new incident. | `{number, sys_id, short_description, state, urgency}` |
| `query_incidents` | `query_text: str = ""` · `state: str = ""` · `limit: int = 5` (1–100) | Lists incidents newest first. `query_text` that looks like `INC0010001` matches the number exactly; other text runs ServiceNow keyword search (`123TEXTQUERY321`), falling back to a `LIKE` match on short_description/description so just-created records are found. `state` accepts a code (`"1"`…`"8"`) or a name (`New`, `In Progress`, `On Hold`, `Resolved`, `Closed`, `Canceled`). | `[{number, sys_id, short_description, state, state_code, urgency, created_on}, …]` |
| `update_incident` | `number: str` (required) · `work_notes: str = ""` · `state: str = ""` | Looks the incident up by number, appends a work note and/or sets the state. At least one of `work_notes`/`state` is required. | `{number, sys_id, changed: {…}, state}` |

### Errors

All tools raise a readable tool error (surfaced to the agent as `isError`) instead of a
stack trace, e.g.:

- `Authentication failed (401): check SNOW_USERNAME and SNOW_PASSWORD. ServiceNow said: User is not authenticated …`
- `Could not connect to https://…: [Errno 8] nodename nor servname provided … Check SNOW_INSTANCE_URL and your network connection.`
- `Timed out after 15s talking to https://… The instance may be hibernating (PDI) or unreachable.`
- `Incident 'INC9999999' was not found on https://….`
- `Forbidden (403): the user lacks permission … or the update was rejected by a business rule. ServiceNow said: …`
  (e.g. resolving an incident without resolution fields).

## Design notes

- **Query routing.** `query_incidents` picks one of three encoded queries: input matching
  `INC\d+` → exact `number=` match; anything else → ServiceNow full-text keyword search
  (`123TEXTQUERY321=…`, relevance-ranked, stemmed); if that returns nothing → `LIKE` on
  short_description/description. Two facts measured against the live instance forced this:
  `123TEXTQUERY321` **cannot be OR'd** with other clauses (the whole query silently returns
  nothing), and its index **lags writes by ~3–5 s**, so an agent that just created a ticket
  would not find it. The LIKE fallback sees fresh rows immediately; keyword search still
  wins for "what's open about printers?"-style lookups.
- **mcp 1.x / 2.x compat.** `mcp` 2.x renamed `FastMCP` → `MCPServer`
  (`mcp.server.mcpserver`); `server.py` tries the 2.x import first and falls back to
  `mcp.server.fastmcp.FastMCP`, so it runs on either major version.
- **Swappable client seam.** All network access goes through `ServiceNowClient` (one
  `httpx.AsyncClient`: basic auth, JSON headers, timeout; every failure becomes a
  `ServiceNowError` with a human-readable message). Tools obtain it via `get_client()`;
  tests call `set_client(fake)` to inject a mock without touching the network. Records are
  fetched with `sysparm_display_value=all`, so state/urgency labels come from the instance
  rather than a hard-coded map (friendly-name aliases are input-only).
- **Transport security.** HTTP mode binds `0.0.0.0:8765`. The `mcp` library only
  auto-enables DNS-rebinding protection for loopback binds, so on `0.0.0.0` any
  `Host`/`Origin` is accepted. Fine on a lab machine; pass a `transport_security=`
  (`TransportSecuritySettings` with `allowed_hosts`/`allowed_origins`) to
  `mcp.run(...)` before exposing it beyond localhost.

## Smoke test

Run against the live instance (writes one incident):

```bash
uv run python -c "
import asyncio, server
async def main():
    c = await server.create_incident('MCP server smoke test', 'created by smoke test')
    print(c)
    print(await server.query_incidents(query_text='MCP server smoke test'))
    print(await server.update_incident(c['number'], work_notes='hello from MCP'))
asyncio.run(main())
"
```
