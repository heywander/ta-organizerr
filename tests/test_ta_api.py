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


if __name__ == "__main__":
    unittest.main()
