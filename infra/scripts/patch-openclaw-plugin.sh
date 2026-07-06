#!/usr/bin/env bash
# Infra-side patches for the staged OpenClaw mycelium-room plugin in compose labs.
#
# Keeps product mycelium-cli unchanged while improving E2E agent compliance:
#   - tick instructions must demand a shell command (prose-only is invalid)
#   - requireMention=false so CognitiveEngine ticks are always addressed

set -euo pipefail

CONFIG_DIR="${OPENCLAW_STATE_DIR:-${HOME}/.openclaw}"
ROUTE="${CONFIG_DIR}/extensions/mycelium/dist/src/channel/route.js"
CONFIG_PATH="${CONFIG_DIR}/openclaw.json"

patch_route() {
    if [ ! -f "$ROUTE" ]; then
        echo "[patch-openclaw-plugin] no route.js at ${ROUTE} — skipping" >&2
        return 0
    fi
    if grep -q 'MANDATORY: run exactly one mycelium negotiate' "$ROUTE"; then
        echo "[patch-openclaw-plugin] route.js already patched"
        return 0
    fi

    python3 - "$ROUTE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

text, n = re.subn(
    r'"Explain your reasoning before running the command\.[^"]*"',
    (
        '"MANDATORY: run exactly one mycelium negotiate shell command below. '
        'Prose-only accept/reject does NOT register with CognitiveEngine.",\n'
        '        "Brief reasoning is fine, but you MUST execute the command in a shell before replying.",\n'
        '        "Walking away with no agreement is valid — keep rejecting until the session ends if your hard constraints cannot be met."'
    ),
    text,
    count=1,
)
if n != 1:
    raise SystemExit(f"[patch-openclaw-plugin] explain line not found in {path}")

needle = "return [\n        roundHeader,"
insert = (
    "return [\n"
    '        "MANDATORY: run exactly one mycelium negotiate shell command. Prose-only replies are invalid.",\n'
    "        roundHeader,"
)
if needle not in text:
    raise SystemExit(f"[patch-openclaw-plugin] return header not found in {path}")
text = text.replace(needle, insert, 1)
path.write_text(text)
print(f"[patch-openclaw-plugin] patched {path}")
PY
}

patch_config() {
    if [ ! -f "$CONFIG_PATH" ]; then
        return 0
    fi
    python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
cfg = json.loads(path.read_text())
channel = cfg.setdefault("channels", {}).setdefault("mycelium-room", {})
if channel.get("requireMention") is False:
    print(f"[patch-openclaw-plugin] {path} already has requireMention=false")
else:
    channel["requireMention"] = False
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[patch-openclaw-plugin] set requireMention=false in {path}")
PY
}

patch_route
patch_config
