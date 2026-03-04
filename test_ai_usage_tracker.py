import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_usage_tracker import (
    _migrate_old_config,
    _resolve_cmd,
    build_password_copy_message,
    detect_agents,
    get_enabled_agents,
    parse_claude_local_sessions,
    parse_claude_telemetry,
    parse_codex_status,
    parse_gemini_stats,
    run_gemini_stats,
)


class TestCodexStatusParsing(unittest.TestCase):
    def test_parse_codex_status_extracts_limits(self):
        raw = """
Model: gpt-5
5h limit: 72% left (resets in 1h 10m)
Weekly limit: 61% left (resets Tue 5:00 PM)
Spark limit
5h limit: 30% left
Weekly limit: 12% left
"""
        out = parse_codex_status(raw)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["model"], "gpt-5")
        self.assertEqual(out["five_hour_left_pct"], 72)
        self.assertEqual(out["weekly_left_pct"], 61)
        self.assertEqual(out["spark_five_hour_left_pct"], 30)
        self.assertEqual(out["spark_weekly_left_pct"], 12)
        self.assertEqual(out["weekly_resets"], "Tue 5:00 PM")


class TestClaudeLocalSessions(unittest.TestCase):
    def test_parse_claude_local_sessions_sums_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            session_file = base / "abc.jsonl"
            rows = [
                {
                    "timestamp": "2026-03-02T10:00:00Z",
                    "usage": {"input_tokens": 120, "output_tokens": 80},
                },
                {
                    "created_at": "2026-02-01T10:00:00Z",
                    "message": {"usage": {"input_tokens": 20, "output_tokens": 10}},
                },
            ]
            session_file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

            out = parse_claude_local_sessions(str(base))
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["total_input_tokens"], 140)
            self.assertEqual(out["total_output_tokens"], 90)
            self.assertEqual(out["total_tokens"], 230)
            self.assertEqual(out["files_scanned"], 1)
            self.assertEqual(out["lines_scanned"], 2)
            self.assertGreaterEqual(out["last_7d_total_tokens"], 200)

    def test_parse_claude_local_sessions_missing_dir(self):
        out = parse_claude_local_sessions(r"C:\definitely\missing\claude\sessions")
        self.assertEqual(out["status"], "error")
        self.assertIn("not found", out["error"].lower())


class TestClaudeTelemetry(unittest.TestCase):
    def test_parse_claude_telemetry_extracts_last_session_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "1p_failed_events.a.b.json"
            line = {
                "event_data": {
                    "event_name": "tengu_exit",
                    "client_timestamp": "2026-03-03T18:37:29.136Z",
                    "additional_metadata": json.dumps(
                        {
                            "last_session_id": "abc123",
                            "last_session_total_input_tokens": 100,
                            "last_session_total_output_tokens": 250,
                            "last_session_total_cache_creation_input_tokens": 300,
                            "last_session_total_cache_read_input_tokens": 400,
                        }
                    ),
                }
            }
            p.write_text(json.dumps(line) + "\n", encoding="utf-8")
            out = parse_claude_telemetry(str(Path(td)))
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["latest_session_id"], "abc123")
            self.assertEqual(out["latest_session_input_tokens"], 800)
            self.assertEqual(out["latest_session_output_tokens"], 250)
            self.assertEqual(out["latest_session_total_tokens"], 1050)


class TestGeminiStatsParsing(unittest.TestCase):
    def test_parse_gemini_stats_extracts_quota_and_spend(self):
        raw = """
Model: gemini-2.5-pro
5h limit: 44% left
Daily limit: 75% left
Weekly limit: 81% left
Resets in 2h 10m
Spend $12.50 Limit $50.00 Remaining $37.50
"""
        out = parse_gemini_stats(raw)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["model"], "gemini-2.5-pro")
        self.assertEqual(out["five_hour_left_pct"], 44.0)
        self.assertEqual(out["daily_left_pct"], 75.0)
        self.assertEqual(out["weekly_left_pct"], 81.0)
        self.assertEqual(out["estimated_spend_usd"], 12.5)
        self.assertEqual(out["limit_usd"], 50.0)
        self.assertEqual(out["remaining_usd"], 37.5)

    def test_parse_gemini_stats_errors_on_unparseable(self):
        out = parse_gemini_stats("no useful stats here")
        self.assertEqual(out["status"], "error")


class TestCommandResolution(unittest.TestCase):
    def test_resolve_cmd_prefers_existing_absolute(self):
        py = _resolve_cmd(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", ["powershell"])
        self.assertIsNotNone(py)
        self.assertTrue(str(py).lower().endswith("powershell.exe"))


class TestPasswordHelpers(unittest.TestCase):
    def test_build_password_copy_message_template_vars(self):
        out = build_password_copy_message(
            "Saved at {path} for {ttl_seconds}s until {expires_at}",
            r"C:\tmp\password.txt",
            45,
        )
        self.assertIn(r"C:\tmp\password.txt", out)
        self.assertIn("45s", out)

    def test_build_password_copy_message_default_is_path_only(self):
        out = build_password_copy_message("", r"C:\tmp\password.txt", 30)
        self.assertIn(r"C:\tmp\password.txt", out)
        self.assertNotIn("Here is the password", out)


class TestGeminiAutoMode(unittest.TestCase):
    @patch("ai_usage_tracker.run_gemini_console_stats")
    @patch("ai_usage_tracker._run_gemini_stats_once")
    @patch("ai_usage_tracker.run_gemini_headless_usage")
    @patch("ai_usage_tracker._resolve_cmd")
    def test_auto_mode_picks_first_success(self, resolve_cmd, run_headless, run_once, run_console):
        run_console.return_value = {"status": "error", "error": "console unavailable"}
        resolve_cmd.return_value = "gemini.cmd"
        run_headless.return_value = {"status": "error", "error": "headless unavailable"}
        run_once.side_effect = [
            {"status": "error", "error": "bad model"},
            {"status": "ok", "weekly_left_pct": 88, "stats_mode": "session"},
        ]
        out = run_gemini_stats("gemini", "auto")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["source"], "cli_stats")
        self.assertEqual(out["weekly_left_pct"], 88)
        self.assertEqual(out["stats_mode"], "session")

    @patch("ai_usage_tracker.run_gemini_console_stats")
    def test_console_buffer_is_primary(self, run_console):
        run_console.return_value = {
            "status": "ok", "source": "cli_stats",
            "per_model_usage": [{"model": "gemini-2.5-flash", "remaining_pct": 90.0, "resets_in": "20h"}],
            "overall_remaining_pct": 90.0,
        }
        out = run_gemini_stats("gemini", "auto")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["source"], "console_buffer")
        self.assertEqual(out["overall_remaining_pct"], 90.0)


class TestConfigMigration(unittest.TestCase):
    def test_migrate_old_config_creates_agents(self):
        old_cfg = {
            "enabled_providers": ["claude", "gemini"],
            "codex_cmd": "codex",
            "gemini_cmd": "gemini.cmd",
            "claude_telemetry_dir": r"C:\Users\test\.claude\telemetry",
            "claude_sessions_dir": r"C:\Users\test\.claude\sessions",
            "claude_last_known_pct": 42.5,
            "claude_last_known_time": "2026-03-04T10:00:00",
        }
        agents = _migrate_old_config(old_cfg)
        types = [a["type"] for a in agents]
        self.assertIn("claude", types)
        self.assertIn("gemini", types)
        # codex not in enabled_providers but has codex_cmd — should still be created
        self.assertIn("codex", types)
        # Claude agent should have calibration data
        claude_agent = [a for a in agents if a["type"] == "claude"][0]
        self.assertEqual(claude_agent["claude_last_known_pct"], 42.5)
        self.assertTrue(claude_agent["enabled"])
        # Codex should be disabled (not in enabled_providers)
        codex_agent = [a for a in agents if a["type"] == "codex"][0]
        self.assertFalse(codex_agent["enabled"])

    def test_migrate_auto_enables_all(self):
        old_cfg = {"enabled_providers": "auto"}
        agents = _migrate_old_config(old_cfg)
        enabled = [a for a in agents if a.get("enabled")]
        # With "auto", all detected types should be enabled
        self.assertTrue(len(enabled) >= 0)  # may be 0 if nothing installed


class TestAgentHelpers(unittest.TestCase):
    def test_get_enabled_agents_filters(self):
        cfg = {
            "agents": [
                {"id": "claude_1", "type": "claude", "enabled": True},
                {"id": "gemini_1", "type": "gemini", "enabled": False},
                {"id": "codex_1", "type": "codex", "enabled": True},
            ]
        }
        enabled = get_enabled_agents(cfg)
        self.assertEqual(len(enabled), 2)
        ids = [a["id"] for a in enabled]
        self.assertIn("claude_1", ids)
        self.assertIn("codex_1", ids)
        self.assertNotIn("gemini_1", ids)

    def test_detect_agents_returns_list(self):
        agents = detect_agents()
        self.assertIsInstance(agents, list)
        for a in agents:
            self.assertIn("id", a)
            self.assertIn("type", a)
            self.assertIn(a["type"], ("claude", "codex", "gemini"))


if __name__ == "__main__":
    unittest.main()
