# servicenow-mcp

A small [MCP](https://modelcontextprotocol.io) server that lets AI agents create, search and
update **ServiceNow incidents** through the Table API. Built with the official Python
`mcp` package (FastMCP-style decorators), `httpx` and `python-dotenv`. Runs over **stdio**.

Everything lives in a single file: [`server.py`](server.py).

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

Run the server (it speaks MCP over stdin/stdout, so there is nothing to see — an MCP client
must launch it):

```bash
uv run server.py
```

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

Environment variables already set in the client's environment take precedence over `.env`.

## Tools

| Tool | Parameters | What it does | Returns |
|------|------------|--------------|---------|
| `create_incident` | `short_description: str` (required) · `description: str = ""` · `urgency: str = "3"` (`"1"` High, `"2"` Medium, `"3"` Low) | Creates a new incident. | `{number, sys_id, short_description, state, urgency}` |
| `query_incidents` | `query_text: str = ""` · `state: str = ""` · `limit: int = 5` (1–100) | Lists incidents newest first. `query_text` that looks like `INC0010001` matches the number exactly; other text runs ServiceNow keyword search (`123TEXTQUERY321`), falling back to a `LIKE` match on short_description/description so just-created records are found. `state` accepts a code (`"1"`…`"8"`) or a name (`New`, `In Progress`, `On Hold`, `Resolved`, `Closed`, `Canceled`). | `[{number, sys_id, short_description, state, state_code, urgency, created_on}, …]` |
| `update_incident` | `number: str` (required) · `work_notes: str = ""` · `state: str = ""` | Looks the incident up by number, appends a work note and/or sets the state. At least one of `work_notes`/`state` is required. | `{number, sys_id, changed: {…}, state}` |

All tools raise a readable tool error (surfaced to the agent as `isError`) instead of a
stack trace, e.g.:

- `Authentication failed (401): check SNOW_USERNAME and SNOW_PASSWORD. ServiceNow said: User is not authenticated …`
- `Could not connect to https://…: [Errno 8] nodename nor servname provided … Check SNOW_INSTANCE_URL and your network connection.`
- `Timed out after 15s talking to https://… The instance may be hibernating (PDI) or unreachable.`
- `Incident 'INC9999999' was not found on https://….`
- `Forbidden (403): the user lacks permission … or the update was rejected by a business rule. ServiceNow said: …`
  (e.g. resolving an incident without resolution fields).

## Design notes

- **`ServiceNowClient`** wraps a single `httpx.AsyncClient` (basic auth, JSON headers,
  timeout) and exposes `create_record / query_records / get_record_by_number / update_record`.
  All errors are translated to `ServiceNowError` with a human-readable message.
- The tools get the client through `get_client()`; call `set_client(fake)` in tests to swap
  in a mock without touching the network.
- Records are fetched with `sysparm_display_value=all`, so state/urgency labels come from
  the instance itself rather than a hard-coded map (the friendly-name aliases are input-only).
- `mcp` 2.x renamed `FastMCP` to `MCPServer`; `server.py` imports whichever exists so it runs
  on both 1.x and 2.x.

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
