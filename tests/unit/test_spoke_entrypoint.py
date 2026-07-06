"""Contract tests for ``infra/scripts/spoke-entrypoint.sh``.

We don't execute the full entrypoint in CI (it needs Matrix tokens, supervisord,
etc.). These tests pin the shell/embedded-node contract so hub-role support and
mycelium plugin gating can't drift silently.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "infra" / "scripts" / "spoke-entrypoint.sh"


def _script() -> str:
    return _ENTRYPOINT.read_text()


def test_spoke_entrypoint_defines_hub_openclaw_agents() -> None:
    text = _script()
    assert 'hub)' in text
    assert 'AGENTS="agent-alpha agent-beta agent-gamma agent-delta"' in text


def test_spoke_entrypoint_keeps_spoke_agent_sets() -> None:
    text = _script()
    assert 'spoke1) AGENTS="claire-agent"' in text
    assert 'spoke2) AGENTS="oclw5-agent"' in text


def test_spoke_entrypoint_gates_mycelium_room_on_plugin_presence() -> None:
    text = _script()
    assert "extensions', 'mycelium')" in text
    assert "omit mycelium-room channel" in text.lower() or "omitting mycelium-room channel" in text
    assert "'mycelium-room':" in text
    assert "hasMycelium ? ['litellm', 'matrix', 'mycelium']" in text
