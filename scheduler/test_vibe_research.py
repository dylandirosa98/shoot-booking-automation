import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from .game_opportunities import ScheduleResearchError
from . import vibe_research as vibe


def _effect(tool, url=None):
    detail = {"toolName": tool, "input": {"url": url} if url else {"query": "q"}}
    return {"type": "effect", "generationStatus": "completed", "detail": detail}


def _answer(obj):
    return {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "```json\n" + json.dumps(obj) + "\n```"}]}


class VibeResearchTests(TestCase):
    def setUp(self):
        self.day = "2026-11-21"
        self.rinks = ["Foxboro Sports Center"]
        self.game = {
            "date": self.day, "time": "3:00 PM", "away": "A", "home": "B", "division": "", "rink": "Foxboro Sports Center",
            "published_venue": "Foxboro Sports Center Rink 2", "site": "Example", "source_url": "https://example.org/sched",
            "source_excerpt": "Nov 21 3:00 PM A vs B at Foxboro Sports Center",
        }

    def test_agent_profile_disables_every_local_tool(self):
        profile = vibe.AGENT_PROFILE
        for tool in ("bash", "powershell", "git_bash", "read_file", "write_file", "edit", "task", "cron"):
            self.assertIn(f'"{tool}"', profile)
        self.assertIn("enable_subagents = false", profile)
        self.assertIn("enable_mcp = false", profile)

    def test_unopened_source_or_wrong_rink_is_discarded(self):
        hosts = {"example.org"}
        bad_host = dict(self.game, source_url="https://made-up.example.net/x")
        bad_rink = dict(self.game, rink="Some Other Rink", published_venue="Some Other Rink")
        no_evidence = dict(self.game, source_excerpt="")
        result = vibe.validate_result({"games": [self.game, bad_host, bad_rink, no_evidence]}, self.day, self.rinks, hosts)
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["sort_time"], "15:00")
        self.assertTrue(any("3 unverified" in note for note in result["limitations"]))

    def test_transcript_parsing(self):
        entries = [_effect("connector_web_search.web_search"), _effect("connector_web_search.open_url", "https://www.example.org/a"),
                   _effect("file_system.bash"), _answer({"games": []})]
        text, hosts, calls = vibe.parse_transcript(entries)
        self.assertEqual(calls, 2)
        self.assertEqual(hosts, {"www.example.org"})
        self.assertEqual(vibe._extract_json(text), {"games": []})
        self.assertTrue(vibe._host_matches("https://example.org/sched", hosts))

    @patch("scheduler.vibe_research.ensure_agent_profile")
    @patch("scheduler.vibe_research.vibe_executable", return_value="/bin/vibe")
    @patch("scheduler.vibe_research.subprocess.Popen")
    def test_run_sends_no_crm_env_and_returns_validated_games(self, popen, _exe, _profile):
        entries = [_effect("connector_web_search.open_url", "https://example.org/sched"),
                   _answer({"games": [self.game], "checked_rinks": self.rinks, "limitations": []})]
        process = Mock(returncode=0, pid=1)
        process.communicate.return_value = (json.dumps(entries), "")
        popen.return_value = process
        with patch.dict(os.environ, {"PIPEDRIVE_API_TOKEN": "secret", "OPPORTUNITIES_SYNC_SECRET": "x"}):
            result = vibe.run_vibe_research_for_day(self.day, self.rinks, [])
        self.assertEqual(len(result["games"]), 1)
        env = popen.call_args.kwargs["env"]
        self.assertFalse(any(k.startswith("PIPEDRIVE_") or "SECRET" in k for k in env))
        cmd = popen.call_args.args[0]
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "rink-research")
        prompt = Path(cmd[cmd.index("--prompt-file") + 1])
        self.assertFalse(prompt.exists(), "scratch prompt is removed after the run")

    @patch("scheduler.vibe_research.ensure_agent_profile")
    @patch("scheduler.vibe_research.vibe_executable", return_value="/bin/vibe")
    @patch("scheduler.vibe_research.subprocess.Popen")
    def test_no_web_calls_is_a_failure(self, popen, _exe, _profile):
        process = Mock(returncode=0, pid=1)
        process.communicate.return_value = (json.dumps([_answer({"games": []})]), "")
        popen.return_value = process
        with self.assertRaisesRegex(ScheduleResearchError, "no successful web calls"):
            vibe.run_vibe_research_for_day(self.day, self.rinks, [])

    def test_prompt_contains_no_crm_fields(self):
        prompt = vibe.build_prompt(self.day, self.rinks, [{"time": "9:00 AM", "rink": "Foxboro Sports Center", "away": "A", "home": "B", "crm_title": "PRIVATE"}])
        self.assertNotIn("PRIVATE", prompt)


class VibeResumeTests(TestCase):
    @patch("scheduler.vibe_research.ensure_agent_profile")
    @patch("scheduler.vibe_research.vibe_executable", return_value="/bin/vibe")
    @patch("scheduler.vibe_research.subprocess.Popen")
    def test_turn_limit_without_answer_resumes_for_final_json(self, popen, _exe, _profile):
        game = {"date": "2026-11-21", "time": "3:00 PM", "away": "A", "home": "B", "rink": "Foxboro Sports Center",
                "source_url": "https://example.org/s", "source_excerpt": "Nov 21 3:00 PM A vs B Foxboro"}
        calls = []

        def launch(cmd, **kwargs):
            calls.append(cmd)
            proc = Mock(pid=1)
            if "--resume" in cmd:
                proc.returncode = 0
                proc.communicate.return_value = (json.dumps([_answer({"games": [game], "checked_rinks": [], "limitations": []})]), "")
            else:
                export = Path(cmd[cmd.index("--output-dir") + 1])
                export.mkdir(parents=True)
                (export / "export.json").write_text(json.dumps({"session_id": "f6b89604-b9e4-c9a4-9bb3-42c6edfe372e"}))
                proc.returncode = 3
                proc.communicate.return_value = (json.dumps([_effect("connector_web_search.open_url", "https://example.org/s")]), "")
            return proc

        popen.side_effect = launch
        result = vibe.run_vibe_research_for_day("2026-11-21", ["Foxboro Sports Center"], [])
        self.assertEqual(len(result["games"]), 1)
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[1][calls[1].index("--agent") + 1], "rink-research")
