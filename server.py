"""ServiceNow MCP server.

Exposes three tools (create_incident, query_incidents, update_incident) backed by
the ServiceNow Table API. Runs over stdio so any MCP client (Claude Desktop,
Claude Code, OpenHands, ...) can launch it as a subprocess.

Configuration comes from environment variables, loaded from a `.env` file next
to this file if present:

    SNOW_INSTANCE_URL=https://devXXXXX.service-now.com
    SNOW_USERNAME=admin
    SNOW_PASSWORD=...
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# The high-level decorator server is called FastMCP in mcp 1.x and MCPServer in
# mcp 2.x. Support both so the file runs on either major version.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

# Load .env sitting next to server.py first (MCP clients often launch us with an
# arbitrary cwd), then fall back to python-dotenv's default search. Existing
# environment variables are never overridden.
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv()

INCIDENT_TABLE = "incident"
INCIDENT_NUMBER_RE = re.compile(r"^INC\d+$", re.IGNORECASE)
DEFAULT_TIMEOUT_SECONDS = 15.0
INCIDENT_FIELDS = "sys_id,number,short_description,description,state,urgency,sys_created_on"

# Standard out-of-the-box incident state codes. Instances can customise these,
# so they're used only to accept friendly names as input; output labels always
# come from the instance itself (sysparm_display_value=all).
INCIDENT_STATE_ALIASES: dict[str, str] = {
    "new": "1",
    "in progress": "2",
    "in_progress": "2",
    "on hold": "3",
    "on_hold": "3",
    "resolved": "6",
    "closed": "7",
    "canceled": "8",
    "cancelled": "8",
}


class ServiceNowError(Exception):
    """A readable, user-facing error raised by ServiceNowClient."""


class ServiceNowClient:
    """Thin async wrapper around the ServiceNow Table API using httpx.

    All network access goes through this class so tests can swap in a fake
    (see `set_client`). Every public method returns plain dicts/lists and raises
    `ServiceNowError` with a human-readable message on any failure (bad
    credentials, missing record, network problems, ServiceNow-side errors).
    """

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not instance_url:
            raise ServiceNowError("SNOW_INSTANCE_URL is not set")
        if not username or not password:
            raise ServiceNowError("SNOW_USERNAME and SNOW_PASSWORD must both be set")
        self.instance_url = instance_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{self.instance_url}/api/now",
            auth=(username, password),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout),
        )

    @classmethod
    def from_env(cls) -> ServiceNowClient:
        """Build a client from SNOW_INSTANCE_URL / SNOW_USERNAME / SNOW_PASSWORD."""
        return cls(
            instance_url=os.getenv("SNOW_INSTANCE_URL", ""),
            username=os.getenv("SNOW_USERNAME", ""),
            password=os.getenv("SNOW_PASSWORD", ""),
            timeout=float(os.getenv("SNOW_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- Table API operations -------------------------------------------------

    async def create_record(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        """POST a new record; returns the created record (display+raw values)."""
        data = await self._request(
            "POST",
            f"/table/{table}",
            json=fields,
            params={"sysparm_display_value": "all", "sysparm_fields": INCIDENT_FIELDS},
        )
        return data["result"]

    async def query_records(
        self, table: str, query: str, limit: int, fields: str = INCIDENT_FIELDS
    ) -> list[dict[str, Any]]:
        """GET records matching an encoded query string."""
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_fields": fields,
            "sysparm_display_value": "all",
        }
        if query:
            params["sysparm_query"] = query
        data = await self._request("GET", f"/table/{table}", params=params)
        return data["result"]

    async def get_record_by_number(self, table: str, number: str) -> dict[str, Any] | None:
        """Return the single record with this `number`, or None if there isn't one."""
        results = await self.query_records(table, f"number={number}", limit=1)
        return results[0] if results else None

    async def update_record(self, table: str, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """PATCH an existing record by sys_id; returns the updated record."""
        data = await self._request(
            "PATCH",
            f"/table/{table}/{sys_id}",
            json=fields,
            params={"sysparm_display_value": "all", "sysparm_fields": INCIDENT_FIELDS},
        )
        return data["result"]

    # ---- plumbing --------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ServiceNowError(
                f"Timed out after {self._http.timeout.read}s talking to {self.instance_url}. "
                "The instance may be hibernating (PDI) or unreachable."
            ) from exc
        except httpx.ConnectError as exc:
            raise ServiceNowError(
                f"Could not connect to {self.instance_url}: {exc}. "
                "Check SNOW_INSTANCE_URL and your network connection."
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceNowError(f"HTTP error talking to {self.instance_url}: {exc}") from exc

        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise ServiceNowError(
                    f"ServiceNow returned a non-JSON response ({response.status_code}) for "
                    f"{method} {path}. Is SNOW_INSTANCE_URL correct?"
                ) from exc

        raise ServiceNowError(self._describe_http_error(response, method, path))

    @staticmethod
    def _describe_http_error(response: httpx.Response, method: str, path: str) -> str:
        detail = ""
        try:
            err = response.json().get("error", {})
            detail = " ".join(p for p in (err.get("message"), err.get("detail")) if p)
        except ValueError:
            detail = response.text[:200]
        suffix = f" ServiceNow said: {detail}" if detail else ""

        status = response.status_code
        if status == 401:
            return "Authentication failed (401): check SNOW_USERNAME and SNOW_PASSWORD." + suffix
        if status == 403:
            return (
                "Forbidden (403): the user lacks permission for this operation "
                "(ACL/role), or the update was rejected by a business rule." + suffix
            )
        if status == 404:
            return f"Not found (404): {method} {path} does not exist on this instance." + suffix
        if status == 400:
            return f"Bad request (400) for {method} {path}." + suffix
        if status >= 500:
            return f"ServiceNow server error ({status}) for {method} {path}." + suffix
        return f"ServiceNow request failed ({status}) for {method} {path}." + suffix


# ---- client wiring -----------------------------------------------------------

_client: ServiceNowClient | None = None


def get_client() -> ServiceNowClient:
    """Return the shared client, creating it from the environment on first use."""
    global _client
    if _client is None:
        _client = ServiceNowClient.from_env()
    return _client


def set_client(client: ServiceNowClient | None) -> None:
    """Replace the shared client (used by tests to inject a fake)."""
    global _client
    _client = client


# ---- helpers ---------------------------------------------------------------------


def _field(record: dict[str, Any], name: str, *, display: bool = False) -> str:
    """Read a field from a `sysparm_display_value=all` record (or a plain one)."""
    value = record.get(name, "")
    if isinstance(value, dict):
        return str(value.get("display_value" if display else "value", "") or "")
    return str(value or "")


def _normalize_state(state: str) -> str:
    """Accept a numeric state code or a friendly name ("In Progress") and return the code."""
    state = state.strip()
    if not state:
        return ""
    if state.isdigit():
        return state
    code = INCIDENT_STATE_ALIASES.get(state.lower())
    if code is None:
        options = ", ".join(sorted({f'"{k}"' for k in INCIDENT_STATE_ALIASES}))
        raise ToolError(f"Unknown incident state {state!r}. Use a numeric code or one of: {options}")
    return code


def _summarize(record: dict[str, Any]) -> dict[str, str]:
    return {
        "number": _field(record, "number"),
        "sys_id": _field(record, "sys_id"),
        "short_description": _field(record, "short_description"),
        "state": _field(record, "state", display=True),
        "state_code": _field(record, "state"),
        "urgency": _field(record, "urgency", display=True),
        "created_on": _field(record, "sys_created_on"),
    }


# ---- MCP server ------------------------------------------------------------------

mcp = FastMCP(
    "servicenow",
    instructions=(
        "Tools for working with incidents in a ServiceNow instance via the Table API. "
        "Use query_incidents to look things up, create_incident to open a new ticket, and "
        "update_incident to add work notes or change state on an existing ticket."
    ),
)

# httpx logs every request at INFO; keep stderr quiet unless something is wrong.
logging.getLogger("httpx").setLevel(logging.WARNING)


@mcp.tool()
async def create_incident(
    short_description: str,
    description: str = "",
    urgency: str = "3",
) -> dict[str, str]:
    """Create a new ServiceNow incident and return its number and sys_id.

    Use this when a user reports a problem that should be tracked as a ticket.

    Args:
        short_description: One-line summary of the issue (required, shown in lists).
        description: Longer free-text details, steps to reproduce, impact, etc.
        urgency: "1" (High), "2" (Medium) or "3" (Low). Defaults to "3".

    Returns:
        {"number": "INC0010001", "sys_id": "...", "short_description": ..., "state": ..., "urgency": ...}
        The `number` is what humans reference; `sys_id` is the stable record id.
    """
    short_description = short_description.strip()
    if not short_description:
        raise ToolError("short_description is required and cannot be empty.")
    if urgency not in {"1", "2", "3"}:
        raise ToolError('urgency must be "1" (High), "2" (Medium) or "3" (Low).')

    fields: dict[str, Any] = {"short_description": short_description, "urgency": urgency}
    if description:
        fields["description"] = description

    try:
        record = await get_client().create_record(INCIDENT_TABLE, fields)
    except ServiceNowError as exc:
        raise ToolError(f"create_incident failed: {exc}") from exc

    summary = _summarize(record)
    return {
        "number": summary["number"],
        "sys_id": summary["sys_id"],
        "short_description": summary["short_description"],
        "state": summary["state"],
        "urgency": summary["urgency"],
    }


@mcp.tool()
async def query_incidents(
    query_text: str = "",
    state: str = "",
    limit: int = 5,
) -> list[dict[str, str]]:
    """List ServiceNow incidents, newest first, optionally filtered by text and/or state.

    Use this to find existing tickets before creating a new one, to check on a
    ticket by number, or to answer "what incidents are open about X?".

    Args:
        query_text: What to search for. An incident number ("INC0010001") matches
            exactly; anything else runs ServiceNow's keyword search over the
            incident's text fields, falling back to a substring match on
            short_description/description. Leave empty to list the most recent incidents.
        state: Filter by state. Accepts a numeric code ("1".."8") or a name such as
            "New", "In Progress", "On Hold", "Resolved", "Closed", "Canceled".
            Leave empty for any state.
        limit: Maximum number of incidents to return (1-100). Defaults to 5.

    Returns:
        A list of {"number", "sys_id", "short_description", "state", "state_code",
        "urgency", "created_on"}; an empty list means nothing matched.
    """
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")

    text = query_text.strip()
    filters: list[str] = []
    state_code = _normalize_state(state)
    if state_code:
        filters.append(f"state={state_code}")
    filters.append("ORDERBYDESCsys_created_on")

    # Build the candidate sysparm_query strings, tried in order until one matches.
    if not text:
        queries = ["^".join(filters)]
    elif INCIDENT_NUMBER_RE.match(text):
        queries = ["^".join([f"number={text.upper()}", *filters])]
    else:
        # 1) ServiceNow full-text keyword search (relevance-ranked, stemmed, all
        #    indexed fields). Its index lags writes by a few seconds, so
        # 2) fall back to a plain substring match, which sees fresh records.
        # Note: 123TEXTQUERY321 cannot be OR'd with other clauses, hence two queries.
        queries = [
            "^".join([f"123TEXTQUERY321={text}", *filters]),
            "^".join([f"short_descriptionLIKE{text}^ORdescriptionLIKE{text}", *filters]),
        ]

    client = get_client()
    try:
        records: list[dict[str, Any]] = []
        for query in queries:
            records = await client.query_records(INCIDENT_TABLE, query, limit=limit)
            if records:
                break
    except ServiceNowError as exc:
        raise ToolError(f"query_incidents failed: {exc}") from exc

    return [_summarize(r) for r in records]


@mcp.tool()
async def update_incident(
    number: str,
    work_notes: str = "",
    state: str = "",
) -> dict[str, Any]:
    """Update an existing incident by number: append a work note and/or change its state.

    Use this to record progress on a ticket or move it through its lifecycle.
    At least one of `work_notes` or `state` must be provided.

    Args:
        number: The incident number, e.g. "INC0010001".
        work_notes: Text to append to the (internal) work notes journal.
        state: New state as a numeric code ("1".."8") or a name such as "In Progress",
            "On Hold", "Resolved", "Closed". Note: resolving/closing on most instances
            also requires resolution fields and will be rejected by ServiceNow if missing.

    Returns:
        {"number", "sys_id", "changed": {field: new value, ...}, "state": <current state>}
        describing exactly what was applied.
    """
    number = number.strip()
    if not number:
        raise ToolError("number is required, e.g. 'INC0010001'.")

    fields: dict[str, Any] = {}
    if work_notes.strip():
        fields["work_notes"] = work_notes
    state_code = _normalize_state(state)
    if state_code:
        fields["state"] = state_code
    if not fields:
        raise ToolError("Nothing to update: provide work_notes and/or state.")

    client = get_client()
    try:
        existing = await client.get_record_by_number(INCIDENT_TABLE, number)
        if existing is None:
            raise ToolError(f"Incident {number!r} was not found on {client.instance_url}.")
        updated = await client.update_record(INCIDENT_TABLE, _field(existing, "sys_id"), fields)
    except ServiceNowError as exc:
        raise ToolError(f"update_incident failed for {number}: {exc}") from exc

    changed: dict[str, str] = {}
    if "work_notes" in fields:
        changed["work_notes"] = fields["work_notes"]
    if "state" in fields:
        changed["state"] = f"{_field(existing, 'state', display=True)} -> {_field(updated, 'state', display=True)}"

    return {
        "number": _field(updated, "number"),
        "sys_id": _field(updated, "sys_id"),
        "changed": changed,
        "state": _field(updated, "state", display=True),
    }


HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8765


if __name__ == "__main__":
    import sys

    # Default: stdio. `--http` flag or SNOW_MCP_TRANSPORT=http selects streamable HTTP
    # (endpoint: http://<host>:8765/mcp).
    use_http = "--http" in sys.argv[1:] or os.getenv("SNOW_MCP_TRANSPORT", "").lower() == "http"
    if use_http:
        mcp.run(transport="streamable-http", host=HTTP_HOST, port=HTTP_PORT)
    else:
        mcp.run(transport="stdio")
