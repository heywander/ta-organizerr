from __future__ import annotations

import unittest

from ta_organizerr.transcode import _build_cmd, output_container


class TranscodeCommandTests(unittest.TestCase):
    def test_container_follows_extension(self) -> None:
        self.assertEqual(output_container("/a/b.mkv"), "matroska")
        self.assertEqual(output_container("/a/b.MP4"), "mp4")
        self.assertEqual(output_container("/a/b.webm"), "mp4")

    def test_mkv_has_no_faststart(self) -> None:
        cmd = _build_cmd("/a/b.mkv", "/a/b.mkv.temp.mkv", "vp9", "libx264")
        self.assertIn("matroska", cmd)
        self.assertNotIn("+faststart", cmd)
        self.assertIn("libx264", cmd)

    def test_h264_source_only_reencodes_audio(self) -> None:
        cmd = _build_cmd("/a/b.mp4", "/a/b.mp4.temp.mp4", "h264", "h264_nvenc")
        self.assertIn("copy", cmd)
        self.assertNotIn("h264_nvenc", cmd)
        self.assertIn("+faststart", cmd)


if __name__ == "__main__":
    unittest.main()
