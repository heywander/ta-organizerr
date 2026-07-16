from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from ta_organizerr import symlinks
from ta_organizerr.symlinks import _destination_for_video
from ta_organizerr.utils import channel_component


class DestinationCollisionTests(unittest.TestCase):
    def test_lossy_channel_names_get_distinct_stable_components(self) -> None:
        first = channel_component("频道")
        second = channel_component("チャンネル")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("untitled ["))
        self.assertEqual(channel_component("Ordinary Channel"), "Ordinary Channel")
        self.assertTrue(channel_component("Foo: Bar").startswith("Foo_ Bar ["))

        collision_map = symlinks._channel_component_map({"Foo: Bar", "Foo? Bar"})
        self.assertNotEqual(collision_map["Foo: Bar"], collision_map["Foo? Bar"])

    def test_superseded_generated_link_is_retired_by_video_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            hidden = root / "hidden"
            old_dir = target / "legacy" / "old title"
            new_dir = target / "stable" / "new title"
            old_dir.mkdir(parents=True)
            new_dir.mkdir(parents=True)
            old_link = old_dir / "video.mp4"
            new_link = new_dir / "video.mp4"
            source = root / "Title [AAAAAAAAAAA].mp4"
            old_link.symlink_to(source)
            new_link.symlink_to(source)

            with (
                patch.object(symlinks, "TARGET_DIR", target),
                patch.object(symlinks, "HIDDEN_DIR", hidden),
            ):
                index = symlinks._index_managed_links([{
                    "video_id": "AAAAAAAAAAA",
                    "symlink": str(old_link),
                }])
                symlinks._remove_stale_managed_links(
                    "AAAAAAAAAAA",
                    new_link,
                    index,
                )

            self.assertFalse(os.path.lexists(old_link))
            self.assertTrue(new_link.is_symlink())

    def test_old_db_owned_link_survives_new_snapshot_persistence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            target = root / "target"
            hidden = root / "hidden"
            source_root.mkdir()
            target.mkdir()
            hidden.mkdir()
            source = source_root / "Title [AAAAAAAAAAA].mp4"
            source.write_bytes(b"media")
            old_dir = target / "legacy" / "2026-01-01 - Title"
            old_dir.mkdir(parents=True)
            old_link = old_dir / "video.mp4"
            old_link.symlink_to(root / "archive" / source.name)

            hidden_result = Mock()
            hidden_result.fetchall.return_value = []
            prior_result = Mock()
            prior_result.fetchall.return_value = [{
                "video_id": "AAAAAAAAAAA",
                "symlink": str(old_link),
            }]
            first_connection = Mock()
            first_connection.execute.side_effect = [hidden_result, prior_result]

            @contextmanager
            def failing_db():
                raise RuntimeError("database write failed")
                yield

            with (
                patch.object(symlinks, "SOURCE_DIR", source_root),
                patch.object(symlinks, "TARGET_DIR", target),
                patch.object(symlinks, "HIDDEN_DIR", hidden),
                patch.object(symlinks, "HOST_SOURCE_BASE", root / "archive"),
                patch.object(symlinks, "fetch_all_metadata", return_value={
                    "AAAAAAAAAAA": {
                        "title": "Title",
                        "channel_name": "New: Channel",
                        "published": "2026-01-01",
                        "is_live": False,
                        "filesystem_path": str(source),
                    }
                }),
                patch.object(
                    symlinks,
                    "get_db",
                    side_effect=[nullcontext(first_connection), failing_db()],
                ),
            ):
                result = symlinks._process_videos_unlocked()

            self.assertIn("database write failed", result)
            self.assertTrue(old_link.is_symlink())

    def test_hidden_enforcement_never_traverses_channel_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            hidden = root / "hidden"
            outside = root / "outside"
            target.mkdir()
            hidden.mkdir()
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            channel_link = target / "Private"
            channel_link.symlink_to(outside, target_is_directory=True)

            with (
                patch.object(symlinks, "TARGET_DIR", target),
                patch.object(symlinks, "HIDDEN_DIR", hidden),
            ):
                symlinks._enforce_hidden_locations({"Private"})

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(channel_link.is_symlink())
            self.assertFalse((hidden / "Private").exists())

    def test_distinct_video_ids_get_distinct_folders_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "Channel"
            legacy_dir = channel / "2026-01-01 - Same title"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "video.mp4").symlink_to(
                "/archive/Same title [AAAAAAAAAAA].mp4"
            )

            video_dir, video_file = _destination_for_video(
                channel,
                "2026-01-01 - Same title",
                "BBBBBBBBBBB",
                ".mp4",
            )

            self.assertEqual(video_dir.name, "2026-01-01 - Same title [BBBBBBBBBBB]")
            self.assertEqual(video_file, video_dir / "video.mp4")

    def test_same_video_id_keeps_legacy_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "Channel"
            legacy_dir = channel / "2026-01-01 - Same title"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "video.mp4").symlink_to(
                "/archive/Same title [AAAAAAAAAAA].mp4"
            )

            video_dir, _ = _destination_for_video(
                channel,
                "2026-01-01 - Same title",
                "AAAAAAAAAAA",
                ".mp4",
            )

            self.assertEqual(video_dir, legacy_dir)

    def test_real_media_in_preferred_folder_uses_unique_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "Channel"
            legacy_dir = channel / "2026-01-01 - Same title"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "video.mp4").write_bytes(b"real media")

            video_dir, _ = _destination_for_video(
                channel,
                "2026-01-01 - Same title",
                "BBBBBBBBBBB",
                ".mp4",
            )

            self.assertEqual(video_dir.name, "2026-01-01 - Same title [BBBBBBBBBBB]")

    def test_unknown_id_symlink_uses_unique_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "Channel"
            legacy_dir = channel / "2026-01-01 - Same title"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "video.mp4").symlink_to("/archive/legacy-name.mp4")

            video_dir, _ = _destination_for_video(
                channel,
                "2026-01-01 - Same title",
                "BBBBBBBBBBB",
                ".mp4",
            )

            self.assertEqual(video_dir.name, "2026-01-01 - Same title [BBBBBBBBBBB]")


if __name__ == "__main__":
    unittest.main()
