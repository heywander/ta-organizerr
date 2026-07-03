from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ta_organizerr.config import SOURCE_DIR
from ta_organizerr.utils import path_in_allowed_tree, path_within, translate_host_path


class PathValidationTests(unittest.TestCase):
    def test_entry_validation_does_not_follow_final_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            outside_file = outside / "video.mp4"
            outside_file.write_text("media", encoding="utf-8")
            link = allowed / "video.mp4"
            link.symlink_to(outside_file)

            self.assertTrue(path_in_allowed_tree(link, [allowed]))
            self.assertFalse(path_within(link, [allowed]))

    def test_host_path_translation_uses_path_boundaries(self) -> None:
        host_path = Path("/mnt/user/tubearchives/bp/Channel/video.mp4")
        similar_prefix = Path("/mnt/user/tubearchives/bp2/Channel/video.mp4")

        self.assertEqual(translate_host_path(host_path), SOURCE_DIR / "Channel/video.mp4")
        self.assertEqual(translate_host_path(similar_prefix), similar_prefix)


if __name__ == "__main__":
    unittest.main()
