from __future__ import annotations

import unittest

from ta_organizerr.utils import channel_component, extract_id_from_filename, sanitize


class SanitizeTests(unittest.TestCase):
    def test_invalid_characters_are_replaced(self) -> None:
        self.assertEqual(sanitize('A/B:C*D?E"F<G>H|I'), "A_B_C_D_E_F_G_H_I")

    def test_control_characters_are_dropped(self) -> None:
        self.assertEqual(sanitize("tab\there"), "tabhere")
        self.assertEqual(sanitize("nul\x00byte"), "nulbyte")

    def test_dot_names_cannot_escape(self) -> None:
        self.assertEqual(sanitize(".."), "untitled")
        self.assertEqual(sanitize(" . "), "untitled")

    def test_fallback_when_empty(self) -> None:
        self.assertEqual(sanitize(""), "untitled")
        self.assertEqual(sanitize("???"), "___")

    def test_long_names_are_capped(self) -> None:
        self.assertEqual(len(sanitize("x" * 500)), 200)

    def test_non_ascii_channels_keep_a_stable_distinct_component(self) -> None:
        # Non-ASCII names are not kept verbatim; they get a stable digest so
        # two lossy names never collapse into the same directory.
        japanese = channel_component("日本語チャンネル")
        russian = channel_component("Канал Фёдора")
        self.assertNotEqual(japanese, russian)
        self.assertEqual(japanese, channel_component("日本語チャンネル"))
        self.assertTrue(japanese.startswith("untitled ["))

    def test_extract_id(self) -> None:
        self.assertEqual(extract_id_from_filename("Some Title [dQw4w9WgXcQ].mp4"), "dQw4w9WgXcQ")
        self.assertEqual(extract_id_from_filename("dQw4w9WgXcQ.mkv"), "dQw4w9WgXcQ")
        self.assertIsNone(extract_id_from_filename("not-an-id.mp4"))


if __name__ == "__main__":
    unittest.main()
