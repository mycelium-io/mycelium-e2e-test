# pyATS E2E Testing for Mycelium

This skill documents the patterns and conventions used in this repo's pyATS test suite for [Mycelium](https://github.com/mycelium-io/mycelium), a SLIM-native multi-agent coordination platform.

## When to Use

Use this skill when:
- Adding a new test to the PR, nightly, or canary suite
- Understanding the pyATS structure (jobs → suites → testcases → datafiles)
- Debugging a test failure in the pyATS execution model
- Writing a new mechanical stub or live-agent scenario

## Core Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Job file (jobs/*.py)             ← orchestrates execution     │
│   pyats run job jobs/nightly_job.py                            │
├──────────────────────────────────────────────────────────────┤
│ Suite file (suites/*.py)         ← thin AEtest script          │
│   CommonSetup → Testcases → CommonCleanup                      │
├──────────────────────────────────────────────────────────────┤
│ Testcase classes (testcases/*.py)  ← reusable test logic       │
│   @aetest.setup / @aetest.test / @aetest.cleanup                │
├──────────────────────────────────────────────────────────────┤
│ Libraries (libs/*.py)            ← API/CLI clients, stubs      │
│ Datafiles (data/*.yaml)          ← YAML-driven parameters      │
│ Testbeds (testbeds/*.yaml)       ← device topology             │
└──────────────────────────────────────────────────────────────┘
```

Three suites: **PR** (`pr_suite.py`, stack/memory/protocol, no LLM), **nightly** (`nightly_suite.py`, mechanical stub negotiation + hub-and-spoke, no LLM), **canary** (`canary_suite.py`, real cursor-agent multi-episode, informational only — never blocks release). See the repo [README](README.md) for the full suite table.

## Key Patterns

### 1. Thin Suite Files

Suite files (`suites/*.py`) contain only class declarations that inherit from testcase classes — no test logic:

```python
from testcases.common_setup_cleanup import MyceliumCommonSetup, MyceliumCommonCleanup
from testcases.pr_stack import BackendHealth as _BackendHealth

class CommonSetup(MyceliumCommonSetup):
    pass

class BackendHealth(_BackendHealth):
    pass

class CommonCleanup(MyceliumCommonCleanup):
    pass
```

Testcase classes are imported under a `_Prefixed` alias and re-declared without one — this is what lets pyATS report the plain class name while still reusing logic across suites (e.g. `TwoNodeHubSpoke` is declared once in `nightly_hub_spoke.py` and appears, unmodified, in `nightly_suite.py`).

### 2. Testcase Classes with `uid`

Test logic lives in `testcases/*.py`. Give it a stable `uid` (independent of the class name) so datafiles and CI logs can reference it durably:

```python
class TwoStubStalematePath(aetest.Testcase):
    """002 — Neither stub rates the other's options above the bar → resolved."""
    uid = "nightly_002"

    @aetest.setup
    def setup(self, api: MyceliumAPI, cli: MyceliumCLI, testscript):
        ...

    @aetest.test
    def best_found_stands_when_the_fixes_run_out(self, api: MyceliumAPI, cli: MyceliumCLI):
        ...
```

### 3. Parameter Injection from `testscript.parameters`

`MyceliumCommonSetup` (in `testcases/common_setup_cleanup.py`) populates `testscript.parameters["api"]`, `["cli"]`, `["env"]`, `["room_name"]`, `["owned_rooms"]`. pyATS injects these into any testcase method by argument name:

```python
@aetest.test
def verify(self, api: MyceliumAPI, cli: MyceliumCLI, room_name: str):
    ...
```

`owned_rooms` is a `set()` every testcase should add its own room(s) to — `MyceliumCommonCleanup` deletes everything in it (unless `MYCELIUM_E2E_NO_CLEANUP`/`MYCELIUM_E2E_KEEP_ROOMS` is set). This is the mechanism that prevents cross-run interference; add to it, don't bypass it.

### 4. Environment-Driven Skip Logic

```python
@aetest.setup
def check_prerequisites(self, env):
    if env.skip_llm_tests:
        self.skipped("LLM not available")
```

`env` (an `EnvironmentInfo`, from `libs/environment.py`) is set in `CommonSetup.detect_environment` and carries `backend_reachable`, `slim_reachable`, `skip_llm_tests`.

### 5. Gating on Testbed Topology

Tests that need more than the base `hub` device (hub-and-spoke) call `require_devices` at the top of setup — it skips (not fails) when the loaded testbed doesn't have the device, and is a no-op when no testbed was passed at all (single-host mode):

```python
from testcases.common_setup_cleanup import require_devices

@aetest.setup
def check_topology(self, testscript):
    require_devices(self, testscript, "spoke1", "spoke2")
```

### 6. Mechanical Stub Coordination (nightly suite)

Negotiation is the conductor engine's `concord` flow, and a conductor run needs a task. `setup_coordination()` creates the agents and a conductor (`engine create conductor --kind conductor`) and files a task (`board new … --json`); `run_stubs_until_terminal()` starts each stub's `await --task <row>` loop, then starts the run with `board coordinate <row> conductor "concord @~a @~b: <ask>"` and polls the task's thread for the run's commit.

Each stub answers from a `libs/concord.py` `Persona`, without any LLM. A turn's prompt opens with a header naming its step (`concord · score · turn 3 of 20 · stub-a`), and the persona answers that step in its format: `bad: N - why` / `ideal: N - why` for `anchor`, an option and a `why:` line for `propose` and `repair`, and `A (…): N - why` per option for `score` and `rescore`. It replies with `respond --task <row>`, since a turn asked in a task's thread is only answered by a reply in that thread. The scenarios' personas, and the outcome each works out to, are in `libs/personas.py`. Cross-container variants (`libs/remote_stub.py`'s `RemoteStubAgent`) do the same over `docker exec` for spoke devices.

```python
cast = personas.happy()
coord = setup_coordination(api, cli, room, list(cast), title="Decide audit log retention")
stubs = [StubAgent(room, h, p, cli=cli) for h, p in cast.items()]
result = run_stubs_until_terminal(api, stubs, setup=coord, ask="decide how long to keep audit logs",
                                  max_rounds=20, total_timeout=180)
assert result.subkind == "converged"
```

A run ends with a `commit` whose subkind is `converged` (every member places the pick at the bar or above; the default bar is 60), `resolved` (the fixes ran out and the best option found stands) or `rejected` (nothing was proposed, or the run hit its step cap). A stalemate ends `resolved`, not `rejected`. The room's message listing shows the commit as an `l9_commit` message in the task's thread; `poll_for_terminal_state(..., episode=coord.episode)` returns its subkind, `memory` (where the decision was saved, `context/decision/<slug>`) and `min_satisfaction`.

Two budgets matter and are easy to undersize: `total_timeout` (the client's own patience) and `max_rounds` (how many steps each stub thread will answer). Both need headroom over the number of steps the run takes — see the next pattern.

### 7. Sizing timeouts around the conductor's step count

A `concord` run is a known sequence of steps: `anchor`, `propose`, `score`, then one `repair` (asked of the member least happy with the pick) and one `rescore` per fix, until the pick meets everyone's bar or the fixes run out. The flow allows 5 fixes; a summon can set fewer with `fixes=N`. On top of that the conductor asks a step once more of a member whose reply it couldn't read, and sends a fix back once when it repeats an option already on the table. A step asks its members at once and waits for all of them, so it costs one round trip: well under 10s for a stub, 30-90s for a real agent.

Size a test's budget from the steps its scenario takes:

```python
# Stalemate with fixes=2: anchor, propose, score, 2 x (repair, rescore) = 7 steps,
# plus one if a fix is sent back as a repeat.
_FIXES = 2
_TOTAL_TIMEOUT = 360
```

The conductor's own limits sit well above these runs: `CONDUCTOR_MAX_STEPS` (backend `app/config.py`, 24) and the flow's `max_steps` (20) cap the steps, the smaller one winning, and the run ends `rejected` when it reaches the cap. `CONDUCTOR_STEP_TIMEOUT_S` (300) is how long a step waits for a member; a member that doesn't answer is recorded as silent and the run moves on. A silent rater leaves the pick unrated by them, which also ends the run `resolved`, so a non-converging test should check its stub actually answered (`TwoStubStalematePath` checks stub-reject answered a `repair` step).

If a test times out with `terminal=None`, read the run's record, a `log/episodes/<id>` memory in the room that the conductor writes from the run's start and updates after every step: its trace lists each step taken and who was silent in it. A run still at `anchor` means the stubs never got the first step (check they await with `--task` and the summon named them); a run that stopped partway means the client gave up or the stub threads ran out of rounds while it was still walking.

### 8. Datafile Inheritance with `extends:`

```yaml
# data/nightly_datafile.yaml
extends: base_datafile.yaml
parameters:
  room_prefix: "qa-coord-fresh"
  max_failures: 5
```

`base_datafile.yaml` holds every shared parameter (topology, timeouts, room prefix); each suite's datafile only overrides what differs.

### 9. Job File Orchestration

```python
from jobs._common import get_datafile, get_project_root, install_job_sigint_cleanup, resolve_backend_url

def main(runtime):
    root = get_project_root()
    datafile = get_datafile(default="nightly_datafile.yaml")
    install_job_sigint_cleanup(resolve_backend_url(datafile))
    for suite_name in ("pr_suite.py", "nightly_suite.py"):
        run(testscript=os.path.join(root, "suites", suite_name), datafile=datafile)
```

`nightly_job.py` runs both `pr_suite.py` and `nightly_suite.py` in sequence (the PR suite gates the nightly suite). `install_job_sigint_cleanup` registers a SIGINT handler that still deletes owned rooms on Ctrl-C.

### 10. Groups for Selective Execution

```python
class SkillCrossChannelReturnTrip(_DistributedBase):
    groups = ["hub_and_spoke", "cross_channel", "llm", "slow"]
```

Filter with `MYCELIUM_E2E_GROUPS` (comma-separated, OR'd — see `jobs/_common.py`'s `groups_filter_from_env`) or pyATS's own `--groups` flag.

## Adding a New Test

1. Add the testcase class to the right `testcases/{pr,nightly,canary}_*.py` file, with a `uid`.
2. Add a thin re-declaration in the suite file(s) it belongs in (`pr_suite.py`, `nightly_suite.py`, or `canary_suite.py`).
3. If it needs its own timeout/round budget, size it against real step/turn cost (pattern 7) — don't silently reuse a module-level constant sized for a different scenario.
4. Set `groups` if it should be selectively filterable.
5. Run it directly first: `pyats run job jobs/<suite>_job.py --testbed-file testbeds/local.yaml`.

## Library Reference

| Module | Class/Function | Purpose |
|--------|-----------------|---------|
| `libs/mycelium_api.py` | `MyceliumAPI` | Backend REST client (rooms, memory, sessions, agent-context) |
| `libs/mycelium_cli.py` | `MyceliumCLI` | CLI subprocess wrapper; handles the `MYCELIUM_LOCAL_WRITE_UID` switch |
| `libs/environment.py` | `EnvironmentInfo`, `detect_environment` | Service probing and skip-flag detection |
| `libs/coordination_flow.py` | `setup_coordination`, `start_concord`, `poll_for_terminal_state`, `wait_for_coordination_join` | Agents + conductor + task; starting a concord run; commit/join polling |
| `libs/concord.py` | `Persona`, `parse_step`, `summon` | Scripted answers to each concord step, in the formats the conductor reads |
| `libs/personas.py` | `happy`, `counter`, `stalemate`, `hub_spoke` | The nightly scenarios' personas, and the outcome each works out to |
| `libs/stub_agent.py` | `StubAgent`, `run_stubs_until_terminal` | Mechanical single-process stub negotiation |
| `libs/remote_stub.py` | `RemoteStubAgent`, `run_remote_stubs_until_terminal` | Same, driven over `docker exec` on spoke devices |
| `libs/agent_pools.py` | — | Provisioner-backed agent pool for `hub_and_spoke_tests.py`-style scenarios |
| `libs/provisioners/` | `Provisioner`, `get_provisioner` | Adapter-agnostic (openclaw/cursor/hermes) create/wake/cleanup protocol |
| `libs/host_exec.py` | `execute` | Local vs. `docker exec` command dispatch by device `custom.transport` |

## Design Decisions

1. **No traditional pyATS testbed for single-host runs** — `testbeds/local.yaml` exists only to satisfy the pyATS schema; topology-dependent tests key off `testscript.parameters["testbed_devices"]`, not a real device connection, and skip cleanly when it's empty.
2. **Stdlib-only sync HTTP** — `libs/mycelium_api.py` uses `urllib`, zero extra dependency.
3. **`uid` over class name for identity** — testcase `uid`s (unprefixed for the PR suite, `nightly_*`/`canary_*` for the others) are what CI logs and datafiles reference; class names can be re-aliased per suite without breaking that.
4. **Owned-room tracking** — `testscript.parameters["owned_rooms"]` is the only thing `MyceliumCommonCleanup` trusts; a testcase that creates a room outside of it leaks state across runs.
5. **The canary suite never blocks** — `canary_job.py`/`weekly-e2e.yaml` treat every failure as informational (`max_failures: 0`, `continue-on-error: true`). It exists to catch live-LLM compatibility drift, not to gate merges.
6. **Timeouts sized against the run's step count** — see pattern 7 above. A budget smaller than the steps a scenario takes reads as a hang (`terminal=None`) rather than as the outcome the scenario was built for.
