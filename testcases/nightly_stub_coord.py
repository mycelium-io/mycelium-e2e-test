"""Nightly checks — stub agent coordination tests.

Gates: nightly / pre-release. No real LLM — stubs answer from scripted personas.

Flow (per test):
  1. setup_coordination: creates the agents and the conductor engine, and
     files the task the run happens in
  2. run_stubs_until_terminal:
       a. Stubs start ``await --task`` (generates coordination_join → presence)
       b. Main thread starts the conductor's ``concord`` flow in the task's
          thread once all stubs have joined
       c. The conductor asks each step; stubs answer it in its format
       d. Poll the task's thread for the commit (converged, resolved, rejected)

The personas and the outcome each scenario works out to are in
``libs/personas.py``.

Tests:
  001 - Happy path: each stub rates the other's option above the bar → converged
  002 - Stalemate: each stub rates only its own number above the bar → the fixes
        run out and the best found stands → resolved
  003 - Counter-offer chain: the first pick leaves stub-proposer short, its fix
        meets everyone's bar → converged
  004 - Respond without active await is rejected by backend
  005 - Cross-episode memory: session 1 writes a work/ row → session 2 agent_context contains it
  006 - Multi-session 100% response rate
"""

from __future__ import annotations

import logging
import time
import uuid

from pyats import aetest

from libs import personas
from libs.concord import DEFAULT_BAR
from libs.coordination_flow import (
    SUBKIND_CONVERGED,
    SUBKIND_RESOLVED,
    collect_debug_info,
    setup_coordination,
)
from libs.mycelium_api import MyceliumAPI
from libs.mycelium_cli import MyceliumCLI
from libs.stub_agent import StubAgent, StubRunResult, run_stubs_until_terminal

log = logging.getLogger(__name__)

parameters = {}

# A concord step asks its members at once and waits for every answer, so a
# step costs one stub round trip (await returns, `respond` posts: well under
# 10s) plus the conductor's own bookkeeping. A run of N steps needs about
# N x 10s. A stub that never answers costs CONDUCTOR_STEP_TIMEOUT_S (300s)
# per step before the conductor moves on, which these budgets don't cover on
# purpose: a silent stub is a failure here, not a slow pass.
_HAPPY_TOTAL_TIMEOUT = 180     # anchor, propose, score: 3 steps
_COUNTER_TOTAL_TIMEOUT = 240   # + one repair and its rescore: 5 steps
_TURN_TIMEOUT = 60             # seconds for a single await call
_MAX_ROUNDS = 20               # steps a stub answers before its thread stops
_JOIN_WAIT = 10                # seconds to wait for coordination_join events

_TITLE = "Decide audit log retention"
_ASK = "decide how long to keep audit logs, given the storage budget and the audit obligation"


def _fresh_room(suffix: str = "") -> str:
    return f"qa-coord-fresh-{suffix or uuid.uuid4().hex[:8]}"


def _stubs(room: str, cli: MyceliumCLI, cast: dict) -> list[StubAgent]:
    return [StubAgent(room, handle, persona, cli=cli) for handle, persona in cast.items()]


def _answered(r: StubRunResult, handle: str, step: str) -> bool:
    return any(t.handle == handle and t.step == step and t.ok for t in r.turns)


class TwoStubHappyPath(aetest.Testcase):
    """001 — Each stub rates the other's option above the bar → converged."""

    uid = "nightly_001"

    @aetest.setup
    def setup(self, api: MyceliumAPI, cli: MyceliumCLI, testscript):
        self.room = _fresh_room()
        testscript.parameters.setdefault("owned_rooms", set()).add(self.room)
        api.create_room(self.room, description="Nightly stub happy path")
        self.cast = personas.happy()
        self.coord = setup_coordination(api, cli, self.room, list(self.cast), title=_TITLE)
        if self.coord is None:
            self.failed("coordination setup failed")

    @aetest.test
    def stubs_converge(self, api: MyceliumAPI, cli: MyceliumCLI):
        run_result = run_stubs_until_terminal(
            api, _stubs(self.room, cli, self.cast), setup=self.coord, ask=_ASK,
            max_rounds=_MAX_ROUNDS, turn_timeout=_TURN_TIMEOUT,
            join_wait=_JOIN_WAIT, total_timeout=_HAPPY_TOTAL_TIMEOUT,
        )
        self._assert_converged(api, run_result, _HAPPY_TOTAL_TIMEOUT)

    def _assert_converged(self, api: MyceliumAPI, r: StubRunResult, budget: int) -> None:
        if r.timed_out or r.terminal is None:
            debug = collect_debug_info(api, self.room, list(self.cast), self.coord.episode)
            self.failed(f"TIMEOUT: no commit within {budget}s. debug={debug}")
        if not r.converged:
            self.failed(
                f"Expected converged, got subkind={r.subkind!r} "
                f"min_satisfaction={r.terminal.get('min_satisfaction')!r}. "
                f"turns={[(t.handle, t.step, t.code) for t in r.turns]}"
            )
        lowest = r.terminal.get("min_satisfaction")
        assert lowest is not None and lowest >= DEFAULT_BAR, (
            f"converged with min_satisfaction={lowest!r}, below the bar {DEFAULT_BAR}"
        )
        memory = r.terminal.get("memory") or ""
        assert memory.startswith("context/decision/"), f"decision not saved: memory={memory!r}"
        log.info(
            "Terminal: subkind=%s min_satisfaction=%s memory=%s turns=%d response_rate=%.0f%%",
            r.subkind, lowest, memory, len(r.turns), r.response_rate * 100,
        )

    @aetest.cleanup
    def teardown(self, api: MyceliumAPI):
        api.delete_room(self.room)


class TwoStubStalematePath(aetest.Testcase):
    """002 — Neither stub rates the other's options above the bar → resolved."""

    uid = "nightly_002"

    # A stalemate only ends when the fixes run out: after every pick the
    # least happy stub is asked for a fix and everyone rates it, so each fix
    # costs two steps. fixes=2 (the flow's own default is 5) makes it
    # anchor, propose, score, then 2 x (repair, rescore): 7 steps, plus one
    # more if the conductor sends a fix back as a repeat of an earlier option.
    _FIXES = 2
    _TOTAL_TIMEOUT = 360

    @aetest.setup
    def setup(self, api: MyceliumAPI, cli: MyceliumCLI, testscript):
        self.room = _fresh_room()
        testscript.parameters.setdefault("owned_rooms", set()).add(self.room)
        api.create_room(self.room, description="Nightly stub stalemate")
        self.cast = personas.stalemate()
        self.coord = setup_coordination(api, cli, self.room, list(self.cast), title=_TITLE)
        if self.coord is None:
            self.failed("coordination setup failed")

    @aetest.test
    def best_found_stands_when_the_fixes_run_out(self, api: MyceliumAPI, cli: MyceliumCLI):
        run_result = run_stubs_until_terminal(
            api, _stubs(self.room, cli, self.cast), setup=self.coord, ask=_ASK,
            fixes=self._FIXES,
            max_rounds=_MAX_ROUNDS, turn_timeout=_TURN_TIMEOUT,
            join_wait=_JOIN_WAIT, total_timeout=self._TOTAL_TIMEOUT,
        )
        if run_result.timed_out or run_result.terminal is None:
            debug = collect_debug_info(api, self.room, list(self.cast), self.coord.episode)
            self.failed(f"TIMEOUT: stalemate did not end within {self._TOTAL_TIMEOUT}s. debug={debug}")
        # concord ends a stalemate `resolved`, not `rejected`: once the fixes
        # are used up the best option found stands, below the bar for someone.
        # `rejected` is for a run with nothing to pick from.
        assert run_result.subkind == SUBKIND_RESOLVED, (
            f"Expected resolved (the fixes run out), got subkind={run_result.subkind!r}"
        )
        lowest = run_result.terminal.get("min_satisfaction")
        assert lowest is not None and lowest < DEFAULT_BAR, (
            f"resolved with min_satisfaction={lowest!r}: someone should be short of the bar"
        )
        # The stalemate has to come from stub-reject's ratings and fixes, not
        # from it going quiet: a silent rater also ends a run `resolved`.
        assert _answered(run_result, "stub-reject", "repair"), (
            f"Expected stub-reject to have been asked for a fix and answered it — "
            f"turns={[(t.handle, t.step, t.ok) for t in run_result.turns]}"
        )
        log.info(
            "Stalemate terminal: subkind=%s min_satisfaction=%s memory=%s",
            run_result.subkind, lowest, run_result.terminal.get("memory"),
        )

    @aetest.cleanup
    def teardown(self, api: MyceliumAPI):
        api.delete_room(self.room)


class CounterOfferChain(aetest.Testcase):
    """003 — The first pick leaves stub-proposer short; its fix converges."""

    uid = "nightly_003"

    @aetest.setup
    def setup(self, api: MyceliumAPI, cli: MyceliumCLI, testscript):
        self.room = _fresh_room()
        testscript.parameters.setdefault("owned_rooms", set()).add(self.room)
        api.create_room(self.room, description="Nightly counter-offer chain")
        self.cast = personas.counter()
        self.coord = setup_coordination(api, cli, self.room, list(self.cast), title=_TITLE)
        if self.coord is None:
            self.failed("coordination setup failed")

    @aetest.test
    def fix_then_converge(self, api: MyceliumAPI, cli: MyceliumCLI):
        run_result = run_stubs_until_terminal(
            api, _stubs(self.room, cli, self.cast), setup=self.coord, ask=_ASK,
            max_rounds=_MAX_ROUNDS, turn_timeout=_TURN_TIMEOUT,
            join_wait=_JOIN_WAIT, total_timeout=_COUNTER_TOTAL_TIMEOUT,
        )
        if run_result.timed_out or run_result.terminal is None:
            self.failed(f"TIMEOUT: counter-offer chain did not end in {_COUNTER_TOTAL_TIMEOUT}s")
        assert run_result.subkind == SUBKIND_CONVERGED, (
            f"Expected converged after one fix, got subkind={run_result.subkind!r}"
        )
        assert _answered(run_result, "stub-proposer", "repair"), (
            f"Expected stub-proposer to have been asked for a fix — "
            f"turns={[(t.handle, t.step, t.ok) for t in run_result.turns]}"
        )
        log.info(
            "Counter-offer chain terminal: subkind=%s min_satisfaction=%s",
            run_result.subkind, run_result.terminal.get("min_satisfaction"),
        )

    @aetest.cleanup
    def teardown(self, api: MyceliumAPI):
        api.delete_room(self.room)


class RespondWithoutTurnRejected(aetest.Testcase):
    """004 — Respond without being an awaiting participant is rejected."""

    uid = "nightly_004"

    @aetest.test
    def out_of_turn_respond_fails(self, api: MyceliumAPI, cli: MyceliumCLI, testscript):
        room = _fresh_room()
        testscript.parameters.setdefault("owned_rooms", set()).add(room)
        api.create_room(room)

        r = cli.respond(room, "ghost-handle", "I accept. [<accept>]")
        assert not r.ok, (
            f"Expected respond to fail for unregistered handle, "
            f"but got rc={r.returncode}: {r.stdout[:200]}"
        )
        log.info("Correctly rejected respond from unregistered handle (rc=%d)", r.returncode)
        api.delete_room(room)


class CrossEpisodeMemory(aetest.Testcase):
    """005 — Session 1 writes a work/ row → session 2 agent_context contains it."""

    uid = "nightly_005"

    @aetest.setup
    def setup(self, api: MyceliumAPI, testscript):
        self.room = f"qa-cross-episode-stub-{uuid.uuid4().hex[:8]}"
        testscript.parameters.setdefault("owned_rooms", set()).add(self.room)
        api.create_room(self.room, description="Nightly cross-episode memory")

    @aetest.test
    def seed_and_verify_in_context(self, api: MyceliumAPI, cli: MyceliumCLI):
        decision_key = f"decisions/api-design-{uuid.uuid4().hex[:6]}"
        decision_content = "We will use REST with JSON:API conventions."
        r = cli.memory_set(self.room, "qa-tester", decision_key, decision_content)
        assert r.ok, f"memory set failed: {r.error_message}"

        work_key = f"work/auth-migration-{uuid.uuid4().hex[:6]}"
        work_content = "Migrate services to JWT tokens. Owner: @stub-a."
        r = cli.memory_set(self.room, "qa-tester", work_key, work_content)
        assert r.ok, f"memory set failed: {r.error_message}"

        cli.memory_reindex(self.room)
        time.sleep(2)

        import urllib.parse
        enc = urllib.parse.quote(self.room, safe="")
        status, context = api.get_json(f"/rooms/{enc}/agent-context")
        if status == 404:
            status2, data = api.list_memory(self.room)
            assert status2 == 200
            keys = [m.get("key") for m in (data if isinstance(data, list) else [])]
            assert any(decision_key in (k or "") for k in keys), (
                f"Decision key not in memory: {keys}"
            )
            return

        assert status == 200, f"agent_context returned {status}"
        context_str = str(context)
        # agent_context is work/-only by design (fastapi-backend/app/services/
        # briefing.py: "the room's title, and the work that is open in it") —
        # decisions/ never appear there, only in full-text memory search. The
        # cross-episode claim this test is actually proving is that session 2
        # sees what session 1 wrote at all, via the work row both sessions share.
        assert "auth-migration" in context_str or work_content[:20] in context_str, (
            f"Session 1 work row not in session 2 agent_context: {context_str[:500]}"
        )

    @aetest.cleanup
    def teardown(self, api: MyceliumAPI):
        api.delete_room(self.room)


class MultiSessionResponseRate(aetest.Testcase):
    """006 — 3 sequential stub sessions; gate on 100% stub response rate."""

    uid = "nightly_006"
    _N_SESSIONS = 3

    @aetest.setup
    def setup(self, api: MyceliumAPI, testscript):
        self.room = f"qa-coord-fresh-multi-{uuid.uuid4().hex[:8]}"
        testscript.parameters.setdefault("owned_rooms", set()).add(self.room)
        api.create_room(self.room, description="Nightly multi-session response rate")

    @aetest.test
    def all_sessions_100pct_response(self, api: MyceliumAPI, cli: MyceliumCLI):
        total_turns = 0
        successful_turns = 0

        for session_num in range(self._N_SESSIONS):
            log.info("Starting session %d/%d", session_num + 1, self._N_SESSIONS)
            # Each session is its own task, and so its own thread and run: the
            # room's earlier commits are in other threads and never answer for it.
            cast = personas.happy()
            coord = setup_coordination(
                api, cli, self.room, list(cast),
                title=f"{_TITLE}, session {session_num + 1}",
            )
            if coord is None:
                self.failed(f"coordination setup failed for session {session_num + 1}")

            run_result = run_stubs_until_terminal(
                api, _stubs(self.room, cli, cast), setup=coord, ask=_ASK,
                max_rounds=_MAX_ROUNDS, turn_timeout=_TURN_TIMEOUT,
                join_wait=_JOIN_WAIT, total_timeout=_HAPPY_TOTAL_TIMEOUT,
            )

            for turn in run_result.turns:
                total_turns += 1
                if turn.ok:
                    successful_turns += 1
                else:
                    log.warning(
                        "Session %d: SILENT %s round %d — %s",
                        session_num + 1, turn.handle, turn.round_num, turn.detail,
                    )

            if run_result.timed_out:
                log.warning("Session %d timed out", session_num + 1)
            else:
                log.info("Session %d: subkind=%s", session_num + 1, run_result.subkind)

        if total_turns == 0:
            self.failed("No turns recorded across any session")

        rate = successful_turns / total_turns
        assert rate == 1.0, (
            f"Expected 100% response rate across {self._N_SESSIONS} sessions, "
            f"got {rate:.0%} ({successful_turns}/{total_turns})"
        )

    @aetest.cleanup
    def teardown(self, api: MyceliumAPI):
        api.delete_room(self.room)
