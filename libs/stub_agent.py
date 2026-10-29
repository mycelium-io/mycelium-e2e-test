"""Stub agent driver for nightly coordination tests.

A stub answers the conductor's ``concord`` steps from a scripted
:class:`~libs.concord.Persona`, with no LLM:
  1. ``setup_coordination`` creates the agents and the conductor and files
     the task the run happens in.
  2. Stub threads start ``mycelium await --task <row>``, which registers
     their presence (``coordination_join``) and wakes them only on the
     task's thread.
  3. The main thread waits briefly for every join, then starts the run with
     ``board coordinate <row> conductor "concord @~a @~b: <ask>"``.
  4. Each turn's prompt names its step (``concord · score · turn 3 of 20 · …``);
     the stub answers it in that step's format with ``respond --task <row>``.
  5. Repeat until the run's commit (converged, resolved or rejected) appears
     in the task's thread.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from libs import concord
from libs.coordination_flow import (
    CoordinationSetup,
    poll_for_terminal_state,
    start_concord,
    wait_for_coordination_join,
)
from libs.mycelium_api import MyceliumAPI
from libs.mycelium_cli import MyceliumCLI

log = logging.getLogger(__name__)

CODE_SILENT = "SILENT"
CODE_OK = "OK"
#: A turn that wasn't a ``concord`` step (nothing the stub should answer).
CODE_SKIPPED = "SKIPPED"


@dataclass
class TurnResult:
    handle: str
    round_num: int
    #: The concord step answered (``anchor``, ``score``…), or "" when none.
    step: str
    ok: bool
    code: str = CODE_OK
    detail: str = ""


@dataclass
class StubRunResult:
    turns: list[TurnResult] = field(default_factory=list)
    terminal: dict | None = None   # result from poll_for_terminal_state
    timed_out: bool = False

    @property
    def converged(self) -> bool:
        return bool(self.terminal and self.terminal.get("converged"))

    @property
    def subkind(self) -> str | None:
        return self.terminal.get("subkind") if self.terminal else None

    @property
    def response_rate(self) -> float:
        if not self.turns:
            return 0.0
        return sum(1 for t in self.turns if t.ok) / len(self.turns)


def _idle(stdout: str) -> bool:
    """Whether ``await --json`` came back empty: nothing asked within its timeout."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "message" in data and data["message"] is None


class StubAgent:
    """Single-handle stub that answers each concord step from its persona."""

    def __init__(
        self,
        room: str,
        handle: str,
        persona: concord.Persona,
        cli: MyceliumCLI | None = None,
    ):
        self.room = room
        self.handle = handle
        self.persona = persona
        self.cli = cli or MyceliumCLI()

    def await_and_respond(
        self,
        round_num: int,
        turn_timeout: int,
        stop_event: threading.Event,
        task: str | None = None,
    ) -> TurnResult | None:
        """Call await; if a step arrives, answer it. ``None`` when nothing was
        asked within *turn_timeout* (the run was asking someone else)."""
        await_r = self.cli.await_turn(self.room, self.handle, timeout=turn_timeout, task=task)

        if stop_event.is_set():
            # Terminal already reached while we were awaiting — not a failure
            return TurnResult(self.handle, round_num, "", ok=True, detail="stop_event set")

        if not await_r.ok:
            if _idle(await_r.stdout):
                return None
            return TurnResult(
                self.handle, round_num, "", ok=False,
                code=CODE_SILENT,
                detail=await_r.error_message or f"await rc={await_r.returncode}",
            )

        turn_json: dict[str, Any] = {}
        if await_r.stdout.strip():
            try:
                turn_json = json.loads(await_r.stdout)
            except json.JSONDecodeError:
                pass

        prompt = concord.turn_prompt(turn_json)
        answer = self.persona.reply(prompt)
        if answer is None:
            log.info("Stub %s: not a concord step, not answering: %r", self.handle, prompt[:120])
            return TurnResult(self.handle, round_num, "", ok=True, code=CODE_SKIPPED)
        step, text = answer
        respond_r = self.cli.respond(self.room, self.handle, text, task=task)
        return TurnResult(
            self.handle, round_num, step,
            ok=respond_r.ok,
            code=CODE_OK if respond_r.ok else CODE_SILENT,
            detail=respond_r.error_message if not respond_r.ok else "",
        )


def run_stubs_until_terminal(
    api: MyceliumAPI,
    stubs: list[StubAgent],
    *,
    setup: CoordinationSetup,
    ask: str,
    fixes: int | None = None,
    bar: int | None = None,
    max_rounds: int = 30,
    turn_timeout: int = 90,
    join_wait: int = 10,
    total_timeout: int = 600,
) -> StubRunResult:
    """Drive stubs through one ``concord`` run in the setup's task.

    Flow:
      1. Start stub threads — each calls ``await --task`` immediately,
         registering presence (coordination_join).
      2. Wait for all coordination_join events (up to *join_wait* seconds).
      3. Start the run (``board coordinate``) over the stubs, in their order.
      4. Stub threads answer steps until the commit appears or timeout.

    *max_rounds* bounds how many steps each stub answers; an ``await`` that
    times out with nothing asked doesn't count against it.
    """
    result = StubRunResult()
    stop_event = threading.Event()
    lock = threading.Lock()
    cli = stubs[0].cli if stubs else MyceliumCLI()

    def run_stub(stub: StubAgent) -> None:
        answered = 0
        while answered < max_rounds and not stop_event.is_set():
            turn_result = stub.await_and_respond(
                answered, turn_timeout, stop_event, task=setup.task_key
            )
            if turn_result is None:
                continue
            if stop_event.is_set() and turn_result.detail == "stop_event set":
                break
            answered += 1
            with lock:
                result.turns.append(turn_result)
            if not turn_result.ok:
                log.warning(
                    "Stub %s round %d: %s — %s",
                    stub.handle, turn_result.round_num, turn_result.code, turn_result.detail,
                )
            else:
                log.debug("Stub %s round %d: %s", stub.handle, turn_result.round_num, turn_result.step)

    # ── Phase 1: start await threads ─────────────────────────────────────────
    threads = [threading.Thread(target=run_stub, args=(s,), daemon=True) for s in stubs]
    for t in threads:
        t.start()

    # ── Phase 2: wait for coordination_join events ────────────────────────────
    expected_handles = [s.handle for s in stubs]
    joined = wait_for_coordination_join(api, setup.room, expected_handles, timeout=join_wait)
    if not joined:
        log.warning("Not all agents joined within %ds — starting the run anyway", join_wait)

    # ── Phase 3: start the concord run in the task's thread ──────────────────
    start_concord(cli, setup, ask, handles=expected_handles, bar=bar, fixes=fixes)

    # ── Phase 4: poll for the commit in the task's thread ────────────────────
    deadline = time.time() + total_timeout
    seen_ids: set[str] = set()
    while time.time() < deadline:
        terminal = poll_for_terminal_state(
            api, setup.room,
            timeout=5, poll_interval=2,
            seen_message_ids=seen_ids,
            episode=setup.episode or None,
        )
        if terminal is not None:
            result.terminal = terminal
            log.info(
                "Terminal state: subkind=%s converged=%s min_satisfaction=%s memory=%s",
                terminal.get("subkind"), terminal.get("converged"),
                terminal.get("min_satisfaction"), terminal.get("memory"),
            )
            stop_event.set()
            break
        time.sleep(3)
    else:
        result.timed_out = True
        stop_event.set()
        log.warning("run_stubs_until_terminal timed out after %ds", total_timeout)

    for t in threads:
        t.join(timeout=turn_timeout + 5)

    return result
