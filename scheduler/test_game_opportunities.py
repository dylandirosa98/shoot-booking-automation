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
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"results": [
            {"season": 15008, "start": "2026-10-24", "end": "2026-10-24", "status_code": 200,
             "payload": {"data": [], "meta": {"filtered": 0}}}
        ]}))
        with patch.dict(os.environ, {"PIPEDRIVE_API_TOKEN": "private", "PYTHONPATH": "incompatible-runtime"}):
            out = research.fetch_gamesheet_batch([(15008, "2026-10-24", "2026-10-24")], public_python="/usr/bin/python3")
        self.assertEqual(out[15008][1]["data"], [])
        env = run.call_args.kwargs["env"]
        self.assertTrue("PIPEDRIVE_API_TOKEN" not in env, "Public worker must not receive CRM credentials")
        self.assertTrue("PYTHONPATH" not in env, "Public worker must use its own dependencies")
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent, {"tasks": [{"season": 15008, "start": "2026-10-24", "end": "2026-10-24"}]})

    def test_ayhl_rest_api_games_are_parsed(self):
        payload = {"possibly_truncated": False, "games": [
            {"id": 46285, "date": "2026-10-24", "time": {"label": "9:30 AM", "start": "09:30"},
             "league": {"name": "12U Minor 15"}, "away": {"name": "Away Club"}, "home": {"name": "Home Club"},
             "rink": {"name": "Foxboro Sports Center"}, "status": "scheduled"},
            {"id": 1, "date": "2026-10-25", "time": {"label": "9:30 AM", "start": "09:30"},
             "league": {"name": "x"}, "away": {"name": "A"}, "home": {"name": "B"},
             "rink": {"name": "Foxboro Sports Center"}, "status": "scheduled"},
        ]}
        games = research.parse_ayhl_schedule(payload, self.shoots)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["sort_time"], "09:30")
        self.assertEqual(games[0]["division"], "12U Minor 15")
        self.assertEqual(games[0]["source_url"], "https://atlantichockey.org/games/46285/preview/")

    def test_ayhl_truncated_or_invalid_payload_is_not_an_empty_schedule(self):
        with self.assertRaises(ValueError):
            research.parse_ayhl_schedule({"games": [], "possibly_truncated": True}, self.shoots)
        with self.assertRaises(ValueError):
            research.parse_ayhl_schedule({"html": "unavailable"}, self.shoots)

    @patch.object(research, "http_get_json", side_effect=requests.HTTPError("404"))
    def test_ayhl_http_failure_raises(self, _get):
        with self.assertRaisesRegex(research.ScheduleResearchError, "AYHL"):
            research.fetch_ayhl_games(self.shoots)

    def _gamesheet(self, results):
        with patch.object(research, "GAMESHEET_SEASONS", [(15008, "FedHockey (EHF)", {"NE"}), (15279, "GameSheet (PHL)", {"NE"})]):
            with patch.object(research, "fetch_gamesheet_batch", return_value=results):
                warnings = []
                games = research.fetch_gamesheet_and_fedhockey_games(self.shoots, public_python="/py", warnings=warnings)
                return games, warnings

    def test_gamesheet_403_aborts_instead_of_publishing_zero_games(self):
        with self.assertRaisesRegex(research.ScheduleResearchError, "GameSheet.*403"):
            self._gamesheet({15008: (403, None), 15279: (403, None)})

    def test_gamesheet_truncated_response_is_not_complete(self):
        bad = {"data": [], "meta": {"filtered": 140, "total": 140}}
        with self.assertRaises(research.ScheduleResearchError):
            self._gamesheet({15008: (200, bad), 15279: (200, bad)})

    def test_gamesheet_valid_zero_games_is_allowed(self):
        ok = {"data": [], "meta": {"filtered": 0, "total": 0}}
        self.assertEqual(self._gamesheet({15008: (200, ok), 15279: (200, ok)}), ([], []))

    def test_gamesheet_single_retired_season_is_a_warning_not_a_failure(self):
        game = {"date": "Oct 24, 2026", "time": "7:00 AM", "location": "Foxboro Sports Center Rink 1",
                "visitor": {"title": "A"}, "home": {"title": "B", "division": {"title": "U14"}}}
        other_day = dict(game, date="Oct 30, 2026")
        games, warnings = self._gamesheet({
            15008: (200, {"data": [game, other_day], "meta": {"filtered": 2}}),
            15279: (200, {"data": [], "meta": {"filtered": 0}}),
        })
        self.assertEqual([g["date"] for g in games], ["2026-10-24"])
        self.assertEqual(games[0]["sort_time"], "07:00")
        with patch.object(research, "GAMESHEET_SEASONS", [(1, "a", {"NE"}), (2, "b", {"NE"}), (3, "c", {"NE"})]):
            with patch.object(research, "fetch_gamesheet_batch", return_value={1: (200, {"data": []}), 2: (200, {"data": []}), 3: (404, None)}):
                warnings = []
                research.fetch_gamesheet_and_fedhockey_games(self.shoots, public_python="/py", warnings=warnings)
        self.assertIn("1/3 seasons failed", warnings[0])

    def test_dynamic_rink_from_shoot_address_matches_league_venue(self):
        research._DYNAMIC_RINKS.clear()
        try:
            name = research.register_shoot_facility("Clark Gillies Arena (Dix Hills Ice Rink), Vanderbilt Parkway, Dix Hills, NY, USA")
            self.assertEqual(research.canonical_rink("Dix Hills Ice Rink", "ayhl"), name)
            self.assertEqual(research.rink_region(name), "NY")
            research.register_shoot_facility("Revolution Ice Centre, Old Boston Road, Pittston, PA, USA")
            self.assertEqual(research.canonical_rink("Revolution Ice Centre"), "Revolution Ice Centre")
            self.assertEqual(research.canonical_rink("Revolution Ice Gardens"), "Revolution Ice Gardens")
            self.assertIsNone(research.register_shoot_facility("Florida, USA"))
        finally:
            research._DYNAMIC_RINKS.clear()

    def test_ages_glued_to_words_and_phone_numbers(self):
        self.assertEqual(research.extract_ages("Home Rink2016 Boston junior terriers"), {"10u"})
        self.assertEqual(research.extract_ages("call 555-201-2014"), set())
        self.assertEqual(research.extract_ages("14UAA"), {"14u"})

    def test_bare_region_is_not_a_club(self):
        self.assertIsNone(research.resolve_club_rule("Jersey Shore"))
        self.assertEqual(research.resolve_club_rule("Jersey Shore Wildcats 14L")[0], "Jersey Shore Wildcats")

    def test_new_matches_are_flagged_against_previous_snapshot(self):
        m = {"crm_kind": "deal", "crm_id": "1", "club": "Top Gun"}
        prev = {"generated_at": "last week", "days": [{"date": "2026-10-24", "direct_matches": [dict(m)], "maybe_matches": []}]}
        report = {"days": [{"date": "2026-10-24", "direct_matches": [dict(m)], "maybe_matches": [dict(m, crm_id="2")]}]}
        self.assertEqual(research.mark_new_matches(report, prev), 1)
        self.assertFalse(report["days"][0]["direct_matches"][0]["is_new"])
        self.assertTrue(report["days"][0]["maybe_matches"][0]["is_new"])

    def _build_with_sources(self, **overrides):
        activities = [{"id": 9, "type": "shoot_booking", "done": False, "due_date": "2099-10-24", "due_time": "",
                       "location": "Foxboro Sports Center, Foxboro, MA, USA", "deal_id": 5, "subject": "Shoot"}]

        def pd(_t, _d, resource, **_f):
            return activities if resource == "activities" else []

        game = {"date": "2099-10-24", "time": "9:00 AM", "sort_time": "09:00", "away": "Top Gun 14U",
                "home": "Other", "division": "14U", "rink": "Foxboro Sports Center", "site": "AYHL", "source_url": "https://x"}
        sources = {
            "fetch_ayhl_games": Mock(return_value=[game]),
            "fetch_ahf_thf_games": Mock(return_value=[]),
            "fetch_gamesheet_and_fedhockey_games": Mock(return_value=[]),
            "fetch_hollydell_facility_games": Mock(return_value=[]),
        }
        sources.update(overrides)
        with patch.object(research, "get_pipedrive_credentials", return_value=("t", "d")), \
             patch.object(research, "pipedrive_get_all", side_effect=pd), \
             patch.object(research, "find_public_python", return_value=None), \
             patch.multiple(research, **sources):
            return research.build_game_opportunities_report()

    def test_one_failed_source_does_not_discard_the_others(self):
        report = self._build_with_sources(fetch_ahf_thf_games=Mock(side_effect=research.ScheduleResearchError("AHF down")))
        self.assertFalse(report["complete"])
        status = {s["source"]: s["status"] for s in report["source_status"]}
        self.assertEqual(status["AHF / THF"], "failed")
        self.assertEqual(report["summary"]["total_games"], 1)
        self.assertTrue(any("AHF down" in w for w in report["days"][0]["coverage_warnings"]))

    def test_all_sources_failing_aborts(self):
        boom = Mock(side_effect=research.ScheduleResearchError("down"))
        with self.assertRaisesRegex(research.ScheduleResearchError, "Every schedule source failed"):
            self._build_with_sources(fetch_ayhl_games=boom, fetch_ahf_thf_games=boom,
                                     fetch_gamesheet_and_fedhockey_games=boom, fetch_hollydell_facility_games=boom)

    def test_ai_day_failure_is_a_coverage_note(self):
        with patch("scheduler.vibe_research.run_vibe_research_for_day", side_effect=research.ScheduleResearchError("Vibe timed out")):
            with patch.object(research, "get_pipedrive_credentials", return_value=("t", "d")):
                report = self._build_with_sources_vibe()
        self.assertEqual(report["coverage"]["ai_days_failed"], 1)
        self.assertTrue(any("Vibe timed out" in w for w in report["days"][0]["coverage_warnings"]))
        self.assertEqual(report["summary"]["total_games"], 1)

    def _build_with_sources_vibe(self):
        orig = research.build_game_opportunities_report
        with patch.object(research, "build_game_opportunities_report", lambda **kw: orig(use_vibe=True, **kw)):
            return self._build_with_sources()

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
                        Command(stdout=StringIO(), stderr=StringIO()).handle(date=None, use_vibe=True, push=True)
                    save.assert_not_called()
                    push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
