from __future__ import annotations

import unittest

from ta_organizerr.utils import extract_id_from_filename, sanitize


class SanitizeTests(unittest.TestCase):
    def test_unicode_is_preserved(self) -> None:
        self.assertEqual(sanitize("日本語チャンネル"), "日本語チャンネル")
        self.assertEqual(sanitize("Канал Фёдора"), "Канал Фёдора")

    def test_invalid_characters_are_replaced(self) -> None:
        self.assertEqual(sanitize('A/B\\C:D*E?F"G<H>I|J'), "A_B_C_D_E_F_G_H_I_J")
        self.assertEqual(sanitize("tab\there"), "tab_here")

    def test_dot_names_cannot_escape(self) -> None:
        self.assertEqual(sanitize("..", fallback="x"), "x")
        self.assertEqual(sanitize(" . ", fallback="x"), "x")
        self.assertEqual(sanitize("...hidden..."), "hidden")

    def test_fallback_when_empty(self) -> None:
        self.assertEqual(sanitize("", fallback="abc"), "abc")
        self.assertEqual(sanitize("???", fallback="abc"), "___")

    def test_long_names_are_capped(self) -> None:
        self.assertEqual(len(sanitize("x" * 500)), 200)

    def test_extract_id(self) -> None:
        self.assertEqual(extract_id_from_filename("Some Title [dQw4w9WgXcQ].mp4"), "dQw4w9WgXcQ")
        self.assertEqual(extract_id_from_filename("dQw4w9WgXcQ.mkv"), "dQw4w9WgXcQ")
        self.assertIsNone(extract_id_from_filename("not-an-id.mp4"))


if __name__ == "__main__":
    unittest.main()
