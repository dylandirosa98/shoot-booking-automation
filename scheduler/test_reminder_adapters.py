"""Synthetic network-boundary tests; never contact a real bridge or CRM."""
import unittest
from unittest.mock import Mock

import requests

from scheduler import reminder_adapters as adapters


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = {"success": True, "data": data}
    return result


class PipedriveTests(unittest.TestCase):
    def test_reads_activity_with_explicit_configuration(self):
        http = Mock()
        http.request.return_value = response({"id": 17, "subject": "Shoot"})
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        self.assertEqual(source.get_activity(17), {"id": 17, "subject": "Shoot"})
        http.request.assert_called_once_with(
            "GET", "https://crm.example/api/v1/activities/17",
            params={"api_token": "secret"}, timeout=15, allow_redirects=False)

    def test_not_found_and_errors_fail_closed_without_secrets(self):
        http = Mock()
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        http.request.return_value = response(None, 404)
        self.assertIsNone(source.get_activity("17"))
        for failure in (response(None, 401), response(None, 500), response(None, 302),
                        response([]), response(None), requests.Timeout("secret")):
            with self.subTest(failure=type(failure).__name__):
                http.request.side_effect = failure if isinstance(failure, Exception) else None
                http.request.return_value = failure
                with self.assertRaises(adapters.SourceError) as caught:
                    source.get_activity(17)
                self.assertNotIn("secret", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_rejects_non_numeric_ids_before_network(self):
        http = Mock()
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        for value in (True, None, "1/../../users", "1?api_token=x", 1.2, "", "０１", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source.get_activity(value)
        http.request.assert_not_called()

    def test_list_pages_all_incomplete_activities(self):
        http = Mock()
        first = response([{"id": 1, "type": "shoot_booking"}])
        first.json.return_value["additional_data"] = {"pagination": {
            "more_items_in_collection": True, "next_start": 100}}
        last = response([{"id": 2}])
        last.json.return_value["additional_data"] = {"pagination": {
            "more_items_in_collection": False}}
        http.request.side_effect = [first, last]
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        self.assertEqual(source.list_activities("2026-10-23", "2026-10-30"),
                         [{"id": 1, "type": "shoot_booking"}, {"id": 2}])
        self.assertEqual([call.kwargs["params"] for call in http.request.call_args_list], [
            {"api_token": "secret", "start_date": "2026-10-23", "end_date": "2026-10-30",
             "done": 0, "user_id": 0, "type": "shoot_booking", "start": start, "limit": 100} for start in (0, 100)])
        self.assertTrue(all(call.args[0:2] == ("GET", "https://crm.example/api/v1/activities")
                            for call in http.request.call_args_list))

    def test_list_rejects_partial_or_bad_pagination(self):
        http = Mock()
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        for failure in (response([], 404), response([], 401), response([]), response({}),
                        requests.Timeout("secret")):
            http.request.side_effect = failure if isinstance(failure, Exception) else None
            http.request.return_value = failure
            with self.subTest(case=type(failure).__name__), self.assertRaises(adapters.SourceError):
                source.list_activities("2026-10-23", "2026-10-30")
        for next_start in (None, 0, -1, "100", True):
            bad = response([])
            bad.json.return_value["additional_data"] = {"pagination": {
                "more_items_in_collection": True, "next_start": next_start}}
            http.request.side_effect = None
            http.request.return_value = bad
            with self.subTest(next_start=next_start), self.assertRaises(adapters.SourceError):
                source.list_activities("2026-10-23", "2026-10-30")

    def test_configuration_and_list_dates_are_validated_before_network(self):
        http = Mock()
        for base, token in (("", "secret"), ("ftp://crm.example", "secret"),
                            ("https://user:secret@crm.example", "secret"),
                            ("https://crm.example?api_token=secret", "secret"),
                            ("https://crm.example", "")):
            with self.subTest(base=base), self.assertRaises(adapters.ConfigurationError):
                adapters.PipedriveReminderSource(base, token, http=http)
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        for start, end in (("not-a-date", "2026-10-30"), ("2026-10-31", "2026-10-30"),
                           (None, "2026-10-30"), ("2026-02-30", "2026-10-30")):
            with self.subTest(start=start), self.assertRaises(ValueError):
                source.list_activities(start, end)
        http.request.assert_not_called()

    def test_malformed_crm_responses_and_later_page_failure_never_return_partial_data(self):
        http = Mock()
        source = adapters.PipedriveReminderSource("https://crm.example/api", "secret", http=http)
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("secret")
        bad_envelope = response({"id": 17})
        bad_envelope.json.return_value["success"] = False
        not_object = response()
        not_object.json.return_value = []
        for failure in (invalid_json, bad_envelope, not_object, requests.ConnectionError("secret")):
            http.request.side_effect = failure if isinstance(failure, Exception) else [failure]
            with self.subTest(case=type(failure).__name__), self.assertRaises(adapters.SourceError) as caught:
                source.get_activity(17)
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
        first = response([{"id": 1}])
        first.json.return_value["additional_data"] = {"pagination": {
            "more_items_in_collection": True, "next_start": 100}}
        http.request.side_effect = [first, response(None, 500)]
        with self.assertRaises(adapters.SourceError):
            source.list_activities("2026-10-23", "2026-10-30")


class BlueBubblesTests(unittest.TestCase):
    phone = "+15555550101"
    sender_phone = "+15555550199"

    def sender(self, http, **overrides):
        config = dict(base_url="https://jack-bridge.example", password="bridge-secret",
                      expected_sender_number=self.sender_phone,
                      verified_sender_number=self.sender_phone, sender_verified=True, http=http)
        config.update(overrides)
        return adapters.BlueBubblesReminderSender(**config)

    def test_requires_explicit_verified_matching_sender_and_bridge(self):
        http = Mock()
        for override in ({"sender_verified": False}, {"sender_verified": "true"},
                         {"verified_sender_number": "+15555550198"},
                         {"expected_sender_number": ""}, {"verified_sender_number": ""},
                         {"base_url": ""}, {"password": ""},
                         {"base_url": "https://user:bridge-secret@jack.example"}):
            with self.subTest(override=tuple(override)), self.assertRaises(adapters.ConfigurationError):
                self.sender(http, **override)
        http.request.assert_not_called()

    def chat(self, service="iMessage", participants=None):
        return response({"guid": service + ";-;" + self.phone,
                         "participants": participants if participants is not None else [{"address": self.phone}]})

    def persisted(self, **changes):
        data = {"guid": "message/guid", "text": "Reminder", "isFromMe": True,
                "error": 0, "handle": {"address": self.phone}}
        data.update(changes)
        return response(data)

    def test_existing_imessage_chat_posts_once_then_independently_reads_message(self):
        http = Mock()
        http.request.side_effect = [self.chat(), response({"guid": "message/guid"}), self.persisted()]
        self.assertEqual(self.sender(http).send("(555) 555-0101", "Reminder", "reminder-17-24h"), "message/guid")
        calls = http.request.call_args_list
        self.assertEqual([(c.args[0], c.args[1]) for c in calls], [
            ("GET", "https://jack-bridge.example/api/v1/chat/iMessage%3B-%3B%2B15555550101"),
            ("POST", "https://jack-bridge.example/api/v1/message/text"),
            ("GET", "https://jack-bridge.example/api/v1/message/message%2Fguid")])
        self.assertEqual(calls[1].kwargs["json"], {"chatGuid": "iMessage;-;" + self.phone,
            "message": "Reminder", "method": "private-api", "tempGuid": "reminder-17-24h"})
        for call in calls:
            self.assertEqual(call.kwargs["params"], {"password": "bridge-secret"})
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], 15)

    def test_existing_chat_requires_exact_one_to_one_recipient(self):
        for participants in ([], [{"address": "+15555550102"}],
                             [{"address": self.phone}, {"address": "+15555550102"}],
                             [{"address": self.phone}, {"address": self.phone}], [{}]):
            http = Mock()
            http.request.side_effect = [self.chat(participants=participants)]
            with self.subTest(participants=participants), self.assertRaises(adapters.TransportError):
                self.sender(http).send(self.phone, "Reminder", "stable-key")
            self.assertEqual(http.request.call_count, 1)
        for data in ({"guid": "iMessage;+;group", "participants": [{"address": self.phone}]},
                     {"guid": "iMessage;-;" + self.phone}, None, []):
            http = Mock()
            http.request.return_value = response(data)
            with self.subTest(data=data), self.assertRaises(adapters.TransportError):
                self.sender(http).send(self.phone, "Reminder", "stable-key")
            self.assertEqual(http.request.call_count, 1)

    def test_existing_sms_chat_used_after_only_imessage_404(self):
        http = Mock()
        http.request.side_effect = [response(None, 404), self.chat("SMS"),
                                    response({"guid": "message/guid"}), self.persisted()]
        self.assertEqual(self.sender(http).send(self.phone, "Reminder", "stable-key"), "message/guid")
        calls = http.request.call_args_list
        self.assertTrue(calls[1].args[1].endswith("/chat/SMS%3B-%3B%2B15555550101"))
        self.assertEqual(calls[2].kwargs["json"]["chatGuid"], "SMS;-;" + self.phone)

    def test_chat_lookup_failure_does_not_fall_through_to_send(self):
        for failure in (requests.Timeout("bridge-secret"), requests.ConnectionError("bridge-secret"),
                        response({}, 401), response({}, 500), response({}, 302)):
            http = Mock()
            http.request.side_effect = failure if isinstance(failure, Exception) else [failure]
            with self.subTest(failure=type(failure).__name__), self.assertRaises(adapters.TransportError) as caught:
                self.sender(http).send(self.phone, "Reminder", "stable-key")
            self.assertNotIn("bridge-secret", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(http.request.call_count, 1)

    def test_new_chat_routes_by_availability_and_explicit_service(self):
        for availability, expected_service in ((response(True), "iMessage"),
                (response({"available": True}), "iMessage"), (response(False), "SMS"),
                (response({"available": False}), "SMS"), (response(None), "SMS"),
                (response({}, 500), "SMS"), (response({}, 401), "SMS"),
                (requests.Timeout("bridge-secret"), "SMS"),
                (requests.ConnectionError("bridge-secret"), "SMS")):
            http = Mock()
            http.request.side_effect = [response(None, 404), response(None, 404), availability,
                                        response({"guid": "message/guid"}), self.persisted()]
            with self.subTest(service=expected_service, availability=type(availability).__name__):
                self.assertEqual(self.sender(http).send(self.phone, "Reminder", "stable-key"), "message/guid")
                calls = http.request.call_args_list
                self.assertTrue(calls[2].args[1].endswith("/handle/availability/imessage"))
                self.assertEqual(calls[2].kwargs["params"], {"password": "bridge-secret", "address": self.phone})
                self.assertTrue(calls[3].args[1].endswith("/chat/new"))
                self.assertEqual(calls[3].kwargs["json"], {"addresses": [self.phone],
                    "message": "Reminder", "method": "private-api", "service": expected_service,
                    "tempGuid": "stable-key"})
                self.assertEqual(sum(c.args[0] == "POST" for c in calls), 1)

    def test_send_failures_are_uncertain_and_never_retry_post(self):
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("bridge-secret")
        for failure in (requests.Timeout("bridge-secret"), requests.ConnectionError("bridge-secret"),
                        response(None, 500), response(None, 401), response(None, 302),
                        response(None), response({}), response({"guid": ""}),
                        response({"guid": 123}), response({"guid": "  "}), invalid_json):
            for new_chat in (False, True):
                http = Mock()
                prefix = [response(None, 404), response(None, 404), response(False)] if new_chat else [self.chat()]
                http.request.side_effect = prefix + [failure]
                with self.subTest(failure=type(failure).__name__, new_chat=new_chat):
                    with self.assertRaises(adapters.DeliveryUncertain) as caught:
                        self.sender(http).send(self.phone, "Reminder", "stable-key")
                    self.assertNotIn("bridge-secret", str(caught.exception))
                    self.assertIsNone(caught.exception.__context__)
                    self.assertEqual(sum(c.args[0] == "POST" for c in http.request.call_args_list), 1)
                    self.assertEqual(http.request.call_count, len(prefix) + 1)

    def test_independent_readback_must_confirm_message_not_just_http_200(self):
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("bridge-secret")
        failures = [requests.Timeout("bridge-secret"), requests.ConnectionError("bridge-secret"),
                    response(None, 404), response(None, 500), response(None, 401), response(None, 302),
                    response(None), response([]), invalid_json,
                    self.persisted(guid="different"), self.persisted(text="Wrong"),
                    self.persisted(isFromMe=False), self.persisted(isFromMe=1),
                    self.persisted(error=42), self.persisted(error="0"),
                    self.persisted(handle={"address": "+15555550102"}),
                    self.persisted(handle=None), self.persisted(handle={})]
        for readback in failures:
            http = Mock()
            http.request.side_effect = [self.chat(), response({"guid": "message/guid"}), readback]
            with self.subTest(readback=type(readback).__name__):
                with self.assertRaises(adapters.DeliveryUncertain) as caught:
                    self.sender(http).send(self.phone, "Reminder", "stable-key")
                self.assertNotIn("bridge-secret", str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(http.request.call_count, 3)
                self.assertEqual(sum(c.args[0] == "POST" for c in http.request.call_args_list), 1)

    def test_readback_can_confirm_exact_chat_without_handle(self):
        http = Mock()
        http.request.side_effect = [self.chat(), response({"guid": "message/guid"}),
            self.persisted(handle=None, chats=[{"guid": "iMessage;-;" + self.phone}])]
        self.assertEqual(self.sender(http).send(self.phone, "Reminder", "stable-key"), "message/guid")

    def test_invalid_inputs_do_not_touch_network(self):
        http = Mock()
        for phone, text, key in (("bad", "Reminder", "key"),
                                 (self.phone, "", "key"), (self.phone, "  ", "key"),
                                 (self.phone, None, "key"), (self.phone, "Reminder", ""),
                                 (self.phone, "Reminder", "  "), (self.phone, "Reminder", None),
                                 (self.phone, "Reminder", 17)):
            with self.subTest(phone=phone, text=text, key=key), self.assertRaises(ValueError):
                self.sender(http).send(phone, text, key)
        http.request.assert_not_called()

    def test_temp_guid_is_stable_for_same_caller_key_across_instances(self):
        temp_guids = []
        for _ in range(2):
            http = Mock()
            http.request.side_effect = [self.chat(), response({"guid": "message/guid"}), self.persisted()]
            self.sender(http).send(self.phone, "Reminder", "stable-key")
            temp_guids.append(http.request.call_args_list[1].kwargs["json"]["tempGuid"])
        self.assertEqual(temp_guids, ["stable-key", "stable-key"])

    def test_readback_rejects_conflicting_address_chat_and_provider_error_envelopes(self):
        bad_envelope = self.persisted()
        bad_envelope.json.return_value["status"] = 500
        failures = [bad_envelope, self.persisted(handle={"address": self.phone},
                     chats=[{"guid": "iMessage;+;group"}]),
                    self.persisted(handle={"address": "+15555550102"},
                     chats=[{"guid": "iMessage;-;" + self.phone}])]
        for readback in failures:
            http = Mock()
            http.request.side_effect = [self.chat(), response({"guid": "message/guid"}), readback]
            with self.subTest(case=failures.index(readback)), self.assertRaises(adapters.DeliveryUncertain):
                self.sender(http).send(self.phone, "Reminder", "stable-key")

    def test_new_chat_readback_accepts_exact_service_chat_guid(self):
        http = Mock()
        http.request.side_effect = [response(None, 404), response(None, 404), response(False),
            response({"guid": "message/guid"}),
            self.persisted(handle=None, chats=[{"guid": "SMS;-;" + self.phone}])]
        self.assertEqual(self.sender(http).send(self.phone, "Reminder", "stable-key"), "message/guid")

    def test_failed_availability_envelopes_fall_back_to_sms(self):
        for envelope in ({"status": 500, "data": True}, {"success": False, "data": True}):
            availability = response()
            availability.json.return_value = envelope
            http = Mock()
            http.request.side_effect = [response(None, 404), response(None, 404), availability,
                                        response({"guid": "message/guid"}), self.persisted()]
            with self.subTest(envelope=envelope):
                self.sender(http).send(self.phone, "Reminder", "stable-key")
                self.assertEqual(http.request.call_args_list[3].kwargs["json"]["service"], "SMS")

    def test_chat_error_envelopes_and_invalid_json_fail_before_post(self):
        invalid_json = self.chat()
        invalid_json.json.side_effect = ValueError("bridge-secret")
        bad_status = self.chat()
        bad_status.json.return_value["status"] = 500
        bad_success = self.chat()
        bad_success.json.return_value["success"] = False
        for failure in (invalid_json, bad_status, bad_success):
            http = Mock()
            http.request.return_value = failure
            with self.subTest(case=type(failure).__name__), self.assertRaises(adapters.TransportError) as caught:
                self.sender(http).send(self.phone, "Reminder", "stable-key")
            self.assertNotIn("bridge-secret", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(http.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
