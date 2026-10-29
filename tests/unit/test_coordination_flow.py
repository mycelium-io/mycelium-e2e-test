"""Unit tests for the coordination flow helpers: the conductor's commit, the
setup that files a task, and the stub driver's turn handling."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest

from libs import concord
from libs.coordination_flow import (
    SUBKIND_CONVERGED,
    SUBKIND_REJECTED,
    SUBKIND_RESOLVED,
    TERMINAL_SUBKINDS,
    CoordinationSetup,
    RoundSnapshot,
    _parse_l9_commit,
    collect_room_responses,
    poll_for_terminal_state,
    scan_for_terminal,
    setup_coordination,
    start_concord,
)
from libs.mycelium_cli import CLIResult, MyceliumCLI
from libs.stub_agent import CODE_SILENT, CODE_SKIPPED, StubAgent

THREAD = "urn:ioc:mycelium:episode:qa-room:t3aa11bb"
OTHER_THREAD = "urn:ioc:mycelium:episode:qa-room:t9ff00ee"


def _l9_commit_msg(subkind: str, *, episode: str | None = THREAD, data: dict | None = None) -> dict:
    """A commit as the room's message listing shows it: an ``l9_commit``
    message whose content is the serialized envelope (``persister.py``)."""
    msg = {
        "message_type": "l9_commit",
        "sender_handle": "conductor",
        "content": json.dumps({
            "l9": {
                "header": {"kind": "commit", "subkind": subkind},
                "payload": {"type": "outcome", "data": data or {}},
            },
            "content": "✓ Everyone meets the bar",
        }),
    }
    if episode is not None:
        msg["episode"] = episode
    return msg


def _ok(stdout: str = "", rc: int = 0) -> CLIResult:
    return CLIResult(rc, stdout, "", 1, ["mycelium"])


# ── _parse_l9_commit ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("subkind", ["converged", "resolved", "rejected"])
def test_parse_terminal_subkinds(subkind: str) -> None:
    result = _parse_l9_commit(_l9_commit_msg(subkind))
    assert result is not None
    assert result["l9"]["header"]["subkind"] == subkind


def test_parse_non_terminal_subkind_returns_none() -> None:
    msg = _l9_commit_msg("in_progress")
    assert _parse_l9_commit(msg) is None


def test_parse_wrong_message_type_returns_none() -> None:
    msg = {"message_type": "broadcast", "content": "{}"}
    assert _parse_l9_commit(msg) is None


def test_parse_invalid_json_returns_raw() -> None:
    msg = {"message_type": "l9_commit", "content": "not-json-but-converged"}
    # Non-terminal (can't parse subkind) → None
    assert _parse_l9_commit(msg) is None


# ── scan_for_terminal ─────────────────────────────────────────────────────────


def test_scan_finds_converged() -> None:
    msgs = [
        {"message_type": "broadcast", "content": "hello"},
        _l9_commit_msg("converged"),
    ]
    result = scan_for_terminal(msgs)
    assert result is not None
    assert result["l9"]["header"]["subkind"] == "converged"


def test_scan_finds_resolved() -> None:
    assert scan_for_terminal([_l9_commit_msg("resolved")]) is not None


def test_scan_returns_none_when_absent() -> None:
    msgs = [{"message_type": "broadcast", "content": "nothing here"}]
    assert scan_for_terminal(msgs) is None


def test_scan_empty_list() -> None:
    assert scan_for_terminal([]) is None


def test_scan_only_reads_the_given_thread() -> None:
    msgs = [_l9_commit_msg("rejected", episode=OTHER_THREAD)]
    assert scan_for_terminal(msgs, episode=THREAD) is None
    assert scan_for_terminal(msgs) is not None


# ── Terminal subkind constants ────────────────────────────────────────────────


def test_terminal_subkinds_are_the_conductors_three_ends() -> None:
    assert TERMINAL_SUBKINDS == {SUBKIND_CONVERGED, SUBKIND_RESOLVED, SUBKIND_REJECTED}


# ── RoundSnapshot ─────────────────────────────────────────────────────────────


def test_round_snapshot_all_responded_true() -> None:
    snap = RoundSnapshot(responses={"stub-a": 2, "stub-b": 1})
    assert snap.all_responded is True


def test_round_snapshot_all_responded_false_when_zero() -> None:
    snap = RoundSnapshot(responses={"stub-a": 1, "stub-b": 0})
    assert snap.all_responded is False


def test_round_snapshot_all_responded_false_when_empty() -> None:
    assert RoundSnapshot(responses={}).all_responded is False


def test_round_snapshot_total() -> None:
    snap = RoundSnapshot(responses={"a": 3, "b": 2})
    assert snap.total_responses() == 5


# ── collect_room_responses ────────────────────────────────────────────────────


def test_collect_counts_broadcast_per_handle() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        {"message_type": "broadcast", "sender_handle": "stub-a", "content": "hi"},
        {"message_type": "broadcast", "sender_handle": "stub-a", "content": "hi2"},
        {"message_type": "broadcast", "sender_handle": "stub-b", "content": "yo"},
        _l9_commit_msg("converged"),
    ])
    snap = collect_room_responses(api, "qa-room", ["stub-a", "stub-b"])
    assert snap.responses == {"stub-a": 2, "stub-b": 1}


def test_collect_ignores_non_broadcast() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        {"message_type": "coordination_join", "sender_handle": "stub-a"},
    ])
    snap = collect_room_responses(api, "qa-room", ["stub-a"])
    assert snap.responses == {"stub-a": 0}


def test_collect_counts_only_the_given_thread() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        {"message_type": "broadcast", "sender_handle": "stub-a", "episode": THREAD},
        {"message_type": "broadcast", "sender_handle": "stub-a", "episode": OTHER_THREAD},
    ])
    snap = collect_room_responses(api, "qa-room", ["stub-a"], episode=THREAD)
    assert snap.responses == {"stub-a": 1}


# ── poll_for_terminal_state ───────────────────────────────────────────────────


def test_poll_returns_on_converged_message_with_what_the_commit_carries() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        _l9_commit_msg("converged", data={
            "memory": "context/decision/audit-log-retention",
            "metrics": {"satisfaction": {"stub-a": 0.95, "stub-b": 0.7}, "min_satisfaction": 0.7},
            "steps": 3,
        }),
    ])
    result = poll_for_terminal_state(api, "qa-room", timeout=5, poll_interval=1, episode=THREAD)
    assert result is not None
    assert result["converged"] is True
    assert result["subkind"] == "converged"
    assert result["memory"] == "context/decision/audit-log-retention"
    assert result["min_satisfaction"] == 0.7
    assert result["satisfaction"] == {"stub-a": 0.95, "stub-b": 0.7}
    assert result["steps"] == 3


def test_poll_returns_on_resolved_message() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        _l9_commit_msg("resolved", data={"metrics": {"min_satisfaction": 0.1}}),
    ])
    result = poll_for_terminal_state(api, "qa-room", timeout=5, poll_interval=1)
    assert result is not None
    assert result["converged"] is False
    assert result["subkind"] == "resolved"
    assert result["min_satisfaction"] == 0.1
    assert result["memory"] is None


def test_poll_returns_none_on_timeout() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [
        {"message_type": "broadcast", "content": "nothing terminal"},
    ])
    result = poll_for_terminal_state(api, "qa-room", timeout=1, poll_interval=1)
    assert result is None


def test_poll_ignores_already_seen_messages() -> None:
    """If all messages are in seen_ids, should not return them."""
    api = MagicMock()
    msg = {**_l9_commit_msg("converged"), "id": "msg-1"}
    api.get_room_messages.return_value = (200, [msg])
    seen = {"msg-1"}
    result = poll_for_terminal_state(
        api, "qa-room", timeout=1, poll_interval=1, seen_message_ids=seen
    )
    assert result is None


def test_poll_ignores_an_earlier_runs_commit_in_another_thread() -> None:
    api = MagicMock()
    api.get_room_messages.return_value = (200, [_l9_commit_msg("converged", episode=OTHER_THREAD)])
    result = poll_for_terminal_state(api, "qa-room", timeout=1, poll_interval=1, episode=THREAD)
    assert result is None


# ── setup_coordination / start_concord ────────────────────────────────────────


def _cli_filing(stdout: str, rc: int = 0) -> MagicMock:
    cli = MagicMock(spec=MyceliumCLI)
    cli.agent_create.return_value = _ok()
    cli.engine_create.return_value = _ok()
    cli.board_new.return_value = _ok(stdout, rc)
    cli.board_coordinate.return_value = _ok()
    return cli


def test_setup_creates_a_conductor_and_files_the_task() -> None:
    cli = _cli_filing(json.dumps({"key": "work/decide-retention", "episode": THREAD}))
    setup = setup_coordination(MagicMock(), cli, "qa-room", ["stub-a", "stub-b"], title="Decide retention")
    assert setup == CoordinationSetup(
        room="qa-room",
        engine_handle="conductor",
        agent_handles=["stub-a", "stub-b"],
        task_key="work/decide-retention",
        episode=THREAD,
    )
    cli.engine_create.assert_called_once_with("conductor", "qa-room", kind="conductor")
    assert [c.args[0] for c in cli.agent_create.call_args_list] == ["stub-a", "stub-b"]
    assert cli.board_new.call_args.args[:2] == ("qa-room", "Decide retention")


def test_setup_is_none_when_the_task_cannot_be_filed() -> None:
    assert setup_coordination(MagicMock(), _cli_filing("", rc=1), "qa-room", ["a"], title="T") is None
    assert setup_coordination(MagicMock(), _cli_filing("not json"), "qa-room", ["a"], title="T") is None


def test_start_concord_summons_the_conductor_in_the_task() -> None:
    cli = _cli_filing("")
    setup = CoordinationSetup("qa-room", "conductor", ["stub-a", "stub-b"], "work/decide-retention", THREAD)
    assert start_concord(cli, setup, "decide retention", fixes=2)
    room, row, engine, ask = cli.board_coordinate.call_args.args
    assert (room, row, engine) == ("qa-room", "work/decide-retention", "conductor")
    assert ask == "concord @~stub-a @~stub-b: decide retention fixes=2"


# ── MyceliumCLI ───────────────────────────────────────────────────────────────


def test_engine_create_takes_a_kind() -> None:
    cli = MyceliumCLI()
    with pytest.raises(TypeError):
        cli.engine_create("conductor", "qa-room")  # type: ignore[call-arg]


def test_cli_scopes_await_and_respond_to_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    cli = MyceliumCLI()
    monkeypatch.setattr(cli, "run", lambda *a, **kw: calls.append(a) or _ok())
    cli.await_turn("qa-room", "stub-a", timeout=5, task="work/x")
    cli.respond("qa-room", "stub-a", "bad: 0 - why", task="work/x")
    cli.board_new("qa-room", "Title", "Body", actor="qa-tester")
    cli.board_coordinate("qa-room", "work/x", "conductor", "concord @~a: q", actor="qa-tester")
    assert calls[0][-2:] == ("--task", "work/x")
    assert calls[1] == ("respond", "--room", "qa-room", "--handle", "stub-a", "--task", "work/x", "bad: 0 - why")
    assert calls[2] == ("board", "new", "Title", "Body", "--room", "qa-room", "--as", "qa-tester", "--json")
    assert calls[3] == (
        "board", "coordinate", "work/x", "conductor", "concord @~a: q", "--room", "qa-room", "--as", "qa-tester",
    )


# ── StubAgent ─────────────────────────────────────────────────────────────────


SCORE = (
    "concord · score · turn 3 of 20 · stub-a\n\nThe options for T:\n\n"
    "A. Keep audit logs for 30 days, then delete them.\n\n"
    "A (Keep audit logs for 30…): <0-100> - <why>\n"
)


def _stub(await_result: CLIResult) -> tuple[StubAgent, MagicMock]:
    cli = MagicMock(spec=MyceliumCLI)
    cli.await_turn.return_value = await_result
    cli.respond.return_value = _ok()
    persona = concord.Persona(proposal="P", why="W", otherwise=(70, "fine"))
    return StubAgent("qa-room", "stub-a", persona, cli=cli), cli


def test_stub_answers_a_step_in_the_tasks_thread() -> None:
    stub, cli = _stub(_ok(json.dumps({"prompt": SCORE, "task": "work/x"})))
    turn = stub.await_and_respond(0, 5, threading.Event(), task="work/x")
    assert turn is not None and turn.ok and turn.step == "score"
    cli.await_turn.assert_called_once_with("qa-room", "stub-a", timeout=5, task="work/x")
    cli.respond.assert_called_once_with(
        "qa-room", "stub-a", "A (Keep audit logs for 30…): 70 - fine", task="work/x"
    )


def test_stub_with_nothing_asked_records_no_turn() -> None:
    idle = _ok(json.dumps({"room": "qa-room", "handle": "stub-a", "message": None}), rc=1)
    stub, cli = _stub(idle)
    assert stub.await_and_respond(0, 5, threading.Event(), task="work/x") is None
    cli.respond.assert_not_called()


def test_stub_does_not_answer_a_message_that_is_no_step() -> None:
    stub, cli = _stub(_ok(json.dumps({"prompt": "Running concord with stub-a."})))
    turn = stub.await_and_respond(0, 5, threading.Event(), task="work/x")
    assert turn is not None and turn.ok and turn.code == CODE_SKIPPED
    cli.respond.assert_not_called()


def test_stub_await_error_is_a_silent_turn() -> None:
    stub, _cli = _stub(CLIResult(1, "", "hub unreachable", 1, ["mycelium"]))
    turn = stub.await_and_respond(0, 5, threading.Event(), task="work/x")
    assert turn is not None and not turn.ok and turn.code == CODE_SILENT
