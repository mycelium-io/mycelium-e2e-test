"""Remote stub agent — drives mycelium await/respond via host_exec.

Since await/respond are stateless HTTP calls to the backend, a stub can run
on any device (local, docker, ssh) simply by dispatching CLI commands through
host_exec.execute(). No SLIM socket or special networking is required on the
spoke — only HTTP connectivity to MYCELIUM_BACKEND_URL.

Each stub answers the conductor's ``concord`` steps in the task's thread from
a scripted :class:`~libs.concord.Persona` (see :mod:`libs.stub_agent`).

Usage::

    hub_device = testscript.parameters["testbed"].devices["hub"]
    spoke1_device = testscript.parameters["testbed"].devices["spoke1"]

    personas = hub_spoke(["stub-hub", "stub-spoke1"])
    stubs = [
        RemoteStubAgent(hub_device, room, "stub-hub", personas["stub-hub"]),
        RemoteStubAgent(spoke1_device, room, "stub-spoke1", personas["stub-spoke1"]),
    ]
    result = run_remote_stubs_until_terminal(
        api, stubs, setup=coord_setup, ask="decide the log retention window",
        total_timeout=180,
    )
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from libs import concord, log_fmt
from libs.coordination_flow import (
    CoordinationSetup,
    poll_for_terminal_state,
    start_concord,
    wait_for_coordination_join,
)
from libs.host_exec import HostExecError, describe, execute
from libs.mycelium_api import MyceliumAPI

log = logging.getLogger(__name__)

CODE_OK = "OK"
CODE_SILENT = "SILENT"
CODE_EXEC_ERROR = "EXEC_ERROR"
CODE_SKIPPED = "SKIPPED"

#: What ``await_turn`` returns when nothing was asked within its timeout.
IDLE = "idle"


@dataclass
class TurnResult:
    handle: str
    device: str  # describe(device) string
    round_num: int
    #: The concord step answered (``anchor``, ``score``…), or "" when none.
    step: str
    ok: bool
    code: str = CODE_OK
    detail: str = ""


@dataclass
class RemoteStubRunResult:
    turns: list[TurnResult] = field(default_factory=list)
    terminal: dict | None = None
    timed_out: bool = False

    @property
    def converged(self) -> bool:
        return bool(self.terminal and self.terminal.get("converged"))

    @property
    def response_rate(self) -> float:
        if not self.turns:
            return 0.0
        return sum(1 for t in self.turns if t.ok) / len(self.turns)


class RemoteStubAgent:
    """Stub agent that runs mycelium CLI on a remote device via host_exec."""

    def __init__(
        self,
        device: Any,
        room: str,
        handle: str,
        persona: concord.Persona,
        backend_url: str = "",
        use_cursor: bool = False,
        cursor_model: str = "",
    ):
        self.device = device
        self.device_desc = describe(device)
        self.room = room
        self.handle = handle
        self.persona = persona
        self.backend_url = backend_url
        # Opt-in: have a real `cursor-agent`/`agent` CLI write this stub's
        # replies instead of the persona's scripted ones. Requires
        # CURSOR_API_KEY set in the environment on ``device`` (host_exec
        # forwards it to docker-transport spokes; local/hub inherits it
        # directly) and the cursor CLI on PATH. Any failure (missing key,
        # binary absent, empty output, timeout) falls back to the scripted
        # reply so a flaky/unconfigured cursor setup never turns into a
        # silent turn.
        self.use_cursor = use_cursor
        self.cursor_model = cursor_model or os.environ.get("CURSOR_MODEL", "")

    def _env_args(self) -> list[str]:
        """Build env var prefix for CLI commands if backend_url is set."""
        if self.backend_url:
            return ["env", f"MYCELIUM_BACKEND_URL={self.backend_url}"]
        return []

    def _cursor_respond_text(self, prompt: str, scripted: str, timeout: int = 45) -> str | None:
        """Write this turn's reply with a real `cursor-agent`/`agent` CLI call.

        The conductor's prompt says exactly what shape the reply takes, so it
        is passed through whole, with the scripted reply as the stance to
        take. Returns the model's plain-text output, or None on any failure
        (missing binary, no CURSOR_API_KEY, empty output, timeout) so the
        caller can fall back to the scripted reply.
        """
        instruction = (
            "You are one member of a negotiation run by the mycelium conductor. "
            "Answer the conductor's prompt below in exactly the format it asks "
            "for: the same line labels, one line each, no markdown, no "
            "preamble, no name prefix. Take the stance of the example answer.\n\n"
            f"Your handle: {self.handle}\n\n"
            f"Example answer:\n{scripted}\n\n"
            f"Conductor's prompt:\n{prompt}"
        )
        cmd = ["agent", "-p", instruction, "--output-format", "text"]
        if self.cursor_model:
            cmd.extend(["--model", self.cursor_model])
        log.info("RemoteStub %s@%s cursor-agent dispatch: %s", self.handle, self.device_desc, " ".join(cmd))
        try:
            result = execute(self.device, cmd, timeout=timeout)
        except HostExecError as e:
            log.warning("RemoteStub %s@%s cursor-agent exec error: %s",
                       self.handle, self.device_desc, e)
            return None
        if result.returncode != 0:
            log.warning("RemoteStub %s@%s cursor-agent rc=%d stderr=%s",
                       self.handle, self.device_desc, result.returncode, result.stderr.strip()[:200])
            return None
        log.info("RemoteStub %s@%s cursor-agent result: rc=0 (%d chars)",
                 self.handle, self.device_desc, len(result.stdout))
        log.debug("RemoteStub %s@%s cursor-agent stdout:\n%s",
                  self.handle, self.device_desc, log_fmt.pretty(result.stdout))
        text = result.stdout.strip()
        return text or None

    def await_turn(
        self, turn_timeout: int, stop_event: threading.Event, task: str | None = None
    ) -> dict | str | None:
        """Call `mycelium await` on the device: the parsed turn, :data:`IDLE`
        when nothing was asked within *turn_timeout*, or None on an error."""
        cmd = [
            *self._env_args(),
            "mycelium", "await",
            "--room", self.room,
            "--handle", self.handle,
            "--timeout", str(turn_timeout),
            "--json",
        ]
        if task:
            cmd.extend(["--task", task])
        log.info("RemoteStub %s@%s await dispatch: %s", self.handle, self.device_desc, " ".join(cmd))
        try:
            result = execute(self.device, cmd, timeout=turn_timeout + 10)
        except HostExecError as e:
            if not stop_event.is_set():
                log.warning("RemoteStub %s@%s await exec error: %s",
                           self.handle, self.device_desc, e)
            return None

        try:
            turn_data = json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            log.debug("RemoteStub %s@%s await: non-JSON stdout: %s",
                      self.handle, self.device_desc, result.stdout.strip()[:200])
            return None

        if isinstance(turn_data, dict) and "message" in turn_data and turn_data["message"] is None:
            return IDLE
        if result.returncode != 0 or not isinstance(turn_data, dict):
            log.debug("RemoteStub %s@%s await result: rc=%d stdout=%r stderr=%r",
                      self.handle, self.device_desc, result.returncode,
                      result.stdout.strip()[:200], result.stderr.strip()[:200])
            return None

        log.info("RemoteStub %s@%s await result: rc=0 turn received", self.handle, self.device_desc)
        log.debug("RemoteStub %s@%s await turn_data:\n%s",
                  self.handle, self.device_desc, log_fmt.pretty(turn_data))
        return turn_data

    def post_respond(self, text: str, turn_timeout: int, task: str | None = None) -> bool:
        """Call `mycelium respond` on the device. Returns True on success."""
        cmd = [
            *self._env_args(),
            "mycelium", "respond",
            "--room", self.room,
            "--handle", self.handle,
        ]
        if task:
            cmd.extend(["--task", task])
        cmd.append(text)
        log.info("RemoteStub %s@%s respond dispatch: %s", self.handle, self.device_desc, " ".join(cmd))
        try:
            result = execute(self.device, cmd, timeout=turn_timeout)
            log.info("RemoteStub %s@%s respond result: rc=%d", self.handle, self.device_desc, result.returncode)
            if result.stdout.strip() or result.stderr.strip():
                log.debug("RemoteStub %s@%s respond stdout=%s stderr=%s",
                         self.handle, self.device_desc,
                         log_fmt.pretty(result.stdout), log_fmt.pretty(result.stderr))
            return result.returncode == 0
        except HostExecError as e:
            log.warning("RemoteStub %s@%s respond exec error: %s",
                       self.handle, self.device_desc, e)
            return False

    def do_one_turn(
        self,
        round_num: int,
        turn_timeout: int,
        stop_event: threading.Event,
        task: str | None = None,
    ) -> TurnResult | None:
        """One await and its answer; None when nothing was asked in time."""
        turn_data = self.await_turn(turn_timeout, stop_event, task)
        if stop_event.is_set():
            return TurnResult(self.handle, self.device_desc, round_num, "",
                              ok=True, detail="stop_event set")
        if turn_data == IDLE:
            return None
        if not isinstance(turn_data, dict):
            return TurnResult(self.handle, self.device_desc, round_num, "",
                              ok=False, code=CODE_SILENT,
                              detail="await returned no message")

        prompt = concord.turn_prompt(turn_data)
        answer = self.persona.reply(prompt)
        if answer is None:
            log.info("RemoteStub %s@%s: not a concord step, not answering: %r",
                     self.handle, self.device_desc, prompt[:120])
            return TurnResult(self.handle, self.device_desc, round_num, "",
                              ok=True, code=CODE_SKIPPED)
        step, text = answer
        if self.use_cursor:
            written = self._cursor_respond_text(prompt, text)
            if written is None:
                log.warning("RemoteStub %s@%s cursor-agent produced no usable reply, "
                           "falling back to the scripted one", self.handle, self.device_desc)
            text = written or text
        ok = self.post_respond(text, turn_timeout, task)
        return TurnResult(
            self.handle, self.device_desc, round_num, step, ok=ok,
            code=CODE_OK if ok else CODE_SILENT,
            detail="" if ok else "respond failed",
        )


def run_remote_stubs_until_terminal(
    api: MyceliumAPI,
    stubs: list[RemoteStubAgent],
    *,
    setup: CoordinationSetup,
    ask: str,
    fixes: int | None = None,
    bar: int | None = None,
    max_rounds: int = 30,
    turn_timeout: int = 90,
    join_wait: int = 10,
    total_timeout: int = 600,
    cli: Any = None,
) -> RemoteStubRunResult:
    """Drive remote stubs through one ``concord`` run in the setup's task.

    Same flow as stub_agent.run_stubs_until_terminal but each agent
    dispatches CLI commands via host_exec to its own device.
    """
    from libs.mycelium_cli import MyceliumCLI
    _cli = cli or MyceliumCLI()

    result = RemoteStubRunResult()
    stop_event = threading.Event()
    lock = threading.Lock()

    def run_stub(stub: RemoteStubAgent) -> None:
        answered = 0
        while answered < max_rounds and not stop_event.is_set():
            tr = stub.do_one_turn(answered, turn_timeout, stop_event, setup.task_key)
            if tr is None:
                continue
            if stop_event.is_set() and tr.detail == "stop_event set":
                break
            answered += 1
            with lock:
                result.turns.append(tr)
            if not tr.ok:
                log.warning("RemoteStub %s@%s round %d: %s — %s",
                           stub.handle, stub.device_desc, tr.round_num, tr.code, tr.detail)
            else:
                log.debug("RemoteStub %s@%s round %d: %s",
                         stub.handle, stub.device_desc, tr.round_num, tr.step)

    # Phase 1: start stub threads (they register presence via await)
    threads = [threading.Thread(target=run_stub, args=(s,), daemon=True) for s in stubs]
    for t in threads:
        t.start()

    # Phase 2: wait for coordination_join events
    expected = [s.handle for s in stubs]
    joined = wait_for_coordination_join(api, setup.room, expected, timeout=join_wait)
    if not joined:
        log.warning("Not all agents joined within %ds — starting the run anyway", join_wait)

    # Phase 3: start the concord run in the task's thread
    start_concord(_cli, setup, ask, handles=expected, bar=bar, fixes=fixes)

    # Phase 4: poll for the commit in the task's thread
    deadline = time.time() + total_timeout
    seen_ids: set[str] = set()
    while time.time() < deadline:
        terminal = poll_for_terminal_state(
            api, setup.room, timeout=5, poll_interval=2,
            seen_message_ids=seen_ids,
            episode=setup.episode or None,
        )
        if terminal is not None:
            result.terminal = terminal
            log.info("Terminal: subkind=%s converged=%s min_satisfaction=%s",
                     terminal.get("subkind"), terminal.get("converged"),
                     terminal.get("min_satisfaction"))
            stop_event.set()
            break
        time.sleep(3)
    else:
        result.timed_out = True
        stop_event.set()
        log.warning("run_remote_stubs_until_terminal timed out after %ds", total_timeout)

    for t in threads:
        t.join(timeout=turn_timeout + 5)

    return result
