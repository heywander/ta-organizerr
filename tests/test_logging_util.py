from __future__ import annotations

import unittest
from unittest.mock import patch

from ta_organizerr import logging_util


class LogCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        with logging_util.log_lock:
            logging_util.log_buffer.clear()
            logging_util._log_start_index = 0
        with logging_util.transcode_log_lock:
            logging_util.transcode_log_buffer.clear()
            logging_util._transcode_log_start_index = 0

    def test_main_cursor_advances_after_buffer_rollover(self) -> None:
        with patch("builtins.print"):
            for index in range(logging_util._MAIN_BUFFER_LIMIT + 2):
                logging_util.log(f"message-{index}")

        messages, cursor = logging_util.read_logs(0)
        self.assertEqual(len(messages), logging_util._MAIN_BUFFER_LIMIT)
        self.assertEqual(messages[0], "message-2")
        self.assertEqual(cursor, logging_util._MAIN_BUFFER_LIMIT + 2)

        with patch("builtins.print"):
            logging_util.log("new-message")
        messages, next_cursor = logging_util.read_logs(cursor)
        self.assertEqual(messages, ["new-message"])
        self.assertEqual(next_cursor, cursor + 1)

    def test_transcode_cursor_advances_after_buffer_rollover(self) -> None:
        with patch("builtins.print"):
            for index in range(logging_util._TRANSCODE_BUFFER_LIMIT + 1):
                logging_util.tlog(f"transcode-{index}")

        messages, cursor = logging_util.read_transcode_logs(0)
        self.assertEqual(len(messages), logging_util._TRANSCODE_BUFFER_LIMIT)
        self.assertEqual(messages[0], "transcode-1")

        with patch("builtins.print"):
            logging_util.tlog("new-transcode")
        messages, next_cursor = logging_util.read_transcode_logs(cursor)
        self.assertEqual(messages, ["new-transcode"])
        self.assertEqual(next_cursor, cursor + 1)


if __name__ == "__main__":
    unittest.main()
