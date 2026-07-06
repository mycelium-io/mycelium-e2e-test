# matrix-mcp-server

MCP server that exposes Matrix client-server API operations as tools. Designed
for testing multi-agent flows where Cursor agents participate in Matrix rooms
alongside OpenClaw agents in the Mycelium / IOC-CFN pipeline.

## Tools

| Tool | Description |
|------|-------------|
| `whoami` | Return the authenticated Matrix user ID |
| `resolve_alias` | Resolve a room alias to its room ID |
| `send_message` | Send a message to a room |
| `read_messages` | Read recent messages from a room |
| `send_reaction` | React to a message |
| `list_rooms` | List joined rooms |
| `create_room` | Create a new room |
| `join_room` | Join a room |
| `invite_user` | Invite a user to a room |
| `get_room_members` | List members of a room |

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd tools/matrix-mcp-server

# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env — set MATRIX_ACCESS_TOKEN
```

### Getting an access token

If the E2E Docker Compose stack is running (`docker compose -f infra/compose.e2e.yaml up -d`),
get a token for the `main` agent user:

```bash
curl -s -X POST http://localhost:8008/_matrix/client/v3/login \
  -H 'Content-Type: application/json' \
  -d '{"type":"m.login.password","user":"main","password":"main-pass"}' \
  | python3 -m json.tool
```

Copy the `access_token` value into your `.env`.

## Running standalone

```bash
uv run python -m matrix_mcp.server
```

The server communicates over stdio (JSON-RPC). It exits with an error if
`MATRIX_HOMESERVER` or `MATRIX_ACCESS_TOKEN` are not set.

## Cursor integration

This workspace ships a `.cursor/mcp.json` that registers the server
automatically. After cloning, set the `MATRIX_ACCESS_TOKEN` value in
`.cursor/mcp.json` (or in `tools/matrix-mcp-server/.env`) and reload
MCP servers from the Cursor settings panel.

## Usage examples

Use the MCP tools from Cursor after configuring `.cursor/mcp.json`, or call
the Matrix client helpers in `libs/matrix_client.py` from pyATS testcases.

## Architecture

```
Cursor Agent ──stdio──> matrix-mcp-server ──HTTP──> Synapse (:8008)
                                                       ↑
OpenClaw agents (main, selina-agent, claire-agent) ─────┘
         │
    Mycelium hooks
         │
    IOC/CFN Stack (:9000 / :9002)
```
