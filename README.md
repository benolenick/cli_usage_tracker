# AI Usage Tracker

A desktop dashboard that tracks your usage across multiple AI coding tools — **Codex**, **Claude**, and **Gemini** — in a single window. Supports multiple instances of each agent (e.g. claude + claude2, gemini + gemini2).

![Python](https://img.shields.io/badge/python-3.10+-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Shows remaining quota, usage percentages, and reset timers for each AI tool
- **Multi-agent support**: track multiple accounts per provider (Claude 1 + Claude 2, etc.)
- Auto-detects all installed CLI agents on first launch
- Per-model breakdowns (Gemini models, Claude Opus/Sonnet/Haiku costs, Codex regular/Spark)
- Progress bars showing your actual usage pace vs. even-pacing target
- Bootstrap verification for newly detected agents
- Scrollable card area when tracking many agents
- Auto-refreshes hourly (configurable)
- Built-in ephemeral password helper for securely passing credentials to AI agents

## Screenshot

*Screenshot coming soon*

## Requirements

- **Python 3.10+** with tkinter (included in standard Windows Python install)
- At least one of these AI tools installed:
  - [Codex CLI](https://github.com/openai/codex) (OpenAI)
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (Google)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/ai-usage-tracker.git
cd ai-usage-tracker
python ai_usage_tracker.py
```

Or on Windows, double-click `run.bat`.

### Optional dependency

For more reliable Codex/Gemini status capture on Windows:
```bash
pip install pywinpty
```

## First Run

On first launch, the tracker auto-detects all installed AI agents:

1. It scans for `claude`, `claude2`, `claude3`, `codex`, `codex2`, `gemini`, `gemini2`, etc.
2. It also checks `~/.claude*/` directories and npm global paths
3. Each detected agent is shown with an editable label (e.g. "Claude (Main)", "Gemini 2")
4. Check/uncheck agents to enable or disable tracking
5. Use **+ Add manually** to add agents not auto-detected
6. Click **Start** — the dashboard shows a card per enabled agent

Unverified agents show a **Verify Now** button that runs a quick command to confirm the agent works.

## How it works

| Provider | Data Source | What you see |
|----------|-----------|-------------|
| **Codex** | Console buffer reads `/status` from Codex TUI | 5h and weekly remaining %, Spark tier %, reset times |
| **Claude** | Local telemetry files (`~/.claude/telemetry/`) | Per-model cost breakdown (Opus/Sonnet/Haiku), 7-day totals |
| **Gemini** | Console buffer reads `/stats` from Gemini TUI | Per-model remaining % and reset timers for all Gemini models |

Each provider also has fallback methods (headless JSON, web scraping via Pinchtab) if the primary method fails.

## Configuration

Settings are saved to `config.json`. Key options:

| Setting | Default | Description |
|---------|---------|-------------|
| `agents` | `[]` | List of agent configs (auto-populated on first run) |
| `refresh_minutes` | `60` | How often to auto-refresh |
| `claude_reset_weekday` | `6` (Sunday) | Day Claude usage resets (0=Mon, 6=Sun) |
| `claude_reset_hour` | `17` | Hour Claude usage resets (local time) |
| `gemini_stats_mode` | `"auto"` | Gemini /stats mode: auto, session, model, or tools |

### Agent config schema

Each entry in the `agents` list:

```json
{
  "id": "claude_1",
  "type": "claude",
  "label": "Claude (Main)",
  "binary": "claude",
  "data_dir": "~/.claude",
  "telemetry_dir": "~/.claude/telemetry",
  "sessions_dir": "~/.claude/sessions",
  "enabled": true,
  "verified": true,
  "claude_last_known_pct": 42.5,
  "claude_last_known_time": "2026-03-04T10:00:00"
}
```

### Migrating from old config

If you have an old `config.json` with flat keys (`codex_cmd`, `gemini_cmd`, `enabled_providers`), the tracker automatically migrates to the new `agents` list format on first load.

## Password Helper

The built-in password helper lets you securely pass credentials to AI agents:

1. Type a password → click **Save/Copy**
2. A random-named ephemeral file is created with the secret (wrapped in "do not repeat" instructions)
3. A clipboard message is generated pointing the agent to the file
4. The file auto-deletes after the configured TTL

Click the **?** button in the app for detailed instructions.

## License

MIT

---

*Built with Python and tkinter. No external API calls — all data is read from local CLI tools and telemetry files.*
