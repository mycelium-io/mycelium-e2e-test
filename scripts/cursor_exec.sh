#!/usr/bin/env bash
# cursor_exec.sh — exec driver for mycelium await --loop --exec
#
# Receives the turn JSON on stdin; env vars set by await:
#   MYCELIUM_ROOM    — room name
#   MYCELIUM_HANDLE  — this agent's handle
#   MYCELIUM_PROMPT  — the prompt for this turn (a conductor step, e.g.
#                      "concord · score · turn 3 of 20 · architect …")
#   MYCELIUM_TASK    — the row whose thread the turn was asked in, empty for the room
#
# Runs cursor-agent -p with the prompt, then posts its reply via respond, in
# the task's thread when the turn was asked in one. Each concord step says
# the exact shape its reply takes (`bad: N - why`, `<option>` then `why: …`,
# `A (…): N - why`), so the prompt is passed through whole.

set -euo pipefail

ROOM="${MYCELIUM_ROOM:-}"
HANDLE="${MYCELIUM_HANDLE:-}"
PROMPT="${MYCELIUM_PROMPT:-}"
TASK="${MYCELIUM_TASK:-}"
WORKSPACE="${CURSOR_WORKSPACE:-}"

if [[ -z "$ROOM" || -z "$HANDLE" ]]; then
    echo "cursor_exec: missing MYCELIUM_ROOM or MYCELIUM_HANDLE" >&2
    exit 1
fi

if [[ -z "$PROMPT" ]]; then
    exit 0
fi

SYSTEM_PROMPT="You are ${HANDLE}, one member of a negotiation the Mycelium conductor runs.
Answer the conductor's prompt below for your own role, in exactly the format it asks for:
the same line labels, one line each where it says so, with your real reasons.
Do NOT include markdown, @-mentions, or preamble.

Conductor's prompt:
${PROMPT}"

CURSOR_ARGS=("--output-format" "json" "--force" "--approve-mcps")
if [[ -n "$WORKSPACE" ]]; then
    CURSOR_ARGS+=("--workspace" "$WORKSPACE")
fi

RAW=$(cursor-agent -p "${CURSOR_ARGS[@]}" -- "$SYSTEM_PROMPT" 2>/dev/null || echo "")

REPLY=$(echo "$RAW" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    text = (
        data.get('result') or
        data.get('text') or
        data.get('message') or
        data.get('content') or ''
    )
    print(str(text).strip())
except Exception:
    pass
" 2>/dev/null)

if [[ -z "$REPLY" ]]; then
    # No stand-in reply: the conductor reads each step's reply for ratings or
    # an option, so a made-up one would be counted as this agent's position.
    # Unanswered, the step records the agent as silent.
    echo "cursor_exec: cursor-agent produced no reply for @${HANDLE}" >&2
    exit 0
fi

ARGS=(--room "$ROOM" --handle "$HANDLE")
if [[ -n "$TASK" ]]; then
    ARGS+=(--task "$TASK")
fi
mycelium respond "${ARGS[@]}" "$REPLY"
