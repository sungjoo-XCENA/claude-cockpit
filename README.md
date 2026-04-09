# claude-cockpit

Real-time Claude Code workspace monitor. A local web dashboard that gives you a single-screen overview of all your Claude Code sessions, instructions, skills, agents, connectors, hooks, plugins, forks, and projects.

> **Read-only by default** — the dashboard reads from `~/.claude/` and never modifies files unless you explicitly click a Delete button.

## Quick Start

```bash
python3 server.py
```

Open `http://localhost:8080` in your browser. That's it.

- Python 3.9+ required (stdlib only, zero external dependencies)
- Bind to all interfaces by default (`0.0.0.0:8080`), accessible from other machines on the same network

## Why

If you run multiple Claude Code sessions across different repos, you've probably experienced:

- **"Which terminal is doing what?"** — hard to track active sessions
- **"Did I set up CLAUDE.md for this repo?"** — instructions scattered across projects
- **"Where was that conversation I forked?"** — fork history buried in jsonl files
- **"Is my context window getting full?"** — no visibility without typing `/context`

Claude Cockpit solves this by scanning `~/.claude/` and presenting everything in one dashboard.

## Features

### Overview (Home)

The Overview tab is a monitoring dashboard with draggable cards:

- **Active Sessions** — count of running Claude processes
- **Today's Activity** — commands executed today
- **Harness Score** — workspace setup completeness (0–100)
- **Components** — count of skills, agents, hooks, connectors, plugins
- **Sessions** — running sessions with name, project, status
- **Alerts** — warnings for CLAUDE.md line count, missing instructions, context health
- **Instructions** — global + per-project CLAUDE.md status with line counts
- **Projects** — project summary with first/latest prompt
- **Forks** — recent conversation branches with resume commands
- **Plugins** — installed plugins with version and install date
- **Skills / Agents / Connectors / Hooks** — quick preview of all configured items

All overview cards are **draggable** — reorder them to your preference. Order is saved in localStorage.

### Sessions Tab

- **Active Sessions** — click to expand **Context X-ray**:
  - Real token usage from `cache_read_input_tokens` (not estimation)
  - Breakdown: System, Memory, Agents, Skills, Messages, Free space, Autocompact buffer
  - Compact history and recommendations (`/compact`, `/handoff`)
- **Search** — full-text search across all conversation history
- **Session History** — all past sessions with slug name, project, message count, fork count
- **Click to copy** `claude --resume --session-id <id>` for quick resume
- **Delete** old sessions to clean up

### Instructions Tab

- **Global** section — `~/.claude/CLAUDE.md` with line count and content viewer
- **Projects** section — grouped by project, showing all CLAUDE.md files in the tree
  - Config CLAUDE.md (`~/.claude/projects/<path>/CLAUDE.md`)
  - Local CLAUDE.md files (in repo subdirectories)
  - Line count warnings (over 200 lines)
  - Click project to expand, click "View content" to see file contents

### Projects Tab

Per-project management status:

- CLAUDE.md presence (config + local)
- Memory file count
- Settings status
- Session count
- First prompt / Latest prompt
- **Delete** unused projects

### Forks Tab

- All conversation fork points extracted from session jsonl files
- Shows the user's message at each fork point as an auto-label
- Click to copy resume command
- **Delete** old fork sessions

### Skills Tab

- **USER** section — user-created skills with delete button
- **PLUGIN** section — plugin-provided skills (no delete)
- Click to expand full SKILL.md content

### Agents Tab

- **USER** section — user-created agents with model badge, delete button
- **PLUGIN** section — plugin-provided agents with model/tools info
- Click to expand full agent definition

### Connectors Tab

- **Local MCP servers** from `.mcp.json` files
- **Cloud MCP servers** (e.g., Atlassian/Jira) detected from session tool usage
- Tool count per server
- Click to expand full tool list

### Hooks Tab

- **USER** section — hooks from `settings.json` with delete button
- **PLUGIN** section — hooks from plugin `hooks/hooks.json` files
- Shows event, type, command, matcher, timeout, and all configured fields
- Click to expand full details

### Plugins Tab

- Installed plugins with version, install date, git SHA, install path
- Click to expand associated skills, agents, and connectors
- Active/Inactive status from `enabledPlugins`

## Real-time Updates

- **SSE (Server-Sent Events)** pushes session updates every 5 seconds
- **Alerts** refresh every 30 seconds
- Active tab is preserved across page refreshes

## Data Sources

All data is read from `~/.claude/`:

| Data | Source |
|------|--------|
| Sessions | `ps aux` + `~/.claude/sessions/*.json` |
| Instructions | `~/.claude/CLAUDE.md` + `~/.claude/projects/*/CLAUDE.md` + repo CLAUDE.md files |
| Skills | `~/.claude/skills/` + plugin cache |
| Agents | `~/.claude/agents/` + plugin cache |
| Connectors | `.mcp.json` files + session jsonl MCP tool detection |
| Hooks | `~/.claude/settings.json` + plugin `hooks/hooks.json` |
| Plugins | `~/.claude/plugins/installed_plugins.json` |
| Forks | Session jsonl parent-child branching analysis |
| Projects | `~/.claude/projects/*/` directory scan |
| Activity | `~/.claude/history.jsonl` |
| Context X-ray | `cache_read_input_tokens` from session jsonl |
| Session names | `customTitle` (from `/rename`) or auto-generated `slug` |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Harness score (0-100) |
| GET | `/api/sessions` | Active Claude processes |
| GET | `/api/activity` | Today's command count + recent prompts |
| GET | `/api/instructions` | Global + per-project CLAUDE.md with content |
| GET | `/api/skills` | All skills with content |
| GET | `/api/agents` | All agents with content |
| GET | `/api/connectors` | Local + cloud MCP servers with tools |
| GET | `/api/hooks` | User + plugin hooks |
| GET | `/api/plugins` | Installed plugins with details |
| GET | `/api/forks` | Fork points from session history |
| GET | `/api/projects-summary` | Per-project command history |
| GET | `/api/project-status` | Per-project config status |
| GET | `/api/alerts` | Aggregated warnings |
| GET | `/api/session-detail` | All sessions with slug, messages, forks |
| GET | `/api/session-search?q=` | Search conversation history |
| GET | `/api/session-xray?id=` | Context X-ray for a session |
| GET | `/sse/live` | Server-Sent Events stream |
| POST | `/api/delete-project?dir=` | Delete a project |
| POST | `/api/delete-skill?name=` | Delete a user skill |
| POST | `/api/delete-agent?name=` | Delete a user agent |
| POST | `/api/delete-hook?event=&index=` | Delete a hook |
| POST | `/api/delete-session?id=` | Delete a session |
| POST | `/api/delete-fork?id=` | Delete a fork session |

## Network Access

By default, the server binds to `0.0.0.0:8080`, making it accessible from other machines on the local network:

```
http://<your-ip>:8080
```

## Tech Stack

- **Backend**: Python 3.9+ (stdlib only — `http.server`, `json`, `subprocess`, `pathlib`)
- **Frontend**: Single HTML file with embedded CSS + JS (no build step, no dependencies)
- **Real-time**: Server-Sent Events (SSE)
- **Theme**: Modern dark (zinc-950 base), sidebar + tab layout
- **Storage**: localStorage for card order and active tab persistence

## License

MIT
