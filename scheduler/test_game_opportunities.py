"""Regression coverage for silent schedule loss during opportunity refreshes."""
import json
import os
import unittest
from io import StringIO
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import requests

from . import game_opportunities as research


class ScheduleRefreshTests(unittest.TestCase):
    def setUp(self):
        self.shoots = {"2026-10-24": [{"rink": "Foxboro Sports Center"}]}

    def test_booked_rink_aliases_match_schedule_names(self):
        aliases = [
            ("Essex Sports Center, Manning Avenue, Middleton, MA, USA", "Essex Sports Complex Rink 1", "Essex Sports Center"),
            ("Icenter Salem, Lowell Road, Salem, NH, USA", "The Icenter - Salem NH", "Icenter Salem"),
            ("Rockland Skating Rink, Summer Street, Rockland, MA, USA", "Rockland Ice Rink", "Rockland Skating Rink"),
            ("Hobomock Arena, Hobomock Street, Pembroke, MA, USA", "Northstar Hobomock Arenas Rink #1", "Hobomock Arena"),
        ]
        for booked, published, expected in aliases:
            with self.subTest(rink=expected):
                self.assertEqual(research.canonical_rink(booked), expected)
                self.assertEqual(research.canonical_rink(published), expected)
                self.assertEqual(research.RINK_REGION[expected], "NE")

    def test_hobomock_is_not_northstar_westborough(self):
        self.assertNotEqual(
            research.canonical_rink("Northstar Hobomock Arenas Rink #1"),
            research.canonical_rink("Northstar Ice Sports, Westborough"),
        )

    def test_roller_hockey_facility_is_explicitly_outside_ice_coverage(self):
        self.assertEqual(research.canonical_rink("Great American Center, Blair Road, Aberdeen Township, NJ, USA"), "Great American Center (roller hockey)")
        self.assertEqual(research.RINK_REGION["Great American Center (roller hockey)"], "ROLLER")
        warnings = research.rink_coverage_warnings(["Great American Center (roller hockey)"])
        self.assertIn("separate roller-hockey research", warnings[0])

    @patch.object(research.subprocess, "run")
    def test_public_schedule_runtime_receives_no_crm_credentials(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({
            "status_code": 200, "payload": {"data": [], "meta": {"filtered": 0}}
        }))
        with patch.dict(os.environ, {"PIPEDRIVE_API_TOKEN": "private", "PYTHONPATH": "incompatible-runtime"}):
            payload = research.fetch_gamesheet_payload("2026-10-24", 15008, public_python="/usr/bin/python3")
        self.assertEqual(payload["data"], [])
        env = run.call_args.kwargs["env"]
        self.assertTrue("PIPEDRIVE_API_TOKEN" not in env, "Public worker must not receive CRM credentials")
        self.assertTrue("PYTHONPATH" not in env, "Public worker must use its own dependencies")

    @patch.object(research.requests, "get")
    def test_ayhl_ignores_commented_out_date_cell(self, get):
        # Real AYHL markup includes a deprecated date-only cell in a comment.
        get.return_value.text = '''
            <table id="schedule_table" class="w3-hide-small"><tr>
            <td>46633 L</td><td>18U</td><td>Away</td><td>0</td>
            <td>Home</td><td>0</td><td>Foxboro Sports Center</td>
            <!--<td>10/24/26</td>-->
            <td>Sat, 10/24/26 9:30 AM</td><td></td></tr></table>
        '''
        games = research.fetch_ayhl_games(self.shoots)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["sort_time"], "09:30")

    @patch.object(research.requests, "get")
    def test_ayhl_invalid_markup_is_not_an_empty_schedule(self, get):
        get.return_value.text = "<html>Schedule temporarily unavailable</html>"
        with self.assertRaises(RuntimeError):
            research.fetch_ayhl_games(self.shoots)

    @patch.object(research.requests, "get")
    def test_gamesheet_403_aborts_instead_of_publishing_zero_games(self, get):
        response = requests.Response()
        response.status_code = 403
        get.return_value = response
        with patch.object(research, "GAMESHEET_SEASONS", [(15008, "FedHockey (EHF)", {"NE"})]):
            with self.assertRaisesRegex(RuntimeError, "GameSheet.*403"):
                research.fetch_gamesheet_and_fedhockey_games(self.shoots)

    @patch.object(research.requests, "get")
    def test_gamesheet_truncated_response_is_not_complete(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"data": [], "meta": {"filtered": 140, "total": 140}}
        with patch.object(research, "GAMESHEET_SEASONS", [(15008, "FedHockey (EHF)", {"NE"})]):
            with self.assertRaises(RuntimeError):
                research.fetch_gamesheet_and_fedhockey_games(self.shoots)

    @patch.object(research.requests, "get")
    def test_gamesheet_valid_zero_games_is_allowed(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"data": [], "meta": {"filtered": 0, "total": 0}}
        with patch.object(research, "GAMESHEET_SEASONS", [(15008, "FedHockey (EHF)", {"NE"})]):
            self.assertEqual(research.fetch_gamesheet_and_fedhockey_games(self.shoots), [])

    @patch.object(research.subprocess, "Popen")
    def test_muse_missing_agent_is_not_a_successful_empty_search(self, popen):
        popen.return_value.returncode = 1
        popen.return_value.communicate.return_value = (
            json.dumps({"type": "error", "error": {"message": 'Agent not found: "rink-research"'}}), ""
        )
        with self.assertRaisesRegex(RuntimeError, "Muse"):
            research.run_muse_spark_agent_for_day("2026-10-24", ["Foxboro Sports Center"])

    @patch.object(research.subprocess, "Popen")
    def test_muse_does_not_inherit_parent_session_or_crm_credentials(self, popen):
        popen.return_value.returncode = 0
        popen.return_value.communicate.return_value = (
            json.dumps({"type": "text", "part": {"text": '{"games": []}'}}), ""
        )
        with patch.dict(os.environ, {"OPENCODE_SESSION_ID": "parent", "PIPEDRIVE_API_TOKEN": "private"}):
            self.assertEqual(research.run_muse_spark_agent_for_day("2026-10-24", ["Foxboro Sports Center"]), [])
        env = popen.call_args.kwargs.get("env", os.environ)
        self.assertTrue("OPENCODE_SESSION_ID" not in env, "Parent session must not be inherited")
        self.assertTrue("PIPEDRIVE_API_TOKEN" not in env, "CRM token must not be inherited")
        self.assertEqual(env["PWD"], popen.call_args.kwargs["cwd"])

    @patch.object(research.subprocess, "Popen")
    def test_muse_expired_provider_auth_is_a_clear_blocker(self, popen):
        popen.return_value.returncode = 1
        popen.return_value.communicate.return_value = (
            json.dumps({"type": "error", "error": {"type": "provider.auth", "status": 401}}), ""
        )
        with self.assertRaisesRegex(RuntimeError, "Muse authentication failed"):
            research.run_muse_spark_agent_for_day("2026-10-24", ["Foxboro Sports Center"])

    @patch.object(research.subprocess, "Popen")
    def test_muse_browser_is_isolated_and_does_not_allow_shell_or_code_execution(self, popen):
        configs = []

        def launch(*args, **kwargs):
            configs.append(json.loads((Path(kwargs["cwd"]) / "opencode.json").read_text()))
            process = Mock(returncode=0)
            process.communicate.return_value = (
                json.dumps({"type": "text", "part": {"text": '{"games": []}'}}), ""
            )
            return process

        popen.side_effect = launch
        research.run_muse_spark_agent_for_day("2026-10-24", ["Foxboro Sports Center"])
        server = configs[0]["mcp"]["servers"]["rinkbrowser"]
        self.assertIn("--isolated", server["command"])
        self.assertIn("--headless", server["command"])
        rules = configs[0]["agents"]["rink-research"]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        allowed = {rule["action"] for rule in rules if rule["effect"] == "allow"}
        self.assertIn("rinkbrowser_browser_navigate", allowed)
        self.assertNotIn("shell", allowed)
        self.assertNotIn("rinkbrowser_browser_run_code", allowed)

    @patch.object(research.subprocess, "Popen")
    def test_muse_invalid_json_aborts(self, popen):
        popen.return_value.returncode = 0
        popen.return_value.communicate.return_value = (
            json.dumps({"type": "text", "part": {"text": "Could not research the schedule"}}), ""
        )
        with self.assertRaises(RuntimeError):
            research.run_muse_spark_agent_for_day("2026-10-24", ["Foxboro Sports Center"])

    @patch.object(research.requests, "get")
    def test_ahf_missing_api_config_aborts(self, get):
        get.return_value.text = "<html>Temporarily unavailable</html>"
        with self.assertRaises(RuntimeError):
            research.fetch_ahf_thf_games(self.shoots)

    @patch.object(research.requests, "get")
    def test_hollydell_503_aborts(self, get):
        response = requests.Response()
        response.status_code = 503
        get.return_value = response
        with self.assertRaises(RuntimeError):
            research.fetch_hollydell_facility_games({"2026-10-24": [{"rink": "Hollydell Ice Arena"}]})

    def test_failed_refresh_never_saves_or_pushes(self):
        from django.core.management.base import CommandError
        from .management.commands.research_game_opportunities import Command

        command_module = "scheduler.management.commands.research_game_opportunities"
        with patch(command_module + ".build_game_opportunities_report", side_effect=research.ScheduleResearchError("GameSheet HTTP 403")):
            with patch(command_module + ".save_report_locally") as save:
                with patch(command_module + ".push_report_to_railway") as push:
                    with self.assertRaisesRegex(CommandError, "snapshots were not changed"):
                        Command(stdout=StringIO()).handle(date=None, use_muse=True, push=True)
                    save.assert_not_called()
                    push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
