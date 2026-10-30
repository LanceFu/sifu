"""Sifu's FastMCP tool server and the allow-listed client agents use to call it.

Every call goes through ``ToolGateway.call``. It refuses tools that are not on the caller's
allow-list and writes a ``tool_call`` event for each attempt, allowed or not.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp import Client, FastMCP

from sifu import db
from sifu.context import ProductContext
from sifu.models import DecisionResult, DecisionType, RiskAssessment, RiskTier

ALLOWLIST: dict[str, frozenset[str]] = {
    # The supervisor reads state and records router decisions. Reviewers and assessors get no
    # tools: they see only the prompt, so injected text has nothing to call.
    "supervisor": frozenset({"get_ticket", "list_glossary_terms", "record_decision"}),
    "reviewer": frozenset(),
    "assessor": frozenset(),
    "implementer": frozenset(),
}


class ToolNotAllowed(PermissionError):
    """Raised when an actor calls a tool that is not on its allow-list."""


def build_server(
    conn: sqlite3.Connection, ctx: ProductContext, lock: threading.RLock | None = None
) -> FastMCP:
    """Create the MCP server exposing three tools over the app database and the glossary."""
    guard = lock or threading.RLock()
    server = FastMCP("sifu")

    @server.tool
    def get_ticket(ticket_id: int) -> dict[str, Any]:
        """Return a ticket's id, title, body and status."""
        with guard:
            row = db.get_ticket(conn, ticket_id)
        return {k: row[k] for k in ("id", "title", "body", "status")}

    @server.tool
    def list_glossary_terms() -> dict[str, Any]:
        """Return the glossary: preferred terms, words to avoid, out-of-scope features."""
        return json.loads(ctx.glossary.model_dump_json())

    @server.tool
    def record_decision(
        ticket_id: int,
        decision_type: str,
        final_tier: int,
        floor_tier: int,
        rules_fired: list[str],
        assessments: list[dict[str, Any]],
    ) -> int:
        """Store a router decision with the rules that fired and every assessor's opinion."""
        result = DecisionResult(
            final_tier=RiskTier(final_tier),
            floor_tier=RiskTier(floor_tier),
            rules_fired=rules_fired,
            assessor_tiers=[RiskTier(a["tier"]) for a in assessments],
        )
        with guard:
            return db.record_decision(
                conn,
                ticket_id,
                DecisionType(decision_type),
                result,
                [RiskAssessment.model_validate(a) for a in assessments],
            )

    return server


class ToolGateway:
    """Synchronous, allow-listed access to the MCP server for LangGraph nodes."""

    def __init__(
        self,
        server: FastMCP,
        log: Callable[[str, dict[str, Any]], None],
        allowlist: Mapping[str, frozenset[str]] = ALLOWLIST,
    ) -> None:
        self.server = server
        self.log = log
        self.allowlist = allowlist

    def call(
        self, actor: str, tool: str, *, for_ticket: int | None = None, **arguments: Any
    ) -> Any:
        """Call a tool as ``actor``. ``for_ticket`` only tags the log entry."""
        meta = {"actor": actor, "tool": tool, "ticket_id": for_ticket}
        if tool not in self.allowlist.get(actor, frozenset()):
            self.log("tool_denied", meta)
            raise ToolNotAllowed(f"{actor} may not call {tool}")
        # A fresh thread gives asyncio.run its own loop, even inside a notebook's running loop.
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(asyncio.run, self._call(tool, arguments)).result()
        self.log("tool_call", {**meta, "arguments": _short(arguments)})
        return result

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        async with Client(self.server) as client:
            return (await client.call_tool(tool, arguments)).data


def _short(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (v if isinstance(v, int | float | bool) else str(v)[:80]) for k, v in arguments.items()
    }
