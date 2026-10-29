"""Coordination helpers: a negotiation run by the conductor's ``concord`` flow.

The flow, in order:
  1. Create the agents and a conductor engine in the room
     (``engine create conductor --kind conductor``).
  2. File a task (``board new … --json``). A conductor run needs one: it walks
     the task's thread and holds that thread's floor for whoever a step asks.
  3. The agents start ``mycelium await --task <row>`` loops. That registers
     their presence (a ``coordination_join`` message per handle) and narrows
     their wake to the task's thread.
  4. Start the run: ``board coordinate <row> conductor "concord @~a @~b: <ask>"``.
     The conductor asks each step of the flow in the thread; the agents answer
     with ``respond --task <row>`` in the formats :mod:`libs.concord` describes.
  5. The run ends with a ``commit`` posted in the thread. The room's message
     listing shows it as an ``l9_commit`` message whose content is the
     serialized envelope, ``header.subkind`` being the outcome:
       converged → every member placed the pick at the bar or above
       resolved  → the fixes ran out and the best option found stands
       rejected  → nothing to pick from, or the run hit its step cap
     The payload carries ``memory`` (where the decision was saved, e.g.
     ``context/decision/<slug>``) and ``metrics.satisfaction`` /
     ``metrics.min_satisfaction``.

Terminal detection: poll room messages for an ``l9_commit`` in the task's
thread (the message's ``episode``), so a room that ran an earlier negotiation
doesn't answer with that one's outcome.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from libs import concord
from libs.mycelium_api import MyceliumAPI
from libs.mycelium_cli import MyceliumCLI

log = logging.getLogger(__name__)

# Commit subkinds a conductor run ends on.
SUBKIND_CONVERGED = "converged"
SUBKIND_RESOLVED = "resolved"
SUBKIND_REJECTED = "rejected"
TERMINAL_SUBKINDS = frozenset({SUBKIND_CONVERGED, SUBKIND_RESOLVED, SUBKIND_REJECTED})

#: The engine handle the suites run negotiations with, and its kind.
CONDUCTOR_HANDLE = "conductor"
CONDUCTOR_KIND = "conductor"

#: Who files tasks and starts runs in the suites.
ACTOR = "qa-tester"


# ── Message scanning ──────────────────────────────────────────────────────────


def _parse_l9_commit(message: dict) -> dict[str, Any] | None:
    """Return the parsed commit envelope if *message* is a terminal commit."""
    if message.get("message_type") != "l9_commit":
        return None
    content = message.get("content") or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = {"raw": content}
    l9 = payload.get("l9", {})
    subkind = l9.get("header", {}).get("subkind", "")
    return payload if subkind in TERMINAL_SUBKINDS else None


def commit_data(commit: dict[str, Any]) -> dict[str, Any]:
    """The commit's payload data: ``memory``, ``metrics``, ``select``, ``steps``…"""
    data = (commit.get("l9", {}).get("payload") or {}).get("data")
    return data if isinstance(data, dict) else {}


def _in_episode(message: dict, episode: str | None) -> bool:
    return episode is None or message.get("episode") == episode


def scan_for_terminal(messages: list[dict], episode: str | None = None) -> dict[str, Any] | None:
    """Return the first terminal commit found in *messages* (in *episode*, when
    given), or None."""
    for m in messages:
        if not _in_episode(m, episode):
            continue
        commit = _parse_l9_commit(m)
        if commit is not None:
            return commit
    return None


def is_converged(commit_payload: dict) -> bool:
    subkind = commit_payload.get("l9", {}).get("header", {}).get("subkind", "")
    return subkind == SUBKIND_CONVERGED


def terminal_result(commit: dict[str, Any]) -> dict[str, Any]:
    """What a terminal commit says, as the runners and testcases read it."""
    subkind = commit.get("l9", {}).get("header", {}).get("subkind", "")
    data = commit_data(commit)
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    return {
        "converged": subkind == SUBKIND_CONVERGED,
        "subkind": subkind,
        "memory": data.get("memory"),
        "satisfaction": metrics.get("satisfaction"),
        "min_satisfaction": metrics.get("min_satisfaction"),
        "steps": data.get("steps"),
        "commit": commit,
    }


def wait_for_coordination_join(
    api: MyceliumAPI,
    room: str,
    expected_handles: list[str],
    timeout: int = 15,
    poll_interval: float = 1.0,
) -> bool:
    """Poll room messages until all *expected_handles* have posted coordination_join."""
    deadline = time.time() + timeout
    joined_handles: set[str] = set()
    while time.time() < deadline:
        _, msgs = api.get_room_messages(room, limit=50)
        if isinstance(msgs, list):
            for m in msgs:
                if m.get("message_type") != "coordination_join":
                    continue
                try:
                    joined_handles.add(json.loads(m.get("content") or "{}").get("handle", ""))
                except json.JSONDecodeError:
                    pass
            if all(h in joined_handles for h in expected_handles):
                log.info("All agents joined: %s", expected_handles)
                return True
        time.sleep(poll_interval)
    missing = [h for h in expected_handles if h not in joined_handles]
    log.warning("Coordination join timeout — missing: %s", missing)
    return False


# ── Terminal state polling ────────────────────────────────────────────────────


def poll_for_terminal_state(
    api: MyceliumAPI,
    room: str,
    *,
    timeout: int = 600,
    poll_interval: int = 5,
    seen_message_ids: set[str] | None = None,
    episode: str | None = None,
) -> dict[str, Any] | None:
    """Poll room messages until a terminal commit appears (in *episode*, the
    task's thread, when given).

    Returns :func:`terminal_result`'s dict::

        {
            "converged": bool,
            "subkind": "converged" | "resolved" | "rejected",
            "memory": "context/decision/<slug>" | None,
            "satisfaction": {handle: 0-1} | None,
            "min_satisfaction": float | None,
            "steps": int | None,
            "commit": {...},   # the full commit envelope
        }

    Returns ``None`` on timeout.
    """
    if seen_message_ids is None:
        seen_message_ids = set()

    deadline = time.time() + timeout
    last_log = 0.0

    while time.time() < deadline:
        _, msgs = api.get_room_messages(room, limit=100)
        if isinstance(msgs, list):
            for m in msgs:
                mid = str(m.get("id") or m.get("message_id") or "")
                if mid and mid in seen_message_ids:
                    continue
                commit = _parse_l9_commit(m) if _in_episode(m, episode) else None
                if commit is not None:
                    return terminal_result(commit)
                if mid:
                    seen_message_ids.add(mid)

        now = time.time()
        if now - last_log >= 30:
            log.info(
                "poll_for_terminal_state: waiting on %s (%.0fs remaining)",
                room, deadline - now,
            )
            last_log = now
        time.sleep(poll_interval)

    log.warning("poll_for_terminal_state timed out after %ds for %s", timeout, room)
    return None


# ── Response tracking ─────────────────────────────────────────────────────────


@dataclass
class RoundSnapshot:
    """Per-handle response counts observed in the room."""

    responses: dict[str, int] = field(default_factory=dict)

    @property
    def all_responded(self) -> bool:
        return bool(self.responses) and all(v > 0 for v in self.responses.values())

    def total_responses(self) -> int:
        return sum(self.responses.values())


def collect_room_responses(
    api: MyceliumAPI,
    room: str,
    expected_handles: list[str],
    episode: str | None = None,
) -> RoundSnapshot:
    """Count ``broadcast`` messages per expected handle in *room* (in *episode*,
    when given)."""
    _, msgs = api.get_room_messages(room, limit=200)
    counts = {h: 0 for h in expected_handles}
    if isinstance(msgs, list):
        for m in msgs:
            if m.get("message_type") == "broadcast" and _in_episode(m, episode):
                handle = m.get("sender_handle") or ""
                if handle in counts:
                    counts[handle] += 1
    return RoundSnapshot(responses=counts)


# ── Setup helpers ─────────────────────────────────────────────────────────────


@dataclass
class CoordinationSetup:
    """Result of :func:`setup_coordination`: where the run will happen."""

    room: str
    engine_handle: str
    agent_handles: list[str]
    #: The task's memory key (``work/<slug>``): what ``--task`` and
    #: ``board coordinate`` take.
    task_key: str
    #: The task's thread (an episode URN): where the run's messages and its
    #: commit land.
    episode: str


def _task_from_json(stdout: str) -> tuple[str, str] | None:
    """``(key, episode)`` from ``board new --json``'s answer, or None."""
    try:
        task = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(task, dict):
        return None
    key = task.get("key")
    if not isinstance(key, str) or not key:
        return None
    episode = task.get("episode")
    return key, episode if isinstance(episode, str) else ""


def setup_coordination(
    api: MyceliumAPI,
    cli: MyceliumCLI,
    room: str,
    agent_handles: list[str],
    *,
    title: str,
    body: str = "",
    engine_handle: str = CONDUCTOR_HANDLE,
    adapter: str = "claude_code",
) -> CoordinationSetup | None:
    """Create the agents and the conductor, and file the task the run happens in.

    Does NOT start the run: start the agents' await loops first, then call
    :func:`start_concord`, so they're waiting in the thread when the first
    step is asked. Returns None when the task could not be filed.
    """
    for handle in agent_handles:
        r = cli.agent_create(handle, room, adapter=adapter)
        if not r.ok and "already" not in r.error_message.lower():
            log.warning("agent create for %s: %s", handle, r.error_message)

    r = cli.engine_create(engine_handle, room, kind=CONDUCTOR_KIND)
    if not r.ok and "already" not in r.error_message.lower():
        log.warning("engine create: %s", r.error_message)

    r = cli.board_new(room, title, body, actor=ACTOR)
    task = _task_from_json(r.stdout) if r.ok else None
    if task is None:
        log.error("board new failed: %s", r.error_message or r.stdout[:200])
        return None
    key, episode = task
    log.info("Filed %s (thread %s) in %s", key, episode, room)

    return CoordinationSetup(
        room=room,
        engine_handle=engine_handle,
        agent_handles=agent_handles,
        task_key=key,
        episode=episode,
    )


def start_concord(
    cli: MyceliumCLI,
    setup: CoordinationSetup,
    ask: str,
    *,
    handles: list[str] | None = None,
    bar: int | None = None,
    fixes: int | None = None,
) -> bool:
    """Summon the conductor's ``concord`` flow in the task's thread.

    ``handles`` are the members, in the order the run names them (default:
    the setup's agents). ``bar`` (a percentage) and ``fixes`` override the
    flow's own 60 and 5. Returns whether the summon was posted.
    """
    text = concord.summon(handles or setup.agent_handles, ask, bar=bar, fixes=fixes)
    r = cli.board_coordinate(setup.room, setup.task_key, setup.engine_handle, text, actor=ACTOR)
    if not r.ok:
        log.error("board coordinate failed: %s", r.error_message)
    return r.ok


# ── Debug helpers ─────────────────────────────────────────────────────────────


def collect_debug_info(
    api: MyceliumAPI,
    room: str,
    expected_handles: list[str],
    episode: str | None = None,
) -> dict[str, Any]:
    """Gather room messages and response counts for failure diagnostics."""
    debug: dict[str, Any] = {
        "room": room,
        "expected_handles": expected_handles,
        "episode": episode,
    }
    _, msgs = api.get_room_messages(room, limit=100)
    if isinstance(msgs, list):
        debug["recent_messages"] = [
            {
                "type": m.get("message_type"),
                "sender": m.get("sender_handle"),
                "preview": (m.get("content") or "")[:120],
            }
            for m in msgs[:20]
            if _in_episode(m, episode)
        ]
        snap = collect_room_responses(api, room, expected_handles, episode)
        debug["response_counts"] = snap.responses
    return debug
