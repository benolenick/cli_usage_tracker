#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import tkinter as tk
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib import request as urlrequest

try:
    from winpty import PtyProcess  # type: ignore
except Exception:
    PtyProcess = None

import tempfile

APP_DIR = Path(__file__).resolve().parent
PS1_SCRIPT = APP_DIR / "cli_status_reader.ps1"

# Native binary paths for console buffer reading
CODEX_NATIVE_EXE = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe"
GEMINI_NODE_ENTRY = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@google" / "gemini-cli" / "dist" / "index.js"
NODE_EXE = Path(r"C:\Program Files\nodejs\node.exe")
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "agents": [],  # list of agent dicts; populated by detect_agents() on first run
    "refresh_minutes": 60,
    "pinchtab_url": "http://localhost:9875",
    "page_wait_seconds": 3,
    "gemini_stats_mode": "auto",
    "claude_reset_weekday": 6,  # 0=Mon, 6=Sun
    "claude_reset_hour": 17,    # 5pm local time
    "password_file_path": str(Path.home() / "Desktop" / "note.txt"),
    "password_ephemeral_mode": True,
    "password_ttl_seconds": 30,
    "password_copy_template": "Secret is in {path}. Read it once, do not repeat it, and delete the file after use.",
    "password_clipboard_clear_seconds": 45,
    "usage_scraper_script": "",
    "providers": {
        "claude": {"url": "https://claude.ai/settings/usage"},
        "gemini": {"url": "https://aistudio.google.com/billing"},
    },
}

AGENT_TYPES = ("claude", "codex", "gemini")

LOGIN_INDICATORS = [
    "please log in",
    "sign in",
    "authentication required",
    "log in to",
    "you need to sign in",
]

ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def _migrate_old_config(cfg: dict) -> list[dict]:
    """Convert old flat-key config into agents list (one-time migration)."""
    agents = []
    old_enabled = cfg.get("enabled_providers", "auto")
    if isinstance(old_enabled, str) and old_enabled == "auto":
        enabled_set = set(AGENT_TYPES)
    elif isinstance(old_enabled, list):
        enabled_set = set(old_enabled)
    else:
        enabled_set = set(AGENT_TYPES)

    if "codex" in enabled_set or cfg.get("codex_cmd"):
        agents.append({
            "id": "codex_1",
            "type": "codex",
            "label": "Codex",
            "binary": cfg.get("codex_cmd", "codex"),
            "data_dir": None,
            "enabled": "codex" in enabled_set,
            "verified": True,
        })
    if "claude" in enabled_set or cfg.get("claude_telemetry_dir"):
        agents.append({
            "id": "claude_1",
            "type": "claude",
            "label": "Claude",
            "binary": "claude",
            "data_dir": str(Path(cfg.get("claude_telemetry_dir", str(Path.home() / ".claude" / "telemetry"))).parent),
            "sessions_dir": cfg.get("claude_sessions_dir", str(Path.home() / ".claude" / "sessions")),
            "telemetry_dir": cfg.get("claude_telemetry_dir", str(Path.home() / ".claude" / "telemetry")),
            "enabled": "claude" in enabled_set,
            "verified": True,
            "claude_last_known_pct": cfg.get("claude_last_known_pct"),
            "claude_last_known_time": cfg.get("claude_last_known_time"),
        })
    if "gemini" in enabled_set or cfg.get("gemini_cmd"):
        agents.append({
            "id": "gemini_1",
            "type": "gemini",
            "label": "Gemini",
            "binary": "gemini",
            "data_dir": None,
            "gemini_cmd": cfg.get("gemini_cmd", "gemini"),
            "enabled": "gemini" in enabled_set,
            "verified": True,
        })
    return agents


def ensure_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return DEFAULT_CONFIG.copy()
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = DEFAULT_CONFIG.copy()

    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    merged["providers"] = DEFAULT_CONFIG["providers"].copy()
    merged["providers"].update(cfg.get("providers", {}))
    if merged.get("gemini_stats_mode") not in ("auto", "session", "model", "tools"):
        merged["gemini_stats_mode"] = "auto"
    try:
        ttl = int(merged.get("password_ttl_seconds", 30))
        if ttl < 1:
            ttl = 30
        merged["password_ttl_seconds"] = ttl
    except Exception:
        merged["password_ttl_seconds"] = 30
    try:
        cttl = int(merged.get("password_clipboard_clear_seconds", 45))
        if cttl < 0:
            cttl = 45
        merged["password_clipboard_clear_seconds"] = cttl
    except Exception:
        merged["password_clipboard_clear_seconds"] = 45
    merged["password_ephemeral_mode"] = bool(merged.get("password_ephemeral_mode", True))

    # Migrate old flat-key config to agents list
    if "agents" not in merged or not isinstance(merged.get("agents"), list):
        if cfg.get("enabled_providers") is not None or cfg.get("codex_cmd") or cfg.get("gemini_cmd") or cfg.get("claude_telemetry_dir"):
            merged["agents"] = _migrate_old_config(cfg)
        else:
            merged["agents"] = []
    # Clean up old keys after migration
    for old_key in ("enabled_providers", "codex_cmd", "gemini_cmd", "claude_sessions_dir",
                     "claude_telemetry_dir", "claude_last_known_pct", "claude_last_known_time"):
        merged.pop(old_key, None)

    CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def detect_providers() -> list[str]:
    """Legacy wrapper for backwards compat with tests."""
    found = []
    if CODEX_NATIVE_EXE.exists() or shutil.which("codex"):
        found.append("codex")
    if (Path.home() / ".claude").is_dir():
        found.append("claude")
    if GEMINI_NODE_ENTRY.exists() or shutil.which("gemini"):
        found.append("gemini")
    return found


def detect_agents() -> list[dict]:
    """Scan system for all CLI agent instances. Returns list of agent dicts."""
    agents = []
    seen_ids = set()

    def _add(agent_type: str, binary: str, data_dir: str | None = None, **extra):
        # Generate unique id
        count = sum(1 for a in agents if a["type"] == agent_type) + 1
        aid = f"{agent_type}_{count}"
        if aid in seen_ids:
            aid = f"{agent_type}_{count}_{len(agents)}"
        seen_ids.add(aid)
        label = f"{agent_type.title()}" + (f" {count}" if count > 1 else "")
        entry = {
            "id": aid,
            "type": agent_type,
            "label": label,
            "binary": binary,
            "data_dir": data_dir,
            "enabled": True,
            "verified": False,
        }
        entry.update(extra)
        agents.append(entry)

    # --- Claude instances ---
    for suffix in ("", "2", "3", "4", "5"):
        name = f"claude{suffix}"
        found = shutil.which(name)
        if found:
            data_dir = str(Path.home() / f".claude{suffix}")
            tel_dir = str(Path(data_dir) / "telemetry")
            ses_dir = str(Path(data_dir) / "sessions")
            _add("claude", name, data_dir=data_dir,
                 telemetry_dir=tel_dir, sessions_dir=ses_dir)
    # Also glob ~/.claude*/ directories for data dirs without binaries
    for d in sorted(Path.home().glob(".claude*")):
        if d.is_dir() and (d / "telemetry").is_dir():
            suffix = d.name.replace(".claude", "") or ""
            binary = f"claude{suffix}" if suffix else "claude"
            # Skip if already found via which()
            if any(a["type"] == "claude" and a.get("data_dir") == str(d) for a in agents):
                continue
            tel_dir = str(d / "telemetry")
            ses_dir = str(d / "sessions")
            _add("claude", binary, data_dir=str(d),
                 telemetry_dir=tel_dir, sessions_dir=ses_dir)

    # --- Codex instances ---
    if CODEX_NATIVE_EXE.exists():
        _add("codex", "codex")
    else:
        for suffix in ("", "2", "3"):
            name = f"codex{suffix}"
            if shutil.which(name):
                _add("codex", name)
    # Check npm paths
    npm_codex = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
    if npm_codex.exists() and not any(a["type"] == "codex" for a in agents):
        _add("codex", str(npm_codex))

    # --- Gemini instances ---
    for suffix in ("", "2", "3"):
        name = f"gemini{suffix}"
        found = shutil.which(name)
        if found:
            _add("gemini", name, gemini_cmd=found)
    # Check npm paths
    npm_gemini = Path.home() / "AppData" / "Roaming" / "npm" / "gemini.cmd"
    if npm_gemini.exists() and not any(a["type"] == "gemini" for a in agents):
        _add("gemini", "gemini", gemini_cmd=str(npm_gemini))
    if GEMINI_NODE_ENTRY.exists() and not any(a["type"] == "gemini" for a in agents):
        _add("gemini", "gemini", gemini_cmd=str(GEMINI_NODE_ENTRY))

    return agents


def get_enabled_agents(cfg: dict) -> list[dict]:
    """Return enabled agents from config."""
    return [a for a in cfg.get("agents", []) if a.get("enabled", True)]


class PinchtabClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> str:
        url = self.base_url + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(url=url, method=method, data=data, headers=headers)
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def health(self) -> bool:
        try:
            self._call("GET", "/health")
            return True
        except Exception:
            return False

    def navigate(self, url: str) -> None:
        self._call("POST", "/navigate", {"url": url})

    def snapshot(self) -> str:
        return self._call("GET", "/snapshot")

    def text(self) -> str:
        return self._call("GET", "/text")


def resolve_pinchtab_url(preferred: str | None) -> str:
    candidates = []
    p = (preferred or "").strip()
    if p:
        candidates.append(p)
    for c in ("http://localhost:9875", "http://localhost:9867"):
        if c not in candidates:
            candidates.append(c)
    for base in candidates:
        try:
            req = urlrequest.Request(base.rstrip("/") + "/health", method="GET")
            with urlrequest.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return base
        except Exception:
            continue
    return p or "http://localhost:9875"


def detect_login_wall(text: str) -> bool:
    lower = text.lower()
    return any(x in lower for x in LOGIN_INDICATORS)


def parse_snapshot_nodes(snapshot_text: str) -> list[dict]:
    try:
        obj = json.loads(snapshot_text)
        return obj.get("nodes", []) if isinstance(obj, dict) else []
    except Exception:
        return []


BOX_CHARS = "│╭╮╰╯─┤├┬┴┼▀▄▌▐░▒▓█║╗╝╚╔═╬╩╦╠╣"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", OSC_RE.sub("", text))


def strip_box_chars(text: str) -> str:
    """Strip box-drawing characters and clean up lines from console buffer output."""
    lines = []
    for line in text.splitlines():
        cleaned = line
        for ch in BOX_CHARS:
            cleaned = cleaned.replace(ch, " ")
        cleaned = cleaned.strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _looks_like_prompt(text: str) -> bool:
    lines = [ln.rstrip() for ln in strip_ansi(text).splitlines() if ln.strip()]
    if not lines:
        return False
    tail = lines[-1]
    if re.search(r"[>\$❯]\s*$", tail):
        return True
    lower = "\n".join(lines[-3:]).lower()
    if "what are we working on today?" in lower:
        return True
    return False


def parse_codex_status(raw: str) -> dict:
    if not raw:
        return {"status": "error", "error": "No Codex output"}
    text = strip_box_chars(strip_ansi(raw))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {
        "status": "ok",
        "model": None,
        "five_hour_left_pct": None,
        "five_hour_resets": None,
        "weekly_left_pct": None,
        "weekly_resets": None,
        "spark_five_hour_left_pct": None,
        "spark_weekly_left_pct": None,
    }

    m_model = re.search(r"Model\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if m_model:
        # Clean model string: remove trailing TUI artifacts like "/model to change"
        model_str = m_model.group(1).strip()
        model_str = re.split(r"\s{2,}|/model", model_str)[0].strip()
        out["model"] = model_str

    in_spark = False
    for i, line in enumerate(lines):
        if re.search(r"Spark\s+limit", line, flags=re.IGNORECASE):
            in_spark = True
        if re.search(r"5h\s+limit\s*:", line, flags=re.IGNORECASE):
            m_pct = re.search(r"(\d+)\s*%\s*left", line, flags=re.IGNORECASE)
            m_reset = re.search(r"resets\s+(.+?)\)?$", line, flags=re.IGNORECASE)
            if in_spark:
                if m_pct:
                    out["spark_five_hour_left_pct"] = int(m_pct.group(1))
            else:
                if m_pct and out["five_hour_left_pct"] is None:
                    out["five_hour_left_pct"] = int(m_pct.group(1))
                if m_reset and out["five_hour_resets"] is None:
                    out["five_hour_resets"] = m_reset.group(1).strip()
        if re.search(r"Weekly\s+limit\s*:", line, flags=re.IGNORECASE):
            m_pct = re.search(r"(\d+)\s*%\s*left", line, flags=re.IGNORECASE)
            m_reset = re.search(r"resets\s+(.+?)\)?$", line, flags=re.IGNORECASE)
            if in_spark:
                if m_pct:
                    out["spark_weekly_left_pct"] = int(m_pct.group(1))
            else:
                if m_pct and out["weekly_left_pct"] is None:
                    out["weekly_left_pct"] = int(m_pct.group(1))
                if m_reset and out["weekly_resets"] is None:
                    out["weekly_resets"] = m_reset.group(1).strip()
                elif out["weekly_resets"] is None and i + 1 < len(lines):
                    m2 = re.search(r"resets\s+(.+?)\)?$", lines[i + 1], flags=re.IGNORECASE)
                    if m2:
                        out["weekly_resets"] = m2.group(1).strip()
    has_signal = any(
        out.get(k) is not None
        for k in (
            "model",
            "five_hour_left_pct",
            "five_hour_resets",
            "weekly_left_pct",
            "weekly_resets",
            "spark_five_hour_left_pct",
            "spark_weekly_left_pct",
        )
    )
    if not has_signal:
        return {
            "status": "error",
            "error": "Could not parse Codex /status output",
            "raw_summary": " | ".join(lines[:4]) if lines else None,
        }
    return out


def parse_gemini_stats(raw: str) -> dict:
    if not raw:
        return {"status": "error", "error": "No Gemini output"}
    text = strip_box_chars(strip_ansi(raw))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {
        "status": "ok",
        "source": "cli_stats",
        "model": None,
        "five_hour_left_pct": None,
        "daily_left_pct": None,
        "weekly_left_pct": None,
        "monthly_left_pct": None,
        "resets": None,
        "estimated_spend_usd": None,
        "limit_usd": None,
        "remaining_usd": None,
        "per_model_usage": [],
        "tier": None,
        "auth_email": None,
        "raw_summary": " | ".join(lines[:4]) if lines else None,
    }

    m_model = re.search(r"Model\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if m_model:
        out["model"] = m_model.group(1).strip()

    # Parse tier
    m_tier = re.search(r"Tier\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if m_tier:
        out["tier"] = m_tier.group(1).strip()

    # Parse auth email
    m_auth = re.search(r"Auth\s+Method\s*:\s*.*?\(([^)]+)\)", text, flags=re.IGNORECASE)
    if m_auth:
        out["auth_email"] = m_auth.group(1).strip()

    # Parse per-model usage rows (e.g. "gemini-2.5-flash    -   88.0% resets in 19h 20m")
    model_re = re.compile(r"(gemini[\w.-]+)\s+.*?(\d+(?:\.\d+)?)\s*%\s*resets?\s+in\s+(.+)", re.IGNORECASE)
    min_remaining_pct = None
    for line in lines:
        m_model_row = model_re.search(line)
        if m_model_row:
            model_name = m_model_row.group(1)
            pct = float(m_model_row.group(2))
            resets_in = m_model_row.group(3).strip()
            out["per_model_usage"].append({"model": model_name, "remaining_pct": pct, "resets_in": resets_in})
            if min_remaining_pct is None or pct < min_remaining_pct:
                min_remaining_pct = pct

    # Set the overall remaining pct as the minimum across models (most constrained)
    if min_remaining_pct is not None:
        out["overall_remaining_pct"] = min_remaining_pct

    for line in lines:
        lower = line.lower()
        m_left = re.search(r"(\d+(?:\.\d+)?)\s*%\s*left", lower)
        if m_left:
            pct = float(m_left.group(1))
            if "5h" in lower and out["five_hour_left_pct"] is None:
                out["five_hour_left_pct"] = pct
            elif "daily" in lower and out["daily_left_pct"] is None:
                out["daily_left_pct"] = pct
            elif "weekly" in lower and out["weekly_left_pct"] is None:
                out["weekly_left_pct"] = pct
            elif "month" in lower and out["monthly_left_pct"] is None:
                out["monthly_left_pct"] = pct
        if "reset" in lower and out["resets"] is None and "resets in" not in lower:
            m_reset = re.search(r"resets?\s+(.+)", line, flags=re.IGNORECASE)
            if m_reset:
                out["resets"] = m_reset.group(1).strip()

    dollar_vals = []
    for m in re.finditer(r"\$([\d,]+(?:\.\d{1,2})?)", text):
        try:
            dollar_vals.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    if dollar_vals:
        out["estimated_spend_usd"] = dollar_vals[0]
        if len(dollar_vals) > 1:
            out["limit_usd"] = dollar_vals[1]
        if len(dollar_vals) > 2:
            out["remaining_usd"] = dollar_vals[2]

    has_signal = any(
        out.get(k) is not None
        for k in (
            "model",
            "five_hour_left_pct",
            "daily_left_pct",
            "weekly_left_pct",
            "monthly_left_pct",
            "estimated_spend_usd",
            "limit_usd",
            "remaining_usd",
            "overall_remaining_pct",
        )
    )
    if not has_signal:
        return {"status": "error", "error": "Could not parse Gemini /stats output", "raw_summary": out.get("raw_summary")}
    return out


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        t = value.strip().replace(",", "")
        if t.isdigit():
            return int(t)
    return 0


def _sum_usage_tokens(node: object) -> tuple[int, int]:
    input_total = 0
    output_total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            lkey = key.lower()
            if lkey in ("input_tokens", "prompt_tokens"):
                input_total += _as_int(value)
            elif lkey in ("output_tokens", "completion_tokens"):
                output_total += _as_int(value)
            elif lkey in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                input_total += _as_int(value)
            else:
                sub_in, sub_out = _sum_usage_tokens(value)
                input_total += sub_in
                output_total += sub_out
    elif isinstance(node, list):
        for item in node:
            sub_in, sub_out = _sum_usage_tokens(item)
            input_total += sub_in
            output_total += sub_out
    return input_total, output_total


def parse_claude_local_sessions(sessions_dir: str | None) -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(sessions_dir or "")))
    if not p.exists() or not p.is_dir():
        return {"status": "error", "error": f"Claude sessions dir not found: {p}"}

    total_in = 0
    total_out = 0
    week_in = 0
    week_out = 0
    line_count = 0
    file_count = 0
    now = datetime.now()
    week_cutoff = now - timedelta(days=7)

    try:
        files = list(p.rglob("*.jsonl"))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    for fp in files:
        file_count += 1
        try:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    line_count += 1
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    in_toks, out_toks = _sum_usage_tokens(obj)
                    if in_toks == 0 and out_toks == 0:
                        continue
                    total_in += in_toks
                    total_out += out_toks

                    ts = (
                        _parse_dt(obj.get("timestamp"))
                        or _parse_dt(obj.get("created_at"))
                        or _parse_dt(obj.get("time"))
                    )
                    if ts is None:
                        continue
                    ts_local = ts.replace(tzinfo=None) if ts.tzinfo else ts
                    if ts_local >= week_cutoff:
                        week_in += in_toks
                        week_out += out_toks
        except Exception:
            continue

    if total_in == 0 and total_out == 0:
        return {"status": "error", "error": "No Claude token usage found in local session files"}

    return {
        "status": "ok",
        "source": "local_sessions",
        "sessions_dir": str(p),
        "files_scanned": file_count,
        "lines_scanned": line_count,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "last_7d_input_tokens": week_in,
        "last_7d_output_tokens": week_out,
        "last_7d_total_tokens": week_in + week_out,
    }


def parse_claude_telemetry(telemetry_dir: str | None) -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(telemetry_dir or "")))
    if not p.exists() or not p.is_dir():
        return {"status": "error", "error": f"Claude telemetry dir not found: {p}"}

    latest_by_session = {}
    file_count = 0
    line_count = 0

    try:
        files = list(p.glob("1p_failed_events.*.json"))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    for fp in files:
        file_count += 1
        try:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line_count += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ed = obj.get("event_data", {})
                    if ed.get("event_name") != "tengu_exit":
                        continue
                    add_meta = ed.get("additional_metadata")
                    if not isinstance(add_meta, str):
                        continue
                    try:
                        meta = json.loads(add_meta)
                    except Exception:
                        continue
                    sid = meta.get("last_session_id")
                    if not sid:
                        continue
                    ts = _parse_dt(ed.get("client_timestamp")) or datetime.min
                    rec = {
                        "timestamp": ts,
                        "last_session_id": sid,
                        "input_tokens": _as_int(meta.get("last_session_total_input_tokens")),
                        "output_tokens": _as_int(meta.get("last_session_total_output_tokens")),
                        "cache_creation_input_tokens": _as_int(meta.get("last_session_total_cache_creation_input_tokens")),
                        "cache_read_input_tokens": _as_int(meta.get("last_session_total_cache_read_input_tokens")),
                    }
                    prev = latest_by_session.get(sid)
                    if not prev or rec["timestamp"] > prev["timestamp"]:
                        latest_by_session[sid] = rec
        except Exception:
            continue

    if not latest_by_session:
        return {"status": "error", "error": "No Claude telemetry usage records found"}

    records = sorted(latest_by_session.values(), key=lambda r: r["timestamp"])
    latest = records[-1]

    total_in = sum(r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"] for r in records)
    total_out = sum(r["output_tokens"] for r in records)
    latest_in = latest["input_tokens"] + latest["cache_creation_input_tokens"] + latest["cache_read_input_tokens"]
    latest_out = latest["output_tokens"]
    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    week_records = []
    for r in records:
        ts = r.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts_cmp = ts.replace(tzinfo=timezone.utc)
        else:
            ts_cmp = ts.astimezone(timezone.utc)
        if ts_cmp >= week_cutoff:
            week_records.append(r)
    week_in = sum(r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"] for r in week_records)
    week_out = sum(r["output_tokens"] for r in week_records)
    recent_sessions = []
    for r in reversed(records[-5:]):
        rin = r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"]
        rout = r["output_tokens"]
        ts = r["timestamp"]
        ts_text = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) and ts != datetime.min else "-"
        recent_sessions.append(
            {
                "session_id": r["last_session_id"],
                "timestamp": ts_text,
                "input_tokens": rin,
                "output_tokens": rout,
                "total_tokens": rin + rout,
            }
        )

    return {
        "status": "ok",
        "source": "telemetry_fallback",
        "telemetry_dir": str(p),
        "files_scanned": file_count,
        "lines_scanned": line_count,
        "sessions_count": len(records),
        "latest_session_id": latest["last_session_id"],
        "latest_session_input_tokens": latest_in,
        "latest_session_output_tokens": latest_out,
        "latest_session_total_tokens": latest_in + latest_out,
        "aggregate_input_tokens": total_in,
        "aggregate_output_tokens": total_out,
        "aggregate_total_tokens": total_in + total_out,
        "last_7d_input_tokens": week_in,
        "last_7d_output_tokens": week_out,
        "last_7d_total_tokens": week_in + week_out,
        "recent_sessions": recent_sessions,
    }


def run_console_status(cli_exe: str, command: str, cli_args: str = "", init_wait: int = 12, post_wait: int = 5, batch_input: bool = False) -> dict:
    """Spawn a CLI TUI headless, send a slash command, read the console screen buffer via PowerShell."""
    if os.name != "nt" or not PS1_SCRIPT.exists():
        return {"status": "error", "error": "Console buffer reader only works on Windows"}
    try:
        out_file = Path(tempfile.mktemp(suffix=".txt", prefix="cli_status_"))
        argv = [
            "powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(PS1_SCRIPT),
            "-CliExe", cli_exe,
            "-Command", command,
            "-InitWait", str(init_wait),
            "-PostWait", str(post_wait),
            "-OutFile", str(out_file),
        ]
        if batch_input:
            argv.append("-BatchInput")
        if cli_args:
            argv.insert(argv.index("-Command"), "-CliArgs")
            argv.insert(argv.index("-Command"), cli_args)
        # NOTE: Do NOT use CREATE_NO_WINDOW — it prevents the PS script from
        # attaching to child consoles and using WriteConsoleInput/VkKeyScan.
        # Instead, hide the PowerShell window via STARTUPINFO.
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=init_wait + post_wait + 20, startupinfo=si)
        if out_file.exists():
            raw = out_file.read_text(encoding="utf-8", errors="replace")
            out_file.unlink(missing_ok=True)
            if raw.startswith("ERROR:"):
                return {"status": "error", "error": raw.strip()}
            return {"status": "ok", "raw": raw}
        return {"status": "error", "error": f"No output file produced. stderr: {(proc.stderr or '')[:500]}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Console status reader timed out"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_codex_console_status() -> dict:
    """Get Codex /status via headless console buffer reading."""
    if not CODEX_NATIVE_EXE.exists():
        return {"status": "error", "error": f"Codex native exe not found: {CODEX_NATIVE_EXE}"}
    result = run_console_status(str(CODEX_NATIVE_EXE), "/status", init_wait=12, post_wait=5)
    if result.get("status") != "ok":
        return result
    return parse_codex_status(result["raw"])


def run_gemini_console_stats(gemini_cmd: str | None = None) -> dict:
    """Get Gemini /stats via headless console buffer reading.

    If *gemini_cmd* points to a .cmd wrapper (e.g. gemini2.cmd that sets
    GEMINI_CLI_HOME), we launch it via ``cmd.exe /c`` so env vars are
    honoured and the correct account is queried.
    """
    cmd_path = gemini_cmd or ""
    # If a .cmd wrapper is provided, use it directly (it sets env vars like GEMINI_CLI_HOME)
    if cmd_path and cmd_path.lower().endswith((".cmd", ".bat")):
        cli_exe = "cmd.exe"
        cli_args = f'/c "{cmd_path}" --screen-reader'
        result = run_console_status(cli_exe, "/stats", cli_args=cli_args, init_wait=25, post_wait=12)
        if result.get("status") != "ok":
            return result
        return parse_gemini_stats(result["raw"])

    # Default: use node.exe + gemini-cli entry point directly
    if not NODE_EXE.exists():
        return {"status": "error", "error": f"Node.exe not found: {NODE_EXE}"}
    if not GEMINI_NODE_ENTRY.exists():
        return {"status": "error", "error": f"Gemini CLI not found: {GEMINI_NODE_ENTRY}"}
    cli_args = f'--no-warnings=DEP0040 {GEMINI_NODE_ENTRY} --screen-reader'
    result = run_console_status(str(NODE_EXE), "/stats", cli_args=cli_args, init_wait=25, post_wait=12)
    if result.get("status") != "ok":
        return result
    return parse_gemini_stats(result["raw"])


def claude_reset_window(reset_weekday: int = 6, reset_hour: int = 17) -> tuple[datetime, datetime]:
    """Calculate the current Claude weekly reset window (start, end) from weekday/hour."""
    now = datetime.now()
    days_ahead = (reset_weekday - now.weekday()) % 7
    next_reset = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if next_reset <= now:
        next_reset += timedelta(days=7)
    prev_reset = next_reset - timedelta(days=7)
    return prev_reset, next_reset


def claude_reset_window_from_start(reset_start: datetime) -> tuple[datetime, datetime]:
    """Calculate the current Claude weekly window from an explicit start datetime.

    Auto-advances: if now > start+7d, shifts forward by 7-day increments.
    """
    now = datetime.now()
    start = reset_start
    end = start + timedelta(days=7)
    # Advance forward if the window has passed
    while end <= now:
        start = end
        end = start + timedelta(days=7)
    # Go back if start is in the future (shouldn't happen normally)
    while start > now:
        end = start
        start = end - timedelta(days=7)
    return start, end


def _resolve_claude_window(agent: dict | None, cfg: dict | None) -> tuple[datetime, datetime]:
    """Resolve the Claude reset window for an agent, preferring per-agent reset_start."""
    if agent and agent.get("claude_reset_start"):
        try:
            start_dt = datetime.strptime(agent["claude_reset_start"], "%Y-%m-%d %H:%M")
            return claude_reset_window_from_start(start_dt)
        except Exception:
            pass
    wd = (cfg or {}).get("claude_reset_weekday", 6)
    hr = (cfg or {}).get("claude_reset_hour", 17)
    return claude_reset_window(wd, hr)


def claude_extrapolate(last_pct: float, last_time: datetime, reset_weekday: int = 6, reset_hour: int = 17,
                       window_start: datetime | None = None, window_end: datetime | None = None) -> dict:
    """Extrapolate Claude usage from a known data point."""
    now = datetime.now()
    if window_start is not None and window_end is not None:
        pass  # use provided window
    else:
        window_start, window_end = claude_reset_window(reset_weekday, reset_hour)
    total_window_hours = (window_end - window_start).total_seconds() / 3600
    hours_since_reset = max(0.01, (last_time - window_start).total_seconds() / 3600)
    hours_elapsed_now = max(0.01, (now - window_start).total_seconds() / 3600)
    hours_remaining = max(0, (window_end - now).total_seconds() / 3600)

    # Usage rate: percent per hour
    rate_pct_per_hour = last_pct / hours_since_reset if hours_since_reset > 0 else 0
    # Extrapolate current usage
    hours_since_calibration = (now - last_time).total_seconds() / 3600
    estimated_current_pct = min(100.0, last_pct + rate_pct_per_hour * hours_since_calibration)
    # Even pace: what % should be used by now if evenly distributed
    even_pace_pct = (hours_elapsed_now / total_window_hours) * 100
    # Projected total at end of window
    projected_end_pct = rate_pct_per_hour * total_window_hours
    # Time to hit 100%
    if rate_pct_per_hour > 0:
        hours_to_100 = (100 - estimated_current_pct) / rate_pct_per_hour
        time_to_100 = now + timedelta(hours=hours_to_100)
    else:
        hours_to_100 = None
        time_to_100 = None
    # Pace multiplier
    pace_multiplier = estimated_current_pct / even_pace_pct if even_pace_pct > 0 else 0

    return {
        "estimated_current_pct": round(estimated_current_pct, 1),
        "calibration_age_hours": round(hours_since_calibration, 1),
        "rate_pct_per_hour": round(rate_pct_per_hour, 2),
        "even_pace_pct": round(even_pace_pct, 1),
        "pace_multiplier": round(pace_multiplier, 2),
        "projected_end_pct": round(projected_end_pct, 1),
        "hours_to_100": round(hours_to_100, 1) if hours_to_100 is not None else None,
        "time_to_100": time_to_100.strftime("%a %I:%M %p") if time_to_100 else None,
        "hours_remaining_in_window": round(hours_remaining, 1),
        "window_start": window_start,
        "window_end": window_end,
    }


def run_codex_exec_usage(resolved: str) -> dict:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    argv = [resolved, "exec", "--json", "--skip-git-repo-check", "Say exactly ok"]
    if os.name == "nt" and resolved.lower().endswith(".cmd"):
        argv = ["cmd.exe", "/d", "/s", "/c", resolved, "exec", "--json", "--skip-git-repo-check", "Say exactly ok"]
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=25,
            creationflags=flags,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    raw = (proc.stdout or "").strip()
    usage = None
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("type") == "turn.completed":
            usage = obj.get("usage") or {}
    if not usage:
        return {"status": "error", "error": "Could not parse Codex exec usage", "raw_text": strip_ansi(raw)[:1200]}
    return {
        "status": "ok",
        "source": "exec_usage",
        "input_tokens": _as_int(usage.get("input_tokens")),
        "cached_input_tokens": _as_int(usage.get("cached_input_tokens")),
        "output_tokens": _as_int(usage.get("output_tokens")),
        "total_tokens": _as_int(usage.get("input_tokens")) + _as_int(usage.get("output_tokens")),
    }


def run_gemini_headless_usage(resolved: str) -> dict:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    argv = [resolved, "--output-format", "json", "-p", "Return exactly: ok"]
    if os.name == "nt" and resolved.lower().endswith(".cmd"):
        argv = ["cmd.exe", "/d", "/s", "/c", resolved, "--output-format", "json", "-p", "Return exactly: ok"]
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=35,
            creationflags=flags,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    raw = (proc.stdout or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        return {"status": "error", "error": "Could not parse Gemini JSON output", "raw_text": strip_ansi(raw)[:1200]}
    stats = obj.get("stats") or {}
    models = stats.get("models") or {}
    total_in = 0
    total_out = 0
    total_tokens = 0
    total_req = 0
    model_names = []
    for name, mobj in models.items():
        model_names.append(name)
        api = (mobj or {}).get("api") or {}
        toks = (mobj or {}).get("tokens") or {}
        total_req += _as_int(api.get("totalRequests"))
        total_in += _as_int(toks.get("input"))
        total_out += _as_int(toks.get("candidates"))
        total_tokens += _as_int(toks.get("total"))
    if total_tokens == 0 and total_req == 0:
        return {"status": "error", "error": "Gemini JSON stats contained no usage signal", "raw_text": strip_ansi(raw)[:1200]}
    return {
        "status": "ok",
        "source": "headless_stats",
        "models_count": len(model_names),
        "models": model_names,
        "requests": total_req,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "total_tokens": total_tokens,
    }


def _format_num(v: object) -> str:
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    return "-"


def build_password_copy_message(template: str | None, path: str, ttl_seconds: int) -> str:
    text = (template or "").strip() or DEFAULT_CONFIG["password_copy_template"]
    expires_at = (datetime.now() + timedelta(seconds=max(1, int(ttl_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
    mapping = {
        "path": path,
        "ttl_seconds": str(max(1, int(ttl_seconds))),
        "expires_at": expires_at,
    }
    try:
        return text.format(**mapping)
    except Exception:
        return text


def _resolve_cmd(cmd_name: str, fallbacks: list[str]) -> str | None:
    candidates = [cmd_name] + fallbacks
    seen = set()
    for cand in candidates:
        c = (cand or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        if ("\\" in c or "/" in c):
            if Path(c).exists():
                return c
        else:
            if shutil.which(c):
                if os.name == "nt":
                    found = shutil.which(c)
                    if found:
                        p = Path(found)
                        if p.suffix.lower() == ".cmd":
                            return str(p)
                        p_cmd = p.with_suffix(".cmd")
                        if p_cmd.exists():
                            return str(p_cmd)
                return c
    return None


def _run_cli_session(resolved: str, commands: list[str], timeout: int = 15) -> dict:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cmd_lines = [(c or "").strip() for c in commands if (c or "").strip()]
    if not cmd_lines:
        return {"status": "error", "error": "No CLI commands provided"}

    if os.name == "nt" and PtyProcess is not None:
        proc = None
        output_parts: list[str] = []

        def _reader() -> None:
            while proc is not None:
                try:
                    chunk = proc.read(4096)
                except Exception:
                    break
                if not chunk:
                    if not proc.isalive():
                        break
                    time.sleep(0.05)
                    continue
                if isinstance(chunk, bytes):
                    output_parts.append(chunk.decode("utf-8", errors="replace"))
                else:
                    output_parts.append(str(chunk))

        try:
            spawn_argv = [resolved]
            if resolved.lower().endswith(".cmd"):
                spawn_argv = ["cmd.exe", "/d", "/s", "/c", resolved]
            proc = PtyProcess.spawn(spawn_argv, dimensions=(30, 140))
        except Exception as exc:
            return {"status": "error", "error": f"PTY spawn failed: {exc}"}

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            start_wait = time.time() + 3.0
            while time.time() < start_wait:
                if _looks_like_prompt("".join(output_parts)):
                    break
                time.sleep(0.05)
            for c in cmd_lines:
                proc.write(c + "\r\n")
                time.sleep(0.15)
            if cmd_lines[-1].strip().lower() != "/exit":
                proc.write("/exit\r\n")
            deadline = time.time() + max(5, int(timeout))
            while time.time() < deadline:
                if not proc.isalive():
                    break
                time.sleep(0.1)
            if proc.isalive():
                try:
                    proc.terminate(force=True)
                except Exception:
                    pass
            t.join(timeout=1.0)
            return {"status": "ok", "raw": "".join(output_parts)}
        except Exception as exc:
            try:
                if proc and proc.isalive():
                    proc.terminate(force=True)
            except Exception:
                pass
            return {"status": "error", "error": str(exc)}

    try:
        if os.name == "nt":
            joined = "`n".join(cmd_lines)
            if cmd_lines[-1].strip().lower() != "/exit":
                joined += "`n/exit"
            ps_script = (
                "$ErrorActionPreference='Stop'; "
                f"$cmd={json.dumps(resolved)}; "
                f"$out = {json.dumps(joined + '`n')} | & $cmd 2>&1 | Out-String; "
                "Write-Output $out"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                text=True,
                capture_output=True,
                timeout=timeout,
                creationflags=flags,
            )
        else:
            joined = "\n".join(cmd_lines)
            if cmd_lines[-1].strip().lower() != "/exit":
                joined += "\n/exit"
            proc = subprocess.run(
                [resolved],
                input=joined + "\n",
                text=True,
                capture_output=True,
                timeout=timeout,
                creationflags=flags,
            )
        return {"status": "ok", "raw": (proc.stdout or "") + "\n" + (proc.stderr or "")}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_codex_status(cmd_name: str) -> dict:
    # Primary: headless console buffer reading (gets real /status with percentages)
    console = run_codex_console_status()
    if console.get("status") == "ok":
        console["source"] = "console_buffer"
        return console

    # Fallback: codex exec --json (gets token counts but no percentages)
    resolved = _resolve_cmd(
        (cmd_name or "codex").strip(),
        [
            "codex",
            "codex.cmd",
            str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
        ],
    )
    if not resolved:
        return {"status": "error", "error": f"Command not found: {cmd_name}", "console_error": console.get("error")}

    alt = run_codex_exec_usage(resolved)
    if alt.get("status") == "ok":
        return alt
    run = _run_cli_session(resolved, ["/status", "/exit"], timeout=15)
    if run.get("status") != "ok":
        return {"status": "error", "error": alt.get("error") or run.get("error", "Codex session failed"), "raw_text": alt.get("raw_text"), "console_error": console.get("error")}
    raw = run.get("raw", "")
    if "stdin is not a terminal" in raw.lower():
        return {"status": "error", "error": alt.get("error") or "Codex requires TTY (non-interactive run blocked)", "raw_text": alt.get("raw_text")}
    parsed = parse_codex_status(raw)
    if parsed.get("status") != "ok":
        parsed["raw_text"] = strip_ansi(raw)[:1400]
        parsed["alt_error"] = alt.get("error")
    return parsed


def _run_gemini_stats_once(resolved: str, mode: str) -> dict:
    stats_cmd = f"/stats {mode}" if mode else "/stats"
    run = _run_cli_session(resolved, [stats_cmd, "/exit"], timeout=15)
    if run.get("status") != "ok":
        return {"status": "error", "error": run.get("error", "Gemini session failed")}
    raw = run.get("raw", "")
    if "what are we working on today?" in raw.lower():
        return {"status": "error", "error": "Gemini CLI bootstrap prompt blocked /stats; complete CLI bootstrap first"}
    if "stdin is not a terminal" in raw.lower():
        return {"status": "error", "error": "Gemini requires TTY (non-interactive run blocked)"}
    parsed = parse_gemini_stats(raw)
    parsed["stats_mode"] = mode
    if parsed.get("status") != "ok":
        parsed["raw_text"] = strip_ansi(raw)[:1400]
    return parsed


def run_gemini_stats(cmd_name: str, mode: str = "auto") -> dict:
    # Primary: headless console buffer reading (gets real /stats with per-model percentages)
    console = run_gemini_console_stats(gemini_cmd=cmd_name)
    if console.get("status") == "ok":
        console["source"] = "console_buffer"
        return console

    resolved = _resolve_cmd(
        (cmd_name or "gemini").strip(),
        [
            "gemini",
            "gemini.cmd",
            str(Path.home() / "AppData" / "Roaming" / "npm" / "gemini.cmd"),
        ],
    )
    if not resolved:
        return {"status": "error", "error": f"Command not found: {cmd_name}", "console_error": console.get("error")}

    try:
        headless = run_gemini_headless_usage(resolved)
        if headless.get("status") == "ok":
            return headless
        mode = (mode or "auto").strip().lower()
        if mode == "auto":
            last_err = None
            for m in ("model", "session"):
                out = _run_gemini_stats_once(resolved, m)
                if out.get("status") == "ok":
                    out["source"] = "cli_stats"
                    return out
                err_text = (out.get("error") or "").lower()
                if any(x in err_text for x in ("command not found", "bootstrap prompt", "requires tty", "pty spawn failed", "gemini session failed")):
                    return out
                last_err = out
            return last_err or {"status": "error", "error": "Gemini /stats failed for all modes"}
        if mode not in ("session", "model", "tools"):
            mode = "model"
        return _run_gemini_stats_once(resolved, mode)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_usage_scraper(script_path: str, provider: str, pinchtab_url: str) -> dict:
    p = Path(script_path)
    if not p.exists():
        return {"status": "error", "error": f"usage_scraper missing: {script_path}"}
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [
                "python",
                str(p),
                "--provider",
                provider,
                "--pinchtab-url",
                pinchtab_url,
                "--no-save",
                "--compact",
            ],
            text=True,
            capture_output=True,
            timeout=20,
            creationflags=flags,
        )
        payload = None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                payload = json.loads(line)
                break
        if not payload:
            return {"status": "error", "error": (proc.stderr or "No JSON from usage_scraper").strip()}
        pdata = payload.get("providers", {}).get(provider)
        if not pdata:
            return {"status": "error", "error": f"No provider block for {provider}"}
        return pdata
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def parse_claude_from_pinchtab(pinchtab_url: str, claude_url: str) -> dict:
    pt = PinchtabClient(pinchtab_url)
    if not pt.health():
        return {"status": "error", "error": f"Pinchtab unreachable at {pinchtab_url}"}
    try:
        pt.navigate(claude_url)
        time.sleep(3)
        snapshot = pt.snapshot()
        page_text = pt.text()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    if detect_login_wall(page_text):
        return {"status": "login_required", "error": "Claude login required"}

    nodes = parse_snapshot_nodes(snapshot)
    names = [n.get("name", "") for n in nodes if n.get("role") == "StaticText"]

    weekly_all = None
    weekly_sonnet = None
    weekly_reset = None

    section = None
    model_type = None
    for name in names:
        lower = name.lower().strip()
        if "weekly limits" in lower or "weekly limit" in lower:
            section = "weekly"
            continue
        if "all models" in lower:
            model_type = "all"
            continue
        if "sonnet" in lower:
            model_type = "sonnet"
            continue
        m_pct = re.match(r"(\d+(?:\.\d+)?)\s*%\s*used", lower)
        if m_pct and section == "weekly":
            pct = float(m_pct.group(1))
            if model_type == "sonnet":
                weekly_sonnet = pct
            elif weekly_all is None:
                weekly_all = pct
        m_reset = re.match(r"resets?\s+(.+)", lower)
        if m_reset and section == "weekly":
            weekly_reset = m_reset.group(1).strip()

    if weekly_all is None and weekly_sonnet is None:
        return {"status": "error", "error": "Could not parse Claude usage"}

    return {
        "status": "ok",
        "weekly_all_models_pct_used": weekly_all,
        "weekly_sonnet_pct_used": weekly_sonnet,
        "weekly_reset": weekly_reset,
    }


def parse_gemini_from_pinchtab(pinchtab_url: str, gemini_url: str) -> dict:
    pt = PinchtabClient(pinchtab_url)
    if not pt.health():
        return {"status": "error", "error": f"Pinchtab unreachable at {pinchtab_url}"}
    try:
        pt.navigate(gemini_url)
        time.sleep(3)
        page_text = pt.text()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    if detect_login_wall(page_text):
        return {"status": "login_required", "error": "Gemini login required"}

    period = None
    m_period = re.search(r"Total\s+cost\s*\(([^)]+)\)", page_text, flags=re.IGNORECASE)
    if m_period:
        period = m_period.group(1).strip()

    vals = []
    for m in re.finditer(r"\$([\d,]+(?:\.\d{1,2})?)", page_text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    if not vals and not period:
        return {"status": "error", "error": "Could not parse Gemini usage page"}

    spend = max(vals) if vals else None
    return {
        "status": "ok",
        "estimated_spend_usd": spend,
        "limit_usd": None,
        "remaining_usd": None,
        "billing_period": period,
        "tier": None,
    }


INSTALL_URLS = {
    "codex": "https://github.com/openai/codex",
    "claude": "https://docs.anthropic.com/en/docs/claude-code/overview",
    "gemini": "https://github.com/google-gemini/gemini-cli",
}


class SetupWizard:
    """Multi-agent setup wizard. Shows detected agents with editable labels."""

    def __init__(self, parent: tk.Tk, current_agents: list[dict] | None = None):
        self.result = None  # will be list[dict] of agent configs on save
        self.parent = parent
        detected = detect_agents()
        # Merge current agents with newly detected ones
        if current_agents:
            existing_keys = {(a["type"], a.get("binary", "")) for a in current_agents}
            self.agents = list(current_agents)
            for d in detected:
                if (d["type"], d.get("binary", "")) not in existing_keys:
                    self.agents.append(d)
        else:
            self.agents = detected

        self.win = tk.Toplevel(parent)
        self.win.title("AI Usage Tracker - Setup")
        wiz_h = max(520, 200 + len(self.agents) * 80)
        self.win.geometry(f"600x{min(wiz_h, 750)}")
        self.win.configure(bg="#111318")
        self.win.resizable(True, True)
        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - 600) // 2
        y = (self.win.winfo_screenheight() - min(wiz_h, 750)) // 2
        self.win.geometry(f"+{x}+{y}")

        tk.Label(self.win, text="AI Usage Tracker", fg="#f4f6fb", bg="#111318",
                 font=("Segoe UI", 16, "bold")).pack(pady=(18, 4))
        tk.Label(self.win, text="Detected agents — edit labels and select which to track:",
                 fg="#aeb6c8", bg="#111318", font=("Segoe UI", 11)).pack(pady=(0, 10))

        # Scrollable agent list
        list_frame = tk.Frame(self.win, bg="#111318")
        list_frame.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        canvas = tk.Canvas(list_frame, bg="#111318", highlightthickness=0)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self.agent_inner = tk.Frame(canvas, bg="#111318")
        self.agent_inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.agent_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self.agent_vars = []  # list of (enable_var, label_var, agent_dict)
        for agent in self.agents:
            self._add_agent_row(agent)

        # Add manually button
        btn_row = tk.Frame(self.win, bg="#111318")
        btn_row.pack(fill="x", padx=20, pady=(4, 4))
        tk.Button(btn_row, text="+ Add manually", width=16, font=("Segoe UI", 9),
                  command=self._add_manual).pack(side="left")

        self.warn_label = tk.Label(self.win, text="", fg="#f0ad4e", bg="#111318", font=("Segoe UI", 9))
        self.warn_label.pack(pady=(4, 0))

        tk.Label(self.win, text="You can change this anytime via the Setup button.",
                 fg="#666", bg="#111318", font=("Segoe UI", 9)).pack(pady=(2, 4))
        tk.Button(self.win, text="Start", width=16, font=("Segoe UI", 11),
                  command=self._save).pack(pady=(4, 14))

        self.win.protocol("WM_DELETE_WINDOW", self._save)
        self.win.transient(parent)
        self.win.grab_set()
        parent.wait_window(self.win)

    def _add_agent_row(self, agent: dict):
        row = tk.Frame(self.agent_inner, bg="#1a1f2a", bd=1, relief="solid")
        row.pack(fill="x", padx=4, pady=3)

        enable_var = tk.BooleanVar(value=agent.get("enabled", True))
        label_var = tk.StringVar(value=agent.get("label", agent.get("type", "").title()))

        left = tk.Frame(row, bg="#1a1f2a")
        left.pack(side="left", fill="x", expand=True, padx=8, pady=6)

        top = tk.Frame(left, bg="#1a1f2a")
        top.pack(anchor="w", fill="x")
        cb = tk.Checkbutton(top, variable=enable_var, fg="#f4f6fb", bg="#1a1f2a",
                            selectcolor="#2a3040", activebackground="#1a1f2a")
        cb.pack(side="left")
        tk.Label(top, text=f"[{agent['type']}]", fg="#7788aa", bg="#1a1f2a",
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        tk.Label(top, text="Label:", fg="#aeb6c8", bg="#1a1f2a",
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(top, textvariable=label_var, width=22, font=("Segoe UI", 9)).pack(side="left", padx=(4, 0))

        binary_text = agent.get("binary", "-")
        data_text = agent.get("data_dir") or agent.get("telemetry_dir") or "-"
        tk.Label(left, text=f"Binary: {binary_text}  |  Data: {data_text}",
                 fg="#97a2bd", bg="#1a1f2a", font=("Segoe UI", 8)).pack(anchor="w", padx=(24, 0))

        right = tk.Frame(row, bg="#1a1f2a")
        right.pack(side="right", padx=8)
        if agent.get("verified"):
            tk.Label(right, text="Verified", fg="#5cb85c", bg="#1a1f2a",
                     font=("Segoe UI", 9, "bold")).pack()
        else:
            tk.Label(right, text="Detected", fg="#f0ad4e", bg="#1a1f2a",
                     font=("Segoe UI", 9, "bold")).pack()

        self.agent_vars.append((enable_var, label_var, agent))

    def _add_manual(self):
        """Show a small dialog to add an agent manually."""
        dlg = tk.Toplevel(self.win)
        dlg.title("Add Agent")
        dlg.geometry("400x220")
        dlg.configure(bg="#111318")
        dlg.transient(self.win)

        tk.Label(dlg, text="Type:", fg="#aeb6c8", bg="#111318").pack(anchor="w", padx=12, pady=(12, 0))
        type_var = tk.StringVar(value="claude")
        type_menu = tk.OptionMenu(dlg, type_var, "claude", "codex", "gemini")
        type_menu.pack(fill="x", padx=12)

        tk.Label(dlg, text="Binary name or path:", fg="#aeb6c8", bg="#111318").pack(anchor="w", padx=12, pady=(8, 0))
        binary_var = tk.StringVar()
        tk.Entry(dlg, textvariable=binary_var, width=40).pack(fill="x", padx=12)

        tk.Label(dlg, text="Label:", fg="#aeb6c8", bg="#111318").pack(anchor="w", padx=12, pady=(8, 0))
        manual_label_var = tk.StringVar()
        tk.Entry(dlg, textvariable=manual_label_var, width=40).pack(fill="x", padx=12)

        def _ok():
            t = type_var.get()
            b = binary_var.get().strip()
            if not b:
                return
            count = sum(1 for _, _, a in self.agent_vars if a["type"] == t) + 1
            agent = {
                "id": f"{t}_{count}",
                "type": t,
                "label": manual_label_var.get().strip() or f"{t.title()} {count}",
                "binary": b,
                "data_dir": None,
                "enabled": True,
                "verified": False,
            }
            if t == "claude":
                agent["telemetry_dir"] = None
                agent["sessions_dir"] = None
            elif t == "gemini":
                agent["gemini_cmd"] = b
            self._add_agent_row(agent)
            dlg.destroy()

        tk.Button(dlg, text="Add", width=10, command=_ok).pack(pady=(12, 8))

    def _save(self):
        result = []
        for enable_var, label_var, agent in self.agent_vars:
            a = dict(agent)
            a["enabled"] = enable_var.get()
            a["label"] = label_var.get().strip() or a.get("label", a["type"].title())
            result.append(a)
        enabled = [a for a in result if a["enabled"]]
        if not enabled:
            self.warn_label.configure(text="Please enable at least one agent, or close to keep current settings.")
            return
        self.result = result
        self.win.destroy()


def _open_url(url: str):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


class UsageWidget:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = ensure_config()
        self.refresh_in_progress = False
        self.password_wipe_tokens = {}
        self.clipboard_clear_token = 0
        self.password_lock = threading.Lock()
        self._ephemeral_history: list[str] = []
        self._pw_idle_after_id = None
        self._pw_idle_seconds = 120  # auto-clear password field after 2 min inactivity
        self.last_good = {}
        self.last_good_time = {}

        self.agents = get_enabled_agents(self.cfg)

        self.root.title("AI Platform Usage")
        n_cards = max(len(self.agents), 1)
        win_height = min(900, 350 + (n_cards * 160))
        self.root.geometry(f"820x{win_height}")
        self.root.minsize(780, min(win_height, 820))
        self.root.configure(bg="#111318")

        self.cards = {}  # keyed by agent["id"]
        self._build_ui()
        self.root.bind("<F5>", lambda _e: self.refresh_now())
        self.root.bind("<Control-r>", lambda _e: self.refresh_now())
        self.schedule_refresh(initial=True)

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#111318")
        header.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(header, text="AI Usage Remaining", fg="#f4f6fb", bg="#111318", font=("Segoe UI", 17, "bold")).pack(side="left")
        self.updated = tk.Label(header, text="Last update: -", fg="#aeb6c8", bg="#111318", font=("Segoe UI", 10))
        self.updated.pack(side="right")

        controls = tk.Frame(self.root, bg="#111318")
        controls.pack(fill="x", padx=14, pady=(0, 10))
        tk.Button(controls, text="Refresh now", width=14, command=self.refresh_now).pack(side="left")
        tk.Button(controls, text="Open config", width=14, command=self.open_config).pack(side="left", padx=(8, 0))
        tk.Button(controls, text="Setup", width=10, command=self._rerun_setup).pack(side="left", padx=(8, 0))

        # Scrollable card area
        card_container = tk.Frame(self.root, bg="#111318")
        card_container.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self._card_canvas = tk.Canvas(card_container, bg="#111318", highlightthickness=0)
        self._card_scrollbar = tk.Scrollbar(card_container, orient="vertical", command=self._card_canvas.yview)
        self._card_inner = tk.Frame(self._card_canvas, bg="#111318")
        self._card_inner.bind("<Configure>", lambda _e: self._card_canvas.configure(scrollregion=self._card_canvas.bbox("all")))
        self._card_canvas.create_window((0, 0), window=self._card_inner, anchor="nw")
        self._card_canvas.configure(yscrollcommand=self._card_scrollbar.set)
        self._card_canvas.pack(side="left", fill="both", expand=True)
        self._card_scrollbar.pack(side="right", fill="y")
        self._card_canvas.bind_all("<MouseWheel>", lambda e: self._card_canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        # Per-agent tracking for Claude pct vars
        self.claude_pct_vars = {}    # agent_id -> StringVar
        self.claude_age_labels = {}  # agent_id -> Label
        self.claude_reset_vars = {}  # agent_id -> StringVar (reset start datetime)

        for agent in self.agents:
            self._build_agent_card(agent)

        footer = tk.Frame(self.root, bg="#10141d", bd=1, relief="solid")
        footer.pack(fill="x", padx=14, pady=(0, 12))

        header_row = tk.Frame(footer, bg="#10141d")
        header_row.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(header_row, text="Temporary Password Helper", fg="#f4f6fb", bg="#10141d", font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(header_row, text="?", width=2, font=("Segoe UI", 9, "bold"), command=self._show_password_help).pack(side="left", padx=(6, 0))

        row1 = tk.Frame(footer, bg="#10141d")
        row1.pack(fill="x", padx=10, pady=(0, 5))
        row1.columnconfigure(1, weight=1)
        tk.Label(row1, text="Password", fg="#d4d9e6", bg="#10141d", width=12, anchor="w").grid(row=0, column=0, sticky="w")
        self.password_var = tk.StringVar()
        self.password_entry = tk.Entry(row1, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        tk.Button(row1, text="Save/Copy", width=12, command=self.save_copy_password).grid(row=0, column=2, sticky="e")
        self.password_entry.bind("<Return>", lambda _e: self.save_copy_password())
        # Auto-clear password field after inactivity
        self.password_entry.bind("<KeyRelease>", lambda _e: self._reset_pw_idle_timer())
        self.password_entry.bind("<FocusIn>", lambda _e: self._reset_pw_idle_timer())

        row2 = tk.Frame(footer, bg="#10141d")
        row2.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(row2, text="Base path", fg="#d4d9e6", bg="#10141d", width=12, anchor="w").pack(side="left")
        self.password_file_var = tk.StringVar(value=self.cfg.get("password_file_path", DEFAULT_CONFIG["password_file_path"]))
        tk.Entry(row2, textvariable=self.password_file_var, width=60).pack(side="left", padx=(0, 8), fill="x", expand=True)
        tk.Button(row2, text="Browse", width=10, command=self.browse_password_file).pack(side="left")

        row3 = tk.Frame(footer, bg="#10141d")
        row3.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(row3, text="TTL sec", fg="#d4d9e6", bg="#10141d", width=12, anchor="w").pack(side="left")
        self.password_ttl_var = tk.StringVar(value=str(self.cfg.get("password_ttl_seconds", 30)))
        tk.Entry(row3, textvariable=self.password_ttl_var, width=8).pack(side="left", padx=(0, 12))
        tk.Label(row3, text="Clip clear sec", fg="#d4d9e6", bg="#10141d", anchor="w").pack(side="left")
        self.password_clip_clear_var = tk.StringVar(value=str(self.cfg.get("password_clipboard_clear_seconds", 45)))
        tk.Entry(row3, textvariable=self.password_clip_clear_var, width=8).pack(side="left", padx=(8, 12))
        tk.Label(row3, text="Clipboard message", fg="#d4d9e6", bg="#10141d", anchor="w").pack(side="left")
        self.password_template_var = tk.StringVar(value=self.cfg.get("password_copy_template", DEFAULT_CONFIG["password_copy_template"]))
        tk.Entry(row3, textvariable=self.password_template_var, width=70).pack(side="left", padx=(8, 0), fill="x", expand=True)

        hint = "Template vars: {path} {ttl_seconds} {expires_at}"
        tk.Label(footer, text=hint, fg="#97a2bd", bg="#10141d", font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(0, 3))
        tk.Label(footer, text="Save/Copy creates a new ephemeral filename each time and removes previous generated files.", fg="#97a2bd", bg="#10141d", font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(0, 3))
        self.password_status = tk.Label(footer, text="", fg="#97a2bd", bg="#10141d", font=("Segoe UI", 9))
        self.password_status.pack(anchor="w", padx=10, pady=(0, 8))

    def _build_agent_card(self, agent: dict):
        """Build a single agent card inside the scrollable card area."""
        aid = agent["id"]
        atype = agent["type"]
        label = agent.get("label", atype.title())

        card = tk.Frame(self._card_inner, bg="#1a1f2a", bd=1, relief="solid")
        card.pack(fill="x", pady=6, padx=2)

        title_row = tk.Frame(card, bg="#1a1f2a")
        title_row.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(title_row, text=label, fg="#f4f6fb", bg="#1a1f2a", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(title_row, text=f"[{atype}]", fg="#7788aa", bg="#1a1f2a", font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        # AI Bootstrap button — expands a text box with setup instructions
        bootstrap_frame = tk.Frame(card, bg="#1a1f2a")
        bootstrap_text = tk.Text(bootstrap_frame, wrap="word", bg="#0d1117", fg="#c9d1d9",
                                  font=("Consolas", 9), height=12, bd=1, relief="solid",
                                  insertbackground="#c9d1d9", selectbackground="#264f78")
        bootstrap_text.insert("1.0", self._get_bootstrap_text(agent))
        bootstrap_text.config(state="disabled")
        bootstrap_text.pack(fill="x", padx=8, pady=(4, 6))

        def toggle_bootstrap(frame=bootstrap_frame):
            if frame.winfo_manager():
                frame.pack_forget()
            else:
                frame.pack(fill="x", padx=12, pady=(0, 2), after=title_row)
            # Update scroll region after expand/collapse
            card.update_idletasks()
            self._card_canvas.configure(scrollregion=self._card_canvas.bbox("all"))

        tk.Button(title_row, text="AI Bootstrap", width=12, font=("Segoe UI", 8),
                  command=toggle_bootstrap).pack(side="right", padx=(4, 0))

        if atype == "claude":
            # Manual percentage input for Claude
            tk.Label(title_row, text="Usage %:", fg="#aeb6c8", bg="#1a1f2a", font=("Segoe UI", 9)).pack(side="left", padx=(20, 4))
            pct_var = tk.StringVar(value=str(agent.get("claude_last_known_pct") or ""))
            self.claude_pct_vars[aid] = pct_var
            pct_entry = tk.Entry(title_row, textvariable=pct_var, width=5, font=("Segoe UI", 9))
            pct_entry.pack(side="left")
            tk.Button(title_row, text="Set", width=4, font=("Segoe UI", 8),
                      command=lambda _aid=aid: self._save_claude_pct(_aid)).pack(side="left", padx=(4, 0))
            age_text = ""
            if agent.get("claude_last_known_pct") is not None and agent.get("claude_last_known_time"):
                try:
                    lt = datetime.fromisoformat(agent["claude_last_known_time"])
                    mins = int((datetime.now() - lt).total_seconds() / 60)
                    age_text = f"(set {mins}m ago)"
                except Exception:
                    pass
            age_label = tk.Label(title_row, text=age_text, fg="#97a2bd", bg="#1a1f2a", font=("Segoe UI", 8))
            age_label.pack(side="left", padx=(6, 0))
            self.claude_age_labels[aid] = age_label

            # Reset start datetime input
            reset_row = tk.Frame(card, bg="#1a1f2a")
            reset_row.pack(fill="x", padx=12, pady=(2, 2))
            tk.Label(reset_row, text="Reset start:", fg="#aeb6c8", bg="#1a1f2a",
                     font=("Segoe UI", 9)).pack(side="left")
            # Default: compute from weekday/hour if no per-agent value
            default_reset = agent.get("claude_reset_start", "")
            if not default_reset:
                wd = self.cfg.get("claude_reset_weekday", 6)
                hr = self.cfg.get("claude_reset_hour", 17)
                ws, _ = claude_reset_window(wd, hr)
                default_reset = ws.strftime("%Y-%m-%d %H:%M")
            reset_var = tk.StringVar(value=default_reset)
            self.claude_reset_vars[aid] = reset_var
            tk.Entry(reset_row, textvariable=reset_var, width=16, font=("Segoe UI", 9)).pack(side="left", padx=(4, 0))
            tk.Label(reset_row, text="(end = start + 7d)", fg="#97a2bd", bg="#1a1f2a",
                     font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))
            tk.Button(reset_row, text="Set", width=4, font=("Segoe UI", 8),
                      command=lambda _aid=aid: self._save_claude_reset(_aid)).pack(side="left", padx=(4, 0))

        # Bootstrap / verify button for unverified agents
        if not agent.get("verified"):
            verify_row = tk.Frame(card, bg="#1a1f2a")
            verify_row.pack(fill="x", padx=12, pady=(2, 2))
            tk.Label(verify_row, text="Not verified", fg="#f0ad4e", bg="#1a1f2a",
                     font=("Segoe UI", 9, "bold")).pack(side="left")
            # Prefer gemini_cmd (full path) over binary name for verify
            verify_bin = agent.get("gemini_cmd") or agent.get("binary", atype)
            verify_cmd = f'"{verify_bin}" --version' if " " in str(verify_bin) else f"{verify_bin} --version"
            cmd_entry = tk.Entry(verify_row, width=40, font=("Consolas", 9))
            cmd_entry.insert(0, verify_cmd)
            cmd_entry.pack(side="left", padx=(8, 4))
            tk.Button(verify_row, text="Verify Now", width=10, font=("Segoe UI", 8),
                      command=lambda _aid=aid, _e=cmd_entry: self._verify_agent(_aid, _e.get())).pack(side="left")

        status = tk.Label(card, text="WAITING", fg="#f0ad4e", bg="#1a1f2a", font=("Segoe UI", 10, "bold"))
        status.pack(anchor="w", padx=12)
        summary = tk.Label(card, text="Waiting for first refresh", fg="#d4d9e6", bg="#1a1f2a",
                           font=("Segoe UI", 11), wraplength=720, justify="left")
        summary.pack(anchor="w", padx=12, pady=(2, 4))
        timeline = tk.Label(card, text="Usage start: -                                    Usage end: -",
                            fg="#9cb5ff", bg="#1a1f2a", font=("Segoe UI", 9))
        timeline.pack(anchor="w", padx=12, pady=(0, 2))
        tk.Label(card, text="Blue: target pace to finish at zero | Green: actual usage so far",
                 fg="#97a2bd", bg="#1a1f2a", font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(0, 1))
        pace_canvas = tk.Canvas(card, width=720, height=10, bg="#1a1f2a", highlightthickness=0, bd=0)
        pace_canvas.pack(anchor="w", padx=12, pady=(0, 2))
        actual_canvas = tk.Canvas(card, width=720, height=10, bg="#1a1f2a", highlightthickness=0, bd=0)
        actual_canvas.pack(anchor="w", padx=12, pady=(0, 4))
        detail = tk.Label(card, text="", fg="#97a2bd", bg="#1a1f2a", font=("Consolas", 9),
                          wraplength=720, justify="left")
        detail.pack(anchor="w", padx=12, pady=(0, 9))
        self.cards[aid] = {
            "status": status, "summary": summary, "timeline": timeline,
            "pace_canvas": pace_canvas, "actual_canvas": actual_canvas, "detail": detail,
            "card_frame": card,
        }

    def _get_bootstrap_text(self, agent: dict) -> str:
        """Generate setup/fix instructions for an AI agent to configure this tracker."""
        atype = agent["type"]
        aid = agent["id"]
        label = agent.get("label", atype.title())
        binary = agent.get("binary", atype)

        lines = [f"=== AI BOOTSTRAP: {label} ({atype}) ===", ""]
        lines.append("You are configuring the AI Usage Tracker for this agent.")
        lines.append(f"Agent ID: {aid}  |  Type: {atype}  |  Binary: {binary}")
        lines.append(f"Config file: {CONFIG_PATH}")
        lines.append("")

        if atype == "claude":
            data_dir = agent.get("data_dir", "~/.claude")
            tel_dir = agent.get("telemetry_dir", "")
            sess_dir = agent.get("sessions_dir", "")
            lines.append("HOW CLAUDE TRACKING WORKS:")
            lines.append(f"  - Reads local telemetry from: {tel_dir or data_dir + '/telemetry'}")
            lines.append(f"  - Sessions dir: {sess_dir or data_dir + '/sessions'}")
            lines.append("  - Telemetry files contain per-model token counts (Opus/Sonnet/Haiku)")
            lines.append("  - Usage % is set manually from claude.ai/settings/usage")
            lines.append("")
            lines.append("SETUP STEPS:")
            lines.append(f"  1. Ensure '{binary}' is installed and on PATH")
            lines.append(f"  2. Verify data dir exists: {data_dir}")
            lines.append(f"  3. Run '{binary}' at least once so telemetry files are created")
            lines.append("  4. Go to https://claude.ai/settings/usage and note the usage %")
            lines.append("  5. Enter that % in the 'Usage %' field above and click Set")
            lines.append("  6. Set the 'Reset start' to when your billing period started")
            lines.append("     (usually shown on the usage page as cycle start date)")
            lines.append("     End = start + 7 days (auto-calculated)")
            lines.append("")
            lines.append("IF USAGE SHOWS 'NO DATA':")
            lines.append(f"  - Check telemetry dir exists: {tel_dir or data_dir + '/telemetry'}")
            lines.append("  - Telemetry files are .jsonl files with timestamps")
            lines.append(f"  - If '{binary}' is a second account, ensure it uses a separate data dir")
            lines.append(f"    (e.g. set CLAUDE_CONFIG_DIR to a unique path like ~/.claude2)")
            pct = agent.get("claude_last_known_pct")
            reset = agent.get("claude_reset_start")
            lines.append("")
            lines.append("CURRENT CALIBRATION:")
            lines.append(f"  Usage %: {pct if pct is not None else 'NOT SET'}")
            lines.append(f"  Reset start: {reset or 'NOT SET'}")
            lines.append(f"  Last updated: {agent.get('claude_last_known_time', 'never')}")

        elif atype == "codex":
            lines.append("HOW CODEX TRACKING WORKS:")
            lines.append("  - Runs 'codex' in a hidden terminal and sends '/status' command")
            lines.append("  - Parses the TUI output for remaining %, reset times, Spark tier")
            lines.append("  - Falls back to headless JSON mode if console read fails")
            lines.append("")
            lines.append("SETUP STEPS:")
            lines.append(f"  1. Ensure '{binary}' is installed: npm i -g @openai/codex")
            lines.append(f"  2. Verify: {binary} --version")
            lines.append("  3. Codex must be logged in (run 'codex' once and authenticate)")
            lines.append("")
            lines.append("IF USAGE SHOWS 'NO DATA':")
            lines.append(f"  - Run '{binary}' manually and type /status — does it show usage?")
            lines.append("  - Install pywinpty for better console capture: pip install pywinpty")
            lines.append("  - Check that the binary is the real codex, not a canary/wrapper")

        elif atype == "gemini":
            gemini_cmd = agent.get("gemini_cmd", binary)
            lines.append("HOW GEMINI TRACKING WORKS:")
            lines.append("  - Runs 'gemini' in a hidden terminal and sends '/stats' command")
            lines.append("  - Parses per-model remaining % and reset timers")
            lines.append("  - Uses OAuth (Google account login), not API keys")
            lines.append("")
            lines.append("SETUP STEPS:")
            lines.append(f"  1. Ensure gemini CLI is installed: npm i -g @google/gemini-cli")
            lines.append(f"  2. Command path: {gemini_cmd}")
            lines.append(f"  3. Run '{binary}' interactively once to complete OAuth login")
            lines.append("  4. Type /stats in the Gemini TUI to verify it shows usage data")
            lines.append("")
            if agent.get("gemini_cmd") and agent.get("gemini_cmd") != binary:
                lines.append("MULTI-ACCOUNT SETUP:")
                lines.append(f"  This agent uses a custom cmd wrapper: {gemini_cmd}")
                home = ""
                # Try to detect GEMINI_CLI_HOME from the cmd
                cmd_path = agent.get("gemini_cmd", "")
                if cmd_path:
                    try:
                        content = Path(cmd_path).read_text(encoding="utf-8", errors="replace")
                        import re
                        m = re.search(r'GEMINI_CLI_HOME=([^\s&"]+)', content)
                        if m:
                            home = m.group(1)
                    except Exception:
                        pass
                if home:
                    lines.append(f"  GEMINI_CLI_HOME is set to: {home}")
                    lines.append(f"  This separates config/auth from the default gemini install")
                lines.append(f"  To re-authenticate: run '{binary}' and complete Google OAuth")
                lines.append("")
            lines.append("IF USAGE SHOWS 'NO DATA':")
            lines.append(f"  - Run '{binary}' manually and type /stats")
            lines.append("  - If it asks to log in, complete the OAuth flow")
            lines.append("  - Ensure the Google account has Gemini API access (aistudio.google.com)")

        lines.append("")
        lines.append("CONFIG ENTRY (in config.json):")
        lines.append(json.dumps(agent, indent=2))
        return "\n".join(lines)

    def _verify_agent(self, agent_id: str, cmd: str):
        """Run a verify command for an agent and mark it verified on success."""
        def worker():
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                # Use shell=True so .cmd/.bat files work and paths with spaces aren't broken
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                      creationflags=flags, shell=True)
                output = (proc.stdout or "").strip() + "\n" + (proc.stderr or "").strip()
                if proc.returncode == 0 and output.strip():
                    # Mark verified in config
                    for a in self.cfg.get("agents", []):
                        if a["id"] == agent_id:
                            a["verified"] = True
                            break
                    CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
                    self.root.after(0, lambda: messagebox.showinfo("Verify", f"Agent verified!\n\n{output.strip()[:500]}"))
                    # Rebuild UI to remove verify row
                    self.root.after(100, self._rebuild_cards)
                else:
                    self.root.after(0, lambda: messagebox.showwarning("Verify", f"Command returned error:\n{output.strip()[:500]}"))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("Verify", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_cards(self):
        """Rebuild the card area after config changes."""
        for w in self._card_inner.winfo_children():
            w.destroy()
        self.cards = {}
        self.claude_pct_vars = {}
        self.claude_age_labels = {}
        self.claude_reset_vars = {}
        self.agents = get_enabled_agents(self.cfg)
        for agent in self.agents:
            self._build_agent_card(agent)

    def open_config(self):
        try:
            os.startfile(str(CONFIG_PATH))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Open config", str(exc))

    def _rerun_setup(self):
        wiz = SetupWizard(self.root, current_agents=list(self.cfg.get("agents", [])))
        if wiz.result is not None:
            self.cfg["agents"] = wiz.result
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
            # Rebuild UI with new agents
            for w in self.root.winfo_children():
                w.destroy()
            self.cards = {}
            self.claude_pct_vars = {}
            self.claude_age_labels = {}
            self.claude_reset_vars = {}
            self.agents = get_enabled_agents(self.cfg)
            n_cards = max(len(self.agents), 1)
            win_height = min(900, 350 + (n_cards * 160))
            self.root.geometry(f"820x{win_height}")
            self.root.minsize(780, min(win_height, 820))
            self._build_ui()
            self.schedule_refresh(initial=True)

    def _save_claude_pct(self, agent_id: str = ""):
        """Save manually entered Claude usage percentage as a calibration point."""
        pct_var = self.claude_pct_vars.get(agent_id)
        if not pct_var:
            return
        try:
            val = float(pct_var.get())
        except (ValueError, AttributeError):
            messagebox.showwarning("Invalid", "Enter a number between 0 and 100")
            return
        if val < 0 or val > 100:
            messagebox.showwarning("Invalid", "Enter a number between 0 and 100")
            return
        now_iso = datetime.now().isoformat()
        # Update in agents list
        for a in self.cfg.get("agents", []):
            if a["id"] == agent_id:
                a["claude_last_known_pct"] = val
                a["claude_last_known_time"] = now_iso
                break
        CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        # Update the UI immediately
        self._update_claude_extrapolation(agent_id)

    def _save_claude_reset(self, agent_id: str = ""):
        """Save the reset start datetime for a Claude agent."""
        reset_var = self.claude_reset_vars.get(agent_id)
        if not reset_var:
            return
        text = reset_var.get().strip()
        if not text:
            messagebox.showwarning("Invalid", "Enter a date like 2026-03-02 17:00")
            return
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                dt = datetime.fromisoformat(text)
            except Exception:
                messagebox.showwarning("Invalid", "Enter a date like 2026-03-02 17:00")
                return
        for a in self.cfg.get("agents", []):
            if a["id"] == agent_id:
                a["claude_reset_start"] = dt.strftime("%Y-%m-%d %H:%M")
                break
        CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        self._update_claude_extrapolation(agent_id)

    def _update_claude_extrapolation(self, agent_id: str = ""):
        """Refresh Claude card with current extrapolation data."""
        agent = None
        for a in self.cfg.get("agents", []):
            if a["id"] == agent_id:
                agent = a
                break
        if not agent:
            return
        pct = agent.get("claude_last_known_pct")
        time_str = agent.get("claude_last_known_time")
        if pct is None or time_str is None:
            return
        window_start, window_end = _resolve_claude_window(agent, self.cfg)
        ext = claude_extrapolate(pct, datetime.fromisoformat(time_str),
                                 window_start=window_start, window_end=window_end)
        est = ext["estimated_current_pct"]
        pace = ext["pace_multiplier"]
        rate = ext["rate_pct_per_hour"]
        t100 = ext.get("time_to_100")
        t100_str = t100.strftime("%a %I:%M %p") if t100 else "N/A"
        hrs_left = ext["hours_remaining_in_window"]
        self.set_card(
            agent_id, "ok",
            f"Est. used: {est:.1f}%  |  Rate: {rate:.2f}%/hr  |  Pace: {pace:.1f}x",
            f"Projected 100%: {t100_str}  |  Reset: {window_end.strftime('%a %I:%M %p')}  |  {hrs_left:.0f}h left in window  |  Source: extrapolation",
        )
        self._set_progress(agent_id, window_start, window_end, est / 100.0)
        # Sync the entry field with the extrapolated value
        pct_var = self.claude_pct_vars.get(agent_id)
        if pct_var:
            pct_var.set(f"{est:.1f}")

    def set_card(self, key: str, status: str, summary: str, detail: str):
        colors = {"ok": "#37c978", "stale": "#f0ad4e", "login_required": "#f0ad4e", "error": "#ff5f56", "waiting": "#f0ad4e"}
        w = self.cards[key]
        w["status"].configure(text=status.upper(), fg=colors.get(status, "#d4d9e6"))
        w["summary"].configure(text=summary)
        w["detail"].configure(text=detail)

    def _draw_bar(self, canvas: tk.Canvas, pct: float | None, color: str):
        canvas.delete("all")
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        canvas.create_rectangle(0, 0, width, height, fill="#263042", outline="#3a455a")
        if pct is None:
            return
        p = max(0.0, min(1.0, float(pct)))
        fill_w = int(width * p)
        if fill_w > 0:
            canvas.create_rectangle(0, 0, fill_w, height, fill=color, outline=color)

    def _parse_period_range(self, period_text: str | None, default_days: int) -> tuple[datetime, datetime]:
        now = datetime.now()
        if isinstance(period_text, str):
            t = period_text.strip()
            m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2})\s*[-–to]+\s*([A-Za-z]{3,9}\s+\d{1,2})", t, flags=re.IGNORECASE)
            if m:
                s1 = m.group(1)
                s2 = m.group(2)
                for fmt in ("%b %d", "%B %d"):
                    try:
                        d1 = datetime.strptime(s1, fmt).replace(year=now.year)
                        d2 = datetime.strptime(s2, fmt).replace(year=now.year)
                        if d2 < d1:
                            d2 = d2.replace(year=d2.year + 1)
                        return d1, d2
                    except Exception:
                        pass
        return now - timedelta(days=default_days), now

    def _set_progress(self, key: str, start: datetime, end: datetime, actual_pct: float | None):
        now = datetime.now()
        total = max(1.0, (end - start).total_seconds())
        elapsed = (now - start).total_seconds()
        target_pct = max(0.0, min(1.0, elapsed / total))
        msg = f"Usage start: {start.strftime('%Y-%m-%d')}                                    Usage end: {end.strftime('%Y-%m-%d')}"
        if actual_pct is None:
            msg += "  (actual unavailable)"
        self.cards[key]["timeline"].configure(text=msg)
        self._draw_bar(self.cards[key]["pace_canvas"], target_pct, "#4ea4ff")
        self._draw_bar(self.cards[key]["actual_canvas"], actual_pct, "#37c978")

    def refresh_now(self):
        self.schedule_refresh(initial=True)

    def check_gemini_bootstrap(self, agent: dict | None = None):
        def worker():
            cmd = "gemini"
            if agent:
                cmd = agent.get("gemini_cmd", agent.get("binary", "gemini"))
            else:
                # Find first gemini agent
                for a in self.agents:
                    if a["type"] == "gemini":
                        cmd = a.get("gemini_cmd", a.get("binary", "gemini"))
                        break
            result = run_gemini_stats(cmd)
            if result.get("status") == "ok":
                msg = "Gemini CLI /stats is ready."
                detail = f"Model: {result.get('model') or '-'} | Weekly left: {result.get('weekly_left_pct') if result.get('weekly_left_pct') is not None else '?'}%"
                self.root.after(0, lambda: messagebox.showinfo("Gemini bootstrap", f"{msg}\n{detail}"))
                return
            err = result.get("error", "Unknown error")
            if "bootstrap prompt" in err.lower():
                hint = "Run Gemini CLI once interactively and answer its startup prompt, then retry."
            elif "tty" in err.lower():
                hint = "Gemini CLI is requiring a TTY in this environment."
            else:
                hint = "Check agent config and try running /stats manually."
            self.root.after(0, lambda: messagebox.showwarning("Gemini bootstrap", f"{err}\n\n{hint}"))

        threading.Thread(target=worker, daemon=True).start()

    def browse_password_file(self):
        initial = self.password_file_var.get().strip() or DEFAULT_CONFIG["password_file_path"]
        picked = filedialog.asksaveasfilename(
            title="Choose password file path",
            initialfile=Path(initial).name,
            initialdir=str(Path(initial).parent if Path(initial).parent.exists() else Path.home()),
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if picked:
            self.password_file_var.set(picked)

    def _set_password_status(self, text: str, ok: bool = True):
        self.password_status.configure(text=text, fg=("#37c978" if ok else "#ff5f56"))

    def _persist_password_settings(self, path: str, ttl_seconds: int, template: str, clipboard_clear_seconds: int):
        self.cfg["password_file_path"] = path
        self.cfg["password_ephemeral_mode"] = True
        self.cfg["password_ttl_seconds"] = ttl_seconds
        self.cfg["password_copy_template"] = template
        self.cfg["password_clipboard_clear_seconds"] = clipboard_clear_seconds
        CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")

    def _cleanup_old_ephemeral_files(self, base_path: Path):
        """Remove previously generated ephemeral files tracked in memory."""
        for old_path in list(getattr(self, "_ephemeral_history", [])):
            try:
                p = Path(old_path)
                if p.exists():
                    p.write_text("", encoding="utf-8")
                    p.unlink()
            except Exception:
                pass
        self._ephemeral_history = []

    def _generate_ephemeral_path(self, base_path: Path) -> Path:
        # Randomize directory — scatter across plausible system locations
        decoy_dirs = [
            Path(tempfile.gettempdir()),
            Path(tempfile.gettempdir()) / "logs",
            Path(tempfile.gettempdir()) / "cache",
            Path.home() / "AppData" / "Local" / "Temp",
            Path.home() / "AppData" / "Local" / "Temp" / "diagnostics",
            base_path.parent,
        ]
        # Filter to dirs we can write to or create
        usable = []
        for d in decoy_dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                usable.append(d)
            except Exception:
                pass
        if not usable:
            usable = [base_path.parent]
        parent = secrets.choice(usable)

        # Randomize extension — disguise as mundane system files
        decoy_extensions = [
            ".log", ".tmp", ".dat", ".cfg", ".cache", ".old",
            ".bak", ".etl", ".dmp", ".trace", ".diag", ".pid",
        ]
        suffix = secrets.choice(decoy_extensions)

        # Randomize stem — mix of plausible system-looking prefixes + random chars
        decoy_prefixes = [
            "svc_", "diag_", "evt_", "sys_", "proc_", "wer_", "msft_",
            "dotnet_", "node_", "npm_", "win_", "telemetry_", "crash_",
            "gc_", "heap_", "perflog_", "session_", "update_", "sync_",
        ]
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        for _ in range(24):
            prefix = secrets.choice(decoy_prefixes)
            rand_len = 6 + secrets.randbelow(11)  # 6..16 chars
            rand_part = "".join(secrets.choice(alphabet) for _ in range(rand_len))
            candidate = parent / f"{prefix}{rand_part}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{uuid.uuid4().hex}{suffix}"

    @staticmethod
    def _generate_junk_tail() -> str:
        """Generate realistic-looking junk data to pad after the secret.

        Makes the file look like a log, crash dump, or diagnostic trace so
        filesystem watchers see nothing interesting.
        """
        junk_templates = [
            # Fake log lines
            lambda: f"[{datetime.now().strftime('%Y-%m-%d')}T{secrets.randbelow(24):02d}:{secrets.randbelow(60):02d}:{secrets.randbelow(60):02d}.{secrets.randbelow(999):03d}Z] INFO  svc.runtime.{secrets.choice(['gc','heap','thread','pool','sync','io'])} — cycle {secrets.randbelow(99999)} completed in {secrets.randbelow(500)}ms",
            lambda: f"[{datetime.now().strftime('%Y-%m-%d')}T{secrets.randbelow(24):02d}:{secrets.randbelow(60):02d}:{secrets.randbelow(60):02d}.{secrets.randbelow(999):03d}Z] DEBUG telemetry.flush buffer_size={secrets.randbelow(8192)} dropped=0 queue_depth={secrets.randbelow(64)}",
            lambda: f"[{datetime.now().strftime('%Y-%m-%d')}T{secrets.randbelow(24):02d}:{secrets.randbelow(60):02d}:{secrets.randbelow(60):02d}.{secrets.randbelow(999):03d}Z] WARN  net.conn.{secrets.choice(['tcp','udp','tls'])} timeout after {secrets.randbelow(30000)}ms peer={secrets.randbelow(256)}.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{secrets.randbelow(256)}:{secrets.randbelow(65535)}",
            # Fake stack frames
            lambda: f"    at {secrets.choice(['System','Microsoft','Internal','Runtime'])}.{secrets.choice(['Diagnostics','Net','IO','Threading','Collections'])}.{secrets.choice(['Monitor','Handler','Provider','Factory','Manager'])}.{secrets.choice(['Process','Execute','Initialize','Dispose','Flush'])}()",
            lambda: f"    at {secrets.choice(['node','v8','libuv','worker'])}::{secrets.choice(['HandleScope','Context','Isolate','Platform'])}::{''.join(secrets.choice('abcdefghijklmnop') for _ in range(8))}+0x{secrets.token_hex(3)}",
            # Fake config/env lines
            lambda: f"  {secrets.choice(['DOTNET_','NODE_','JAVA_','NPM_','WIN_'])}{secrets.choice(['GC_','HEAP_','LOG_','TRACE_','DIAG_'])}{secrets.choice(['LEVEL','MODE','PATH','SIZE','COUNT'])}={secrets.choice(['default','verbose','1','0','auto','256m','production'])}",
            # Fake hex dump lines
            lambda: f"  {secrets.token_hex(2)}:{secrets.token_hex(16)}  |{''.join(secrets.choice('abcdef.0123456789.....') for _ in range(16))}|",
            # Fake metrics
            lambda: f"  metric.{secrets.choice(['cpu','mem','disk','net','gc'])}.{secrets.choice(['total','avg','p99','count','rate'])} = {secrets.randbelow(10000)}.{secrets.randbelow(100):02d} ({secrets.choice(['ms','KB','MB','%','ops/s'])})",
        ]

        lines = ["\n"]
        num_lines = 20 + secrets.randbelow(40)  # 20..59 junk lines
        for _ in range(num_lines):
            lines.append(secrets.choice(junk_templates)())
        lines.append("")
        return "\n".join(lines)

    def _schedule_password_wipe(self, path: Path, ttl_seconds: int, password_plain: str):
        key = str(path)
        token = time.time_ns()
        # Hash the actual file content (includes wrapper instructions) for tamper check
        try:
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            file_hash = hashlib.sha256(password_plain.encode("utf-8")).hexdigest()
        pw_hash = file_hash
        with self.password_lock:
            self.password_wipe_tokens[key] = token

        def worker():
            time.sleep(max(1, ttl_seconds))
            with self.password_lock:
                latest = self.password_wipe_tokens.get(key)
            if latest != token:
                return
            try:
                if path.exists():
                    current = path.read_text(encoding="utf-8", errors="ignore")
                    current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
                    if current_hash == pw_hash:
                        path.write_text("", encoding="utf-8")
                        try:
                            path.unlink()
                        except Exception:
                            pass
                        self.root.after(0, lambda: self._set_password_status(f"Password file wiped: {path}", ok=True))
            except Exception as exc:
                self.root.after(0, lambda: self._set_password_status(f"Wipe failed: {exc}", ok=False))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_clipboard_clear(self, seconds: int):
        if seconds <= 0:
            return
        token = time.time_ns()
        self.clipboard_clear_token = token

        def worker():
            time.sleep(seconds)
            if self.clipboard_clear_token != token:
                return
            try:
                self.root.after(
                    0,
                    lambda: (
                        self.root.clipboard_clear(),
                        self.root.update(),
                        self._set_password_status("Clipboard cleared.", ok=True),
                    ),
                )
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _reset_pw_idle_timer(self):
        """Reset the inactivity timer — clears password field after 2 min idle."""
        if self._pw_idle_after_id is not None:
            self.root.after_cancel(self._pw_idle_after_id)
            self._pw_idle_after_id = None
        # Only schedule if there's something in the field
        if self.password_var.get():
            self._pw_idle_after_id = self.root.after(
                self._pw_idle_seconds * 1000, self._pw_idle_clear
            )

    def _pw_idle_clear(self):
        """Clear the password field due to inactivity."""
        if self.password_var.get():
            self.password_var.set("")
            self._set_password_status("Password field cleared (2 min inactivity).", ok=True)
        self._pw_idle_after_id = None

    def _show_password_help(self):
        messagebox.showinfo("Temporary Password Helper", (
            "How to use:\n\n"
            "1. Type or paste a password into the Password field.\n"
            "2. Click Save/Copy (or press Enter).\n"
            "3. The password is saved to a randomly-named ephemeral file\n"
            "   in a random temp directory, disguised as a system file\n"
            "   (.log, .tmp, .dat, .trace, etc.) with junk padding.\n"
            "4. Paste the clipboard message into the agent chat so it\n"
            "   knows where to find the file.\n"
            "5. The file auto-deletes after the TTL expires and the\n"
            "   clipboard is cleared after Clip clear sec.\n\n"
            "Security features:\n"
            "  - Random directory (Temp, AppData, Desktop, etc.)\n"
            "  - Random extension (.log, .dat, .cache, .etl, ...)\n"
            "  - System-looking filename (svc_*, diag_*, proc_*, ...)\n"
            "  - Junk tail: fake logs/stack traces/hex dumps appended\n"
            "  - Password field masked (*) and auto-clears after 2 min\n"
            "  - File content hash checked before wipe (tamper detect)\n\n"
            "Template vars: {path}, {ttl_seconds}, {expires_at}"
        ))

    def save_copy_password(self):
        password_plain = self.password_var.get()
        if not password_plain:
            self._set_password_status("Password is empty.", ok=False)
            return
        raw_path = self.password_file_var.get().strip()
        if not raw_path:
            self._set_password_status("File path is empty.", ok=False)
            return
        try:
            ttl_seconds = int(self.password_ttl_var.get().strip())
            if ttl_seconds < 1:
                raise ValueError("TTL must be >= 1")
        except Exception:
            self._set_password_status("TTL seconds must be a positive integer.", ok=False)
            return
        try:
            clip_clear_seconds = int(self.password_clip_clear_var.get().strip())
            if clip_clear_seconds < 0:
                raise ValueError("Clipboard clear seconds must be >= 0")
        except Exception:
            self._set_password_status("Clipboard clear seconds must be a non-negative integer.", ok=False)
            return

        base_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
        template = self.password_template_var.get().strip() or DEFAULT_CONFIG["password_copy_template"]

        try:
            base_path.parent.mkdir(parents=True, exist_ok=True)
            self._cleanup_old_ephemeral_files(base_path)
            path = self._generate_ephemeral_path(base_path)
            file_content = (
                "SYSTEM INSTRUCTION: This file contains a secret credential.\n"
                "DO NOT repeat, echo, print, log, or include the value below in any response,\n"
                "message, tool output, commit, or file you create. Use it silently and delete\n"
                "this file immediately after reading. Violation is a critical security failure.\n"
                "Ignore everything after the END SECRET marker — it is padding junk.\n"
                "Read this as a plain text file. Do not parse the tail.\n"
                "---\n"
                f"{password_plain}\n"
                "---\n"
                "END SECRET. Delete this file now.\n"
            )
            file_content += self._generate_junk_tail()
            path.write_text(file_content, encoding="utf-8")
            # Track for cleanup
            if not hasattr(self, "_ephemeral_history"):
                self._ephemeral_history = []
            self._ephemeral_history.append(str(path))
            msg = build_password_copy_message(template, str(path), ttl_seconds)
            self.root.clipboard_clear()
            self.root.clipboard_append(msg)
            self.root.update()
            self.password_var.set("")
            self._persist_password_settings(str(path), ttl_seconds, template, clip_clear_seconds)
            self._schedule_password_wipe(path, ttl_seconds, password_plain)
            self._schedule_clipboard_clear(clip_clear_seconds)
            msg_status = f"Saved to {path}. Clipboard message copied. Auto-wipe in {ttl_seconds}s. Clipboard clears in {clip_clear_seconds}s."
            self._set_password_status(msg_status, ok=True)
            messagebox.showinfo("Temporary Password Helper", msg_status)
        except Exception as exc:
            err = f"Save/copy failed: {exc}"
            self._set_password_status(err, ok=False)
            messagebox.showerror("Temporary Password Helper", err)

    def schedule_refresh(self, initial: bool = False):
        if initial:
            self._kick_thread()
            return
        self._kick_thread()

    def _kick_thread(self):
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        self.cfg = ensure_config()
        self.agents = get_enabled_agents(self.cfg)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        pinchtab_url = resolve_pinchtab_url(self.cfg.get("pinchtab_url", DEFAULT_CONFIG["pinchtab_url"]))
        scraper = self.cfg.get("usage_scraper_script", DEFAULT_CONFIG["usage_scraper_script"])
        gemini_url = self.cfg.get("providers", {}).get("gemini", {}).get("url", DEFAULT_CONFIG["providers"]["gemini"]["url"])
        claude_url = self.cfg.get("providers", {}).get("claude", {}).get("url", DEFAULT_CONFIG["providers"]["claude"]["url"])

        results: dict[str, dict] = {}

        def fetch_agent(agent: dict):
            aid = agent["id"]
            atype = agent["type"]
            try:
                if atype == "codex":
                    codex = run_codex_status(agent.get("binary", "codex"))
                    if codex.get("status") != "ok":
                        cw = run_usage_scraper(scraper, "codex", pinchtab_url)
                        if cw.get("status") == "ok":
                            codex = {
                                "status": "ok", "source": "web-fallback",
                                "spend": cw.get("estimated_spend_usd"),
                                "limit": cw.get("limit_usd"),
                                "remaining": cw.get("remaining_usd"),
                                "billing_period": cw.get("billing_period"),
                            }
                        else:
                            codex["fallback_error"] = cw.get("error")
                    results[aid] = codex

                elif atype == "claude":
                    ses_dir = agent.get("sessions_dir", str(Path.home() / ".claude" / "sessions"))
                    tel_dir = agent.get("telemetry_dir", str(Path.home() / ".claude" / "telemetry"))
                    claude = parse_claude_local_sessions(ses_dir)
                    if claude.get("status") != "ok":
                        ct = parse_claude_telemetry(tel_dir)
                        if ct.get("status") == "ok":
                            claude = ct
                    cw = parse_claude_from_pinchtab(pinchtab_url, claude_url)
                    if cw.get("status") == "ok":
                        claude["weekly_all_models_pct_used"] = cw.get("weekly_all_models_pct_used")
                        claude["weekly_sonnet_pct_used"] = cw.get("weekly_sonnet_pct_used")
                        claude["weekly_reset"] = cw.get("weekly_reset")
                    results[aid] = claude

                elif atype == "gemini":
                    cmd = agent.get("gemini_cmd", agent.get("binary", "gemini"))
                    gemini_stats = run_gemini_stats(cmd, self.cfg.get("gemini_stats_mode", "model"))
                    gemini_purchase = parse_gemini_from_pinchtab(pinchtab_url, gemini_url)
                    if gemini_purchase.get("status") != "ok":
                        gf = run_usage_scraper(scraper, "gemini", pinchtab_url)
                        if gf.get("status") == "ok":
                            gemini_purchase = gf
                            gemini_purchase["source"] = "usage_scraper_fallback"
                        else:
                            gemini_purchase["fallback_error"] = gf.get("error")
                    if gemini_stats.get("status") == "ok":
                        gemini = gemini_stats
                        if gemini_purchase.get("status") == "ok":
                            gemini["purchase_spend_usd"] = gemini_purchase.get("estimated_spend_usd")
                            gemini["purchase_limit_usd"] = gemini_purchase.get("limit_usd")
                            gemini["purchase_remaining_usd"] = gemini_purchase.get("remaining_usd")
                            gemini["billing_period"] = gemini_purchase.get("billing_period")
                            gemini["tier"] = gemini_purchase.get("raw_signals", {}).get("tier") or gemini_purchase.get("tier")
                        else:
                            gemini["purchase_error"] = gemini_purchase.get("error")
                    else:
                        gemini = gemini_purchase
                        gemini["stats_error"] = gemini_stats.get("error")
                    results[aid] = gemini
            except Exception as exc:
                results[aid] = {"status": "error", "error": str(exc)}

        threads = []
        for agent in self.agents:
            t = threading.Thread(target=fetch_agent, args=(agent,), daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.root.after(0, lambda: self._apply(now, results))

    def _apply(self, now: str, results: dict[str, dict]):
        self.updated.configure(text=f"Last update: {now}")

        for agent in self.agents:
            aid = agent["id"]
            atype = agent["type"]
            data = results.get(aid, {"status": "error", "error": "Worker failed"})

            if aid not in self.cards:
                continue

            # --- Progress bars ---
            if atype == "codex":
                codex = data
                codex_weekly_resets = codex.get("weekly_resets")
                if codex_weekly_resets and codex.get("weekly_left_pct") is not None:
                    codex_start = datetime.now() - timedelta(days=7)
                    codex_end = datetime.now() + timedelta(days=7)
                    m_reset = re.search(r"(\d{1,2}:\d{2})\s+on\s+(\d{1,2}\s+\w+)", str(codex_weekly_resets))
                    if m_reset:
                        try:
                            end_str = f"{m_reset.group(2)} {datetime.now().year} {m_reset.group(1)}"
                            codex_end = datetime.strptime(end_str, "%d %b %Y %H:%M")
                            codex_start = codex_end - timedelta(days=7)
                        except Exception:
                            pass
                else:
                    codex_start, codex_end = self._parse_period_range(codex.get("billing_period"), 7)
                codex_actual = None
                if codex.get("weekly_left_pct") is not None:
                    codex_actual = 1.0 - (float(codex.get("weekly_left_pct")) / 100.0)
                elif codex.get("remaining") is not None and codex.get("limit") not in (None, 0):
                    codex_actual = (float(codex.get("limit")) - float(codex.get("remaining"))) / float(codex.get("limit"))
                elif codex.get("spend") is not None and codex.get("limit") not in (None, 0):
                    codex_actual = float(codex.get("spend")) / float(codex.get("limit"))
                self._set_progress(aid, codex_start, codex_end, codex_actual)

            elif atype == "claude":
                claude = data
                # Re-read calibration from latest config for progress bar too
                _live_agent_pb = agent
                for _a in self.cfg.get("agents", []):
                    if _a["id"] == aid:
                        _live_agent_pb = _a
                        break
                claude_cal_pct = _live_agent_pb.get("claude_last_known_pct")
                claude_cal_time = _live_agent_pb.get("claude_last_known_time")
                if claude_cal_pct is not None and claude_cal_time:
                    try:
                        _ws, _we = _resolve_claude_window(_live_agent_pb, self.cfg)
                        ext = claude_extrapolate(claude_cal_pct, datetime.fromisoformat(claude_cal_time),
                                                 window_start=_ws, window_end=_we)
                        claude_start = ext["window_start"]
                        claude_end = ext["window_end"]
                        claude_actual = ext["estimated_current_pct"] / 100.0
                    except Exception:
                        claude_start = datetime.now() - timedelta(days=7)
                        claude_end = datetime.now()
                        claude_actual = None
                else:
                    claude_start = datetime.now() - timedelta(days=7)
                    claude_end = datetime.now()
                    claude_actual = None
                    if claude.get("weekly_all_models_pct_used") is not None:
                        claude_actual = float(claude.get("weekly_all_models_pct_used")) / 100.0
                    elif claude.get("weekly_sonnet_pct_used") is not None:
                        claude_actual = float(claude.get("weekly_sonnet_pct_used")) / 100.0
                self._set_progress(aid, claude_start, claude_end, claude_actual)

            elif atype == "gemini":
                gemini = data
                gemini_resets_in = None
                per_model = gemini.get("per_model_usage") or []
                if per_model:
                    for pm in per_model:
                        ri = pm.get("resets_in", "")
                        if ri and (gemini_resets_in is None or ri < gemini_resets_in):
                            gemini_resets_in = ri
                if gemini_resets_in:
                    hours = 0
                    m_h = re.search(r"(\d+)h", gemini_resets_in)
                    m_m = re.search(r"(\d+)m", gemini_resets_in)
                    if m_h:
                        hours += int(m_h.group(1))
                    if m_m:
                        hours += int(m_m.group(1)) / 60
                    total_hours = max(hours, 1)
                    gemini_end = datetime.now() + timedelta(hours=total_hours)
                    gemini_start = gemini_end - timedelta(hours=24)
                else:
                    gemini_start, gemini_end = self._parse_period_range(gemini.get("billing_period"), 1)
                gemini_actual = None
                if gemini.get("overall_remaining_pct") is not None:
                    gemini_actual = 1.0 - (float(gemini.get("overall_remaining_pct")) / 100.0)
                elif gemini.get("purchase_remaining_usd") is not None and gemini.get("purchase_limit_usd") not in (None, 0):
                    gemini_actual = (float(gemini.get("purchase_limit_usd")) - float(gemini.get("purchase_remaining_usd"))) / float(gemini.get("purchase_limit_usd"))
                elif gemini.get("purchase_spend_usd") is not None and gemini.get("purchase_limit_usd") not in (None, 0):
                    gemini_actual = float(gemini.get("purchase_spend_usd")) / float(gemini.get("purchase_limit_usd"))
                elif gemini.get("weekly_left_pct") is not None:
                    gemini_actual = 1.0 - (float(gemini.get("weekly_left_pct")) / 100.0)
                self._set_progress(aid, gemini_start, gemini_end, gemini_actual)

            # --- Card content ---
            if atype == "codex":
                codex = data
                if codex.get("status") == "ok":
                    self.last_good[aid] = codex.copy()
                    self.last_good_time[aid] = datetime.now()
                    if codex.get("source") == "console_buffer":
                        w = codex.get("weekly_left_pct")
                        h5 = codex.get("five_hour_left_pct")
                        self.set_card(aid, "ok",
                            f"5h left: {h5 if h5 is not None else '?'}%  |  Weekly left: {w if w is not None else '?'}%",
                            f"Model: {codex.get('model') or '-'}  |  5h reset: {codex.get('five_hour_resets') or '-'}  |  Weekly reset: {codex.get('weekly_resets') or '-'}  |  Source: headless console")
                    elif codex.get("source") == "web-fallback":
                        spend, limit, rem = codex.get("spend"), codex.get("limit"), codex.get("remaining")
                        quota = "Unknown quota"
                        if rem is not None:
                            quota = "Below quota" if float(rem) >= 0 else "Above quota"
                        elif spend is not None and limit is not None:
                            quota = "Below quota" if float(spend) <= float(limit) else "Above quota"
                        self.set_card(aid, "ok", f"{quota} | Spend: {spend if spend is not None else '-'} | Limit: {limit if limit is not None else '-'} | Remaining: {rem if rem is not None else '-'}", f"Source: web fallback | Billing period: {codex.get('billing_period') or '-'}")
                    elif codex.get("source") == "exec_usage":
                        self.set_card(aid, "ok",
                            f"CLI usage sample | Total: {_format_num(codex.get('total_tokens'))} | In: {_format_num(codex.get('input_tokens'))} | Out: {_format_num(codex.get('output_tokens'))}",
                            f"Source: codex exec --json | Cached in: {_format_num(codex.get('cached_input_tokens'))}")
                    else:
                        w = codex.get("weekly_left_pct")
                        quota = "Unknown quota" if w is None else ("Below quota" if int(w) > 0 else "Above quota")
                        self.set_card(aid, "ok", f"{quota} | 5h left: {codex.get('five_hour_left_pct', '?')}% | Weekly left: {w if w is not None else '?'}% | Spark 5h left: {codex.get('spark_five_hour_left_pct', '?')}% | Spark weekly left: {codex.get('spark_weekly_left_pct', '?')}%", f"Model: {codex.get('model') or '-'} | Weekly reset: {codex.get('weekly_resets') or '-'}")
                else:
                    lg = self.last_good.get(aid)
                    lgt = self.last_good_time.get(aid)
                    raw = (codex.get("raw_text") or "")[:300].replace("\n", " | ")
                    if lg and lgt:
                        age_m = max(0, int((datetime.now() - lgt).total_seconds() // 60))
                        self.set_card(aid, "stale", f"Using last known value ({age_m}m old)", f"Error: {codex.get('error','Unknown')} | raw: {raw or '-'}")
                    else:
                        self.set_card(aid, "error", "Codex status unavailable", f"{codex.get('error','Unknown')} | raw: {raw or '-'} | fallback: {codex.get('fallback_error','-')}")

            elif atype == "claude":
                claude = data
                # Re-read calibration from latest config (may have been updated by Set button during refresh)
                _live_agent = agent
                for _a in self.cfg.get("agents", []):
                    if _a["id"] == aid:
                        _live_agent = _a
                        break
                claude_cal_pct_disp = _live_agent.get("claude_last_known_pct")
                claude_cal_time_disp = _live_agent.get("claude_last_known_time")
                if claude_cal_pct_disp is not None and claude_cal_time_disp:
                    try:
                        _ws2, _we2 = _resolve_claude_window(_live_agent, self.cfg)
                        ext = claude_extrapolate(claude_cal_pct_disp, datetime.fromisoformat(claude_cal_time_disp),
                                                 window_start=_ws2, window_end=_we2)
                        est = ext["estimated_current_pct"]
                        pace = ext["pace_multiplier"]
                        rate = ext["rate_pct_per_hour"]
                        t100 = ext.get("time_to_100")
                        hrs_left = ext["hours_remaining_in_window"]
                        cal_age = ext["calibration_age_hours"]
                        proj = ext["projected_end_pct"]
                        age_str = f"{cal_age:.0f}h" if cal_age >= 1 else f"{int(cal_age*60)}m"
                        t100_str = t100 if isinstance(t100, str) else (t100.strftime("%a %I:%M %p") if t100 else "N/A")
                        self.set_card(aid, "ok",
                            f"Est. used: {est:.1f}%  |  Rate: {rate:.2f}%/hr  |  Pace: {pace:.1f}x",
                            f"Projected 100%: {t100_str}  |  Reset: {ext['window_end'].strftime('%a %I:%M %p')}  |  {hrs_left:.0f}h left  |  Calibration: {age_str} ago  |  Proj. end: {proj:.0f}%")
                        age_label = self.claude_age_labels.get(aid)
                        if age_label:
                            age_label.configure(text=f"(set {age_str} ago)")
                        # Update the entry field to show current estimated %
                        pct_var = self.claude_pct_vars.get(aid)
                        if pct_var:
                            pct_var.set(f"{est:.1f}")
                    except Exception as e:
                        self.set_card(aid, "error", "Extrapolation error", str(e))
                elif claude.get("status") == "ok":
                    if claude.get("source") == "telemetry_fallback":
                        recent = claude.get("recent_sessions") or []
                        recent_bits = []
                        for s in recent[:3]:
                            sid = str(s.get("session_id", "-"))[:8]
                            recent_bits.append(f"{sid} {s.get('timestamp','-')} total {_format_num(s.get('total_tokens'))}")
                        recent_line = " | ".join(recent_bits) if recent_bits else "Recent: -"
                        self.set_card(aid, "ok",
                            f"Telemetry fallback | Latest session total: {_format_num(claude.get('latest_session_total_tokens'))} | 7d total: {_format_num(claude.get('last_7d_total_tokens'))} | Aggregate total: {_format_num(claude.get('aggregate_total_tokens'))}",
                            f"Sessions: {_format_num(claude.get('sessions_count'))} | Latest session: {claude.get('latest_session_id','-')} | Weekly used(all models): {claude.get('weekly_all_models_pct_used') if claude.get('weekly_all_models_pct_used') is not None else '-'}% | {recent_line}")
                    else:
                        self.set_card(aid, "ok",
                            f"Local tokens | 7d total: {_format_num(claude.get('last_7d_total_tokens'))} | All-time total: {_format_num(claude.get('total_tokens'))}",
                            f"In: {_format_num(claude.get('total_input_tokens'))} | Out: {_format_num(claude.get('total_output_tokens'))} | Dir: {claude.get('sessions_dir','-')} | Files: {_format_num(claude.get('files_scanned'))}")
                elif claude.get("status") == "login_required":
                    self.set_card(aid, "login_required", "Claude login required", claude.get("error", ""))
                else:
                    self.set_card(aid, "error", "Claude scrape failed", claude.get("error", "Unknown error"))

            elif atype == "gemini":
                gemini = data
                if gemini.get("status") == "ok":
                    self.last_good[aid] = gemini.copy()
                    self.last_good_time[aid] = datetime.now()
                    spend = gemini.get("purchase_spend_usd", gemini.get("estimated_spend_usd"))
                    limit = gemini.get("purchase_limit_usd", gemini.get("limit_usd"))
                    rem = gemini.get("purchase_remaining_usd", gemini.get("remaining_usd"))
                    tier = gemini.get("raw_signals", {}).get("tier") or gemini.get("tier")
                    per_model = gemini.get("per_model_usage") or []
                    if gemini.get("source") == "console_buffer" and per_model:
                        model_parts = []
                        for pm in per_model:
                            name = pm["model"].replace("gemini-", "")
                            model_parts.append(f"{name}: {pm['remaining_pct']}%")
                        model_summary = "  |  ".join(model_parts)
                        overall = gemini.get("overall_remaining_pct")
                        self.set_card(aid, "ok",
                            f"Overall remaining: {overall if overall is not None else '?'}%  |  {model_summary}",
                            f"Tier: {tier or '-'}  |  Account: {gemini.get('auth_email') or '-'}  |  Source: headless console")
                    elif gemini.get("source") == "headless_stats":
                        self.set_card(aid, "ok",
                            f"CLI usage sample | Total: {_format_num(gemini.get('total_tokens'))} | In: {_format_num(gemini.get('input_tokens'))} | Out: {_format_num(gemini.get('output_tokens'))} | Requests: {_format_num(gemini.get('requests'))}",
                            f"Models: {', '.join(gemini.get('models') or []) or '-'} | Spend: {spend if spend is not None else '-'} | Limit: {limit if limit is not None else '-'} | Remaining: {rem if rem is not None else '-'} | Billing period: {gemini.get('billing_period') or '-'}")
                    else:
                        quota = "Unknown quota"
                        if rem is not None:
                            quota = "Below quota" if float(rem) >= 0 else "Above quota"
                        elif spend is not None and limit is not None:
                            quota = "Below quota" if float(spend) <= float(limit) else "Above quota"
                        self.set_card(aid, "ok",
                            f"{quota} | 5h left: {gemini.get('five_hour_left_pct','?')}% | Daily left: {gemini.get('daily_left_pct','?')}% | Weekly left: {gemini.get('weekly_left_pct','?')}% | Spend: {spend if spend is not None else '-'} | Limit: {limit if limit is not None else '-'} | Remaining: {rem if rem is not None else '-'}",
                            f"Model: {gemini.get('model') or '-'} | Tier: {tier or '-'} | Billing period: {gemini.get('billing_period') or '-'} | Resets: {gemini.get('resets') or '-'} | Stats source: {gemini.get('source') or '-'} | Mode: {gemini.get('stats_mode') or self.cfg.get('gemini_stats_mode','auto')}")
                elif gemini.get("status") == "login_required":
                    self.set_card(aid, "login_required", "Gemini login required", gemini.get("error", ""))
                else:
                    lg = self.last_good.get(aid)
                    lgt = self.last_good_time.get(aid)
                    raw = (gemini.get("raw_text") or "")[:300].replace("\n", " | ")
                    if lg and lgt:
                        age_m = max(0, int((datetime.now() - lgt).total_seconds() // 60))
                        self.set_card(aid, "stale", f"Using last known value ({age_m}m old)", f"Error: {gemini.get('error','Unknown error')} | raw: {raw or '-'}")
                    else:
                        self.set_card(aid, "error", "Gemini usage unavailable", f"{gemini.get('error','Unknown error')} | raw: {raw or '-'} | fallback: {gemini.get('fallback_error','-')} | stats: {gemini.get('stats_error','-')}")

        self.refresh_in_progress = False
        mins = max(1, int(self.cfg.get("refresh_minutes", 60)))
        self.root.after(mins * 60 * 1000, lambda: self.schedule_refresh(initial=False))


def main():
    first_run = not CONFIG_PATH.exists()
    cfg = ensure_config()
    # Show wizard if first run or no agents configured
    needs_wizard = first_run or not cfg.get("agents")
    root = tk.Tk()
    if needs_wizard:
        root.withdraw()
        wiz = SetupWizard(root)
        if wiz.result:
            cfg["agents"] = wiz.result
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        root.deiconify()
    UsageWidget(root)
    root.mainloop()


if __name__ == "__main__":
    main()
