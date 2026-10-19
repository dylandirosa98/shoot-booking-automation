import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from .game_opportunities import ScheduleResearchError
from .luna_research import codex_command, run_luna_research_for_day, validate_result


class LunaResearchTests(TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {"OPPORTUNITIES_BROWSER_EXECUTABLE": "/fake/chromium"}))
        self.rinks = ["Foxboro Sports Center"]
        self.result = {"games": [], "checked_rinks": self.rinks, "limitations": []}

    def test_uses_luna_codex_with_no_shell_and_browser_allowlist(self):
        command = codex_command(Path("/scratch"))
        self.assertEqual(command[0:2], ["codex", "exec"])
        self.assertIn("gpt-6-luna", command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("read-only", command)
        self.assertFalse(any("browser_run_code" in value for value in command))

    def test_browser_skipped_cannot_be_reported_as_completed(self):
        with self.assertRaisesRegex(ScheduleResearchError, "navigate"):
            validate_result(self.result, "2026-10-24", self.rinks, False)

    def test_every_facility_must_be_accounted_for(self):
        self.result["checked_rinks"] = []
        with self.assertRaisesRegex(ScheduleResearchError, "every requested facility"):
            validate_result(self.result, "2026-10-24", self.rinks, True)

    def test_wrong_date_is_rejected(self):
        self.result["games"] = [{
            "date": "2026-10-25", "time": "9:00 AM", "away": "Away", "home": "Home",
            "division": "14U", "rink": self.rinks[0], "published_venue": self.rinks[0],
            "site": "EHF", "source_url": "https://fedhockey.com/", "source_excerpt": "Published game evidence",
        }]
        with self.assertRaisesRegex(ScheduleResearchError, "wrong date"):
            validate_result(self.result, "2026-10-24", self.rinks, True)

    @patch("scheduler.luna_research.subprocess.Popen")
    def test_failed_codex_process_aborts(self, popen):
        process = Mock(returncode=1)
        process.communicate.return_value = ("", "")
        popen.return_value = process
        with self.assertRaisesRegex(ScheduleResearchError, "Luna/Codex failed"):
            run_luna_research_for_day("2026-10-24", self.rinks, [])

    @patch("scheduler.luna_research.subprocess.Popen")
    def test_success_requires_actual_browser_event_and_preserves_limitations(self, popen):
        def launch(*args, **kwargs):
            result = dict(self.result, limitations=["One league blocks browser access."])
            (Path(kwargs["cwd"]) / "result.json").write_text(json.dumps(result))
            process = Mock(returncode=0)
            event = {"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "rinkbrowser", "tool": "browser_navigate",
                "status": "completed", "error": None, "result": {"content": [{"text": "Page URL: https://fedhockey.com/"}]},
            }}
            snapshot = {"type": "item.completed", "item": {
                "type": "mcp_tool_call", "server": "rinkbrowser", "tool": "browser_snapshot",
                "status": "completed", "error": None, "result": {"content": [{"text": "Public schedule snapshot"}]},
            }}
            process.communicate.return_value = (json.dumps(event) + "\n" + json.dumps(snapshot), "")
            return process

        popen.side_effect = launch
        result = run_luna_research_for_day("2026-10-24", self.rinks, [])
        self.assertEqual(result["limitations"], ["One league blocks browser access."])
        env = popen.call_args.kwargs["env"]
        self.assertTrue(all(not key.startswith("PIPEDRIVE_") for key in env))
