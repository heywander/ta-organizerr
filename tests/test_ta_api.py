from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from ta_organizerr.ta_api import MetadataFetchError, fetch_all_metadata


class MetadataFetchTests(unittest.TestCase):
    def test_request_failure_raises(self) -> None:
        with patch("ta_organizerr.ta_api.requests.get", side_effect=requests.Timeout("boom")):
            with self.assertRaises(MetadataFetchError):
                fetch_all_metadata()

    def test_missing_data_key_raises(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"unexpected": []}

        with patch("ta_organizerr.ta_api.requests.get", return_value=response):
            with self.assertRaises(MetadataFetchError):
                fetch_all_metadata()

    def test_non_list_data_raises(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"not": "a list"}}

        with patch("ta_organizerr.ta_api.requests.get", return_value=response):
            with self.assertRaises(MetadataFetchError):
                fetch_all_metadata()

    def test_empty_page_before_reported_last_page_raises(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [],
            "paginate": {"current_page": 1, "last_page": 5},
        }

        with patch("ta_organizerr.ta_api.requests.get", return_value=response):
            with self.assertRaisesRegex(MetadataFetchError, "empty page"):
                fetch_all_metadata()

    def test_repeated_page_raises_instead_of_looping_forever(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [{"youtube_id": "AAAAAAAAAAA", "title": "Example"}],
        }

        with patch("ta_organizerr.ta_api.requests.get", return_value=response) as get:
            with self.assertRaises(MetadataFetchError):
                fetch_all_metadata()
        self.assertEqual(get.call_count, 2)

    def test_metadata_types_are_normalized(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [{
                "youtube_id": "AAAAAAAAAAA",
                "title": None,
                "channel": "malformed",
                "published": 20260715,
            }],
            "paginate": {"current_page": "1", "last_page": "1"},
        }

        with patch("ta_organizerr.ta_api.requests.get", return_value=response):
            result = fetch_all_metadata()

        self.assertEqual(result["AAAAAAAAAAA"]["title"], "unknown_title")
        self.assertEqual(result["AAAAAAAAAAA"]["channel_name"], "Unknown Channel")
        self.assertEqual(result["AAAAAAAAAAA"]["published"], "20260715")
        self.assertIsNone(result["AAAAAAAAAAA"]["channel_id"])

    def test_channel_id_is_preserved_when_canonical(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [{
                "youtube_id": "AAAAAAAAAAA",
                "title": "Example",
                "channel": {
                    "channel_name": "Example channel",
                    "channel_id": "UCtbyZ5js_2m1LLi4nrrnFfw",
                },
            }],
            "paginate": {"current_page": 1, "last_page": 1},
        }
        with patch("ta_organizerr.ta_api.requests.get", return_value=response):
            result = fetch_all_metadata()
        self.assertEqual(
            result["AAAAAAAAAAA"]["channel_id"],
            "UCtbyZ5js_2m1LLi4nrrnFfw",
        )


if __name__ == "__main__":
    unittest.main()
