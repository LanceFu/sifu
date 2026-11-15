import pytest

pytest.importorskip("fastmcp")

from sifu.tools import ToolGateway, ToolNotAllowed, build_server  # noqa: E402


@pytest.fixture()
def gateway(conn, ctx):
    events = []
    server = build_server(conn, ctx)
    return ToolGateway(server, lambda kind, payload: events.append((kind, payload))), events


def test_supervisor_reads_a_ticket_through_mcp(conn, make_ticket, gateway):
    gw, events = gateway
    ticket = make_ticket("Title", "Body")
    data = gw.call("supervisor", "get_ticket", ticket_id=ticket)
    assert data["title"] == "Title" and data["status"] == "intent"
    assert events[-1][0] == "tool_call"


def test_glossary_tool_returns_terms(gateway):
    gw, _ = gateway
    data = gw.call("supervisor", "list_glossary_terms")
    assert data["terms"][0]["term"] == "Traveller" and "Booking" in data["out_of_scope"]


def test_record_decision_tool_writes_the_decisions_table(conn, make_ticket, gateway):
    gw, _ = gateway
    ticket = make_ticket("T", "B")
    decision_id = gw.call(
        "supervisor",
        "record_decision",
        ticket_id=ticket,
        decision_type="accept_spec",
        final_tier=1,
        floor_tier=0,
        rules_fired=["assessor_disagreement"],
        assessments=[{"assessor": "m", "tier": 1, "confidence": 0.8, "rationale": "r"}],
    )
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    assert (row["final_tier"], row["resolution"]) == (1, "auto_with_veto")


def test_actors_without_the_tool_are_refused_and_logged(gateway):
    gw, events = gateway
    for actor in ("reviewer", "assessor", "stranger"):
        with pytest.raises(ToolNotAllowed):
            gw.call(actor, "get_ticket", ticket_id=1)
    assert [k for k, _ in events] == ["tool_denied"] * 3


def test_unknown_tool_is_refused(gateway):
    gw, _ = gateway
    with pytest.raises(ToolNotAllowed):
        gw.call("supervisor", "delete_everything")
