from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import channel_designation
from ta_organizerr.media_reservations import (
    _reset_reservations_for_tests,
    media_identity,
    release_media,
    reserve_media,
)


CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
OTHER_CHANNEL_ID = "UCaaaaaaaaaaaaaaaaaaaaaa"


class ChannelDesignationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.target = self.base / "target"
        self.hidden = self.base / "hidden"
        self.journals = self.base / "data" / "designation-journals"
        self.target.mkdir()
        self.hidden.mkdir()
        self.journals.parent.mkdir()
        self.roots = (self.target, self.hidden)
        _reset_reservations_for_tests()

    def tearDown(self) -> None:
        _reset_reservations_for_tests()
        self.temporary_directory.cleanup()

    def _plan(
        self,
        component: str = "The Oversphere [Recovered]",
        **kwargs,
    ) -> dict:
        return channel_designation.build_designation_plan(
            CHANNEL_ID,
            component,
            root=kwargs.pop("root", self.target),
            revision=kwargs.pop("revision", 7),
            managed_roots=self.roots,
            **kwargs,
        )

    def _apply(self, plan: dict, update_paths, **kwargs) -> dict:
        kwargs.setdefault("database_state", lambda _journal: "old")
        return channel_designation.apply_designation_plan(
            CHANNEL_ID,
            plan["source_component"],
            plan["plan_token"],
            root=Path(plan["root"]),
            revision=plan["revision"],
            destination_component=plan["destination_component"],
            destination_channel_id=plan["destination_channel_id"],
            managed_roots=self.roots,
            journal_dir=self.journals,
            update_paths=update_paths,
            **kwargs,
        )

    def test_component_resolver_preserves_recovered_provenance(self) -> None:
        self.assertEqual(
            channel_designation.component_for_designation(
                "The Oversphere [Recovered]", "terminated"
            ),
            "The Oversphere [Recovered] [Terminated]",
        )
        self.assertEqual(
            channel_designation.resolve_channel_component(
                "The Oversphere",
                provenance="recovered",
                designation="terminated",
            ),
            "The Oversphere [Recovered] [Terminated]",
        )
        self.assertEqual(
            channel_designation.component_for_designation(
                "The Oversphere [Recovered] [Terminated]", "terminated"
            ),
            "The Oversphere [Recovered] [Terminated]",
        )

    def test_absent_destination_renames_complete_tree_without_rewriting_entries(self) -> None:
        source = self.target / "The Oversphere [Recovered]"
        nested = source / "nested"
        nested.mkdir(parents=True)
        media = nested / "real.mp4"
        media.write_bytes(b"unaltered media bytes")
        link = nested / "video.mp4"
        raw_target = "/mnt/archive/UCtbyZ5js_2m1LLi4nrrnFfw/g58pQud8er0.mp4"
        link.symlink_to(raw_target)
        old_file_identity = media_identity(os.lstat(media))
        old_link_identity = media_identity(os.lstat(link))

        plan = self._plan()
        self.assertTrue(plan["safe_to_apply"])
        self.assertEqual(plan["mode"], "rename")
        self.assertEqual(plan["counts"]["real_files"], 1)
        self.assertEqual(plan["counts"]["symlinks"], 1)
        updates = []
        result = self._apply(plan, updates.append)

        destination = self.target / "The Oversphere [Recovered] [Terminated]"
        moved_media = destination / "nested" / "real.mp4"
        moved_link = destination / "nested" / "video.mp4"
        self.assertFalse(source.exists())
        self.assertEqual(moved_media.read_bytes(), b"unaltered media bytes")
        self.assertEqual(media_identity(os.lstat(moved_media)), old_file_identity)
        self.assertEqual(media_identity(os.lstat(moved_link)), old_link_identity)
        self.assertEqual(os.readlink(moved_link), raw_target)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(updates[0]["old_path"], str(source))
        self.assertEqual(updates[0]["new_path"], str(destination))
        journals = channel_designation.list_designation_journals(
            journal_dir=self.journals, include_terminal=True
        )
        self.assertEqual(journals[0]["status"], "committed")

    def test_unsupported_noreplace_uses_whole_directory_compatibility_rename(self) -> None:
        source = self.target / "The Oversphere [Recovered]"
        source.mkdir()
        media = source / "real.mp4"
        media.write_bytes(b"unaltered media bytes")
        link = source / "video.mp4"
        raw_target = "/archive/UCtbyZ5js_2m1LLi4nrrnFfw/g58pQud8er0.mp4"
        link.symlink_to(raw_target)
        source_identity = media_identity(os.lstat(source))
        media_file_identity = media_identity(os.lstat(media))
        link_identity = media_identity(os.lstat(link))
        plan = self._plan()

        unsupported = OSError(
            errno.EINVAL,
            os.strerror(errno.EINVAL),
            source.name,
        )
        with patch.object(
            channel_designation,
            "_rename_noreplace",
            side_effect=unsupported,
        ):
            result = self._apply(plan, lambda _mutation: None)

        destination = self.target / "The Oversphere [Recovered] [Terminated]"
        self.assertEqual(result["rename_method"], "renameat_compat")
        self.assertFalse(source.exists())
        self.assertEqual(media_identity(os.lstat(destination)), source_identity)
        self.assertEqual(
            media_identity(os.lstat(destination / media.name)),
            media_file_identity,
        )
        self.assertEqual(
            media_identity(os.lstat(destination / link.name)),
            link_identity,
        )
        self.assertEqual(os.readlink(destination / link.name), raw_target)

    def test_compatibility_rename_rolls_back_after_database_failure(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        media = source / "video.mp4"
        media.write_bytes(b"media")
        source_identity = media_identity(os.lstat(source))
        media_file_identity = media_identity(os.lstat(media))
        plan = self._plan("Channel")

        with (
            patch.object(
                channel_designation,
                "_rename_noreplace",
                side_effect=OSError(errno.EINVAL, os.strerror(errno.EINVAL)),
            ),
            self.assertRaises(channel_designation.ChannelDesignationError),
        ):
            self._apply(
                plan,
                lambda _mutation: (_ for _ in ()).throw(RuntimeError("DB failed")),
            )

        self.assertEqual(media_identity(os.lstat(source)), source_identity)
        self.assertEqual(media_identity(os.lstat(media)), media_file_identity)
        self.assertFalse((self.target / "Channel [Terminated]").exists())
        journals = channel_designation.list_designation_journals(
            journal_dir=self.journals,
            include_terminal=True,
        )
        self.assertEqual(journals[0]["status"], "rolled_back")
        self.assertEqual(journals[0]["rename_method"], "renameat_compat")
        self.assertEqual(
            journals[0]["rollback_rename_method"],
            "renameat_compat",
        )

    def test_compatibility_rename_never_replaces_destination_seen_on_recheck(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        updated = False

        def destination_race(*_args):
            destination.mkdir()
            (destination / "foreign.mp4").write_bytes(b"foreign")
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

        def update(_mutation):
            nonlocal updated
            updated = True

        with (
            patch.object(
                channel_designation,
                "_rename_noreplace",
                side_effect=destination_race,
            ),
            self.assertRaises(channel_designation.ChannelDesignationError) as raised,
        ):
            self._apply(plan, update)

        self.assertEqual(raised.exception.code, "stale_plan")
        self.assertFalse(updated)
        self.assertEqual((source / "video.mp4").read_bytes(), b"media")
        self.assertEqual(
            (destination / "foreign.mp4").read_bytes(),
            b"foreign",
        )

    def test_unexpected_noreplace_error_does_not_use_compatibility_rename(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")

        with (
            patch.object(
                channel_designation,
                "_rename_noreplace",
                side_effect=OSError(errno.EACCES, os.strerror(errno.EACCES)),
            ),
            patch.object(channel_designation.os, "rename") as fallback,
            self.assertRaises(channel_designation.ChannelDesignationError),
        ):
            self._apply(plan, lambda _mutation: None)

        fallback.assert_not_called()
        self.assertEqual((source / "video.mp4").read_bytes(), b"media")
        self.assertFalse((self.target / "Channel [Terminated]").exists())

    def test_consolidation_does_not_use_directory_compatibility_fallback(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        destination.mkdir()
        (source / "source.mp4").write_bytes(b"source")
        (destination / "existing.mp4").write_bytes(b"existing")
        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        with (
            patch.object(
                channel_designation,
                "_rename_noreplace",
                side_effect=OSError(errno.EINVAL, os.strerror(errno.EINVAL)),
            ),
            patch.object(channel_designation.os, "rename") as fallback,
            self.assertRaises(channel_designation.ChannelDesignationError),
        ):
            self._apply(plan, lambda _mutation: None)

        fallback.assert_not_called()
        self.assertEqual((source / "source.mp4").read_bytes(), b"source")
        self.assertEqual(
            (destination / "existing.mp4").read_bytes(),
            b"existing",
        )
        self.assertFalse((destination / "source.mp4").exists())

    def test_old_source_path_reappearing_after_rename_fails_closed(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        original = channel_designation._rename_directory_noreplace_compat

        def rename_then_recreate(*args, **kwargs):
            method = original(*args, **kwargs)
            source.mkdir()
            (source / "foreign.mp4").write_bytes(b"foreign")
            return method

        with (
            patch.object(
                channel_designation,
                "_rename_directory_noreplace_compat",
                side_effect=rename_then_recreate,
            ),
            self.assertRaises(channel_designation.ChannelDesignationError) as raised,
        ):
            self._apply(plan, lambda _mutation: None)

        self.assertEqual(raised.exception.code, "recovery_blocked")
        self.assertEqual((source / "foreign.mp4").read_bytes(), b"foreign")
        self.assertEqual((destination / "video.mp4").read_bytes(), b"media")
        journals = channel_designation.list_designation_journals(
            journal_dir=self.journals,
            include_terminal=True,
        )
        self.assertEqual(journals[0]["status"], "recovery_blocked")

    def test_relative_internal_symlink_keeps_meaning(self) -> None:
        source = self.target / "Channel"
        (source / "files").mkdir(parents=True)
        (source / "files" / "real.mp4").write_bytes(b"media")
        (source / "links").mkdir()
        link = source / "links" / "video.mp4"
        link.symlink_to("../files/real.mp4")

        plan = self._plan("Channel")
        self.assertTrue(plan["safe_to_apply"])
        self._apply(plan, lambda _mutation: None)

        moved = self.target / "Channel [Terminated]" / "links" / "video.mp4"
        self.assertEqual(os.readlink(moved), "../files/real.mp4")
        self.assertEqual(moved.read_bytes(), b"media")

    def test_absolute_link_into_old_tree_blocks_because_text_would_break(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        media = source / "real.mp4"
        media.write_bytes(b"media")
        (source / "video.mp4").symlink_to(media)

        plan = self._plan("Channel")

        self.assertFalse(plan["safe_to_apply"])
        self.assertIn("symlink_meaning_changes", {item["code"] for item in plan["blockers"]})
        self.assertTrue(source.exists())

    def test_stale_plan_is_rejected_before_mutation(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "first.mp4").write_bytes(b"one")
        plan = self._plan("Channel")
        (source / "later.mp4").write_bytes(b"two")
        called = False

        def updater(_mutation):
            nonlocal called
            called = True

        with self.assertRaises(channel_designation.ChannelDesignationError) as raised:
            self._apply(plan, updater)

        self.assertEqual(raised.exception.code, "stale_plan")
        self.assertFalse(called)
        self.assertTrue(source.exists())
        self.assertFalse((self.target / "Channel [Terminated]").exists())

    def test_existing_destination_requires_exact_same_channel_identity(self) -> None:
        (self.target / "Channel").mkdir()
        (self.target / "Channel [Terminated]").mkdir()

        unknown = self._plan("Channel")
        mismatch = self._plan(
            "Channel", destination_channel_id=OTHER_CHANNEL_ID
        )
        matching = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        self.assertIn(
            "destination_identity_unknown", {item["code"] for item in unknown["blockers"]}
        )
        self.assertIn(
            "destination_identity_mismatch", {item["code"] for item in mismatch["blockers"]}
        )
        self.assertTrue(matching["safe_to_apply"])

    def test_verified_same_channel_consolidates_without_overwrite(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        destination.mkdir()
        real = source / "new.mp4"
        real.write_bytes(b"new")
        link = source / "new-link.mp4"
        link.symlink_to("new.mp4")
        (destination / "existing.mp4").write_bytes(b"existing")
        old_real_identity = media_identity(os.lstat(real))
        old_link_identity = media_identity(os.lstat(link))

        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)
        self.assertEqual(plan["mode"], "consolidate")
        self.assertTrue(plan["safe_to_apply"])
        self._apply(plan, lambda _mutation: None)

        self.assertFalse(source.exists())
        self.assertEqual((destination / "existing.mp4").read_bytes(), b"existing")
        self.assertEqual((destination / "new.mp4").read_bytes(), b"new")
        self.assertEqual(media_identity(os.lstat(destination / "new.mp4")), old_real_identity)
        self.assertEqual(media_identity(os.lstat(destination / "new-link.mp4")), old_link_identity)
        self.assertEqual(os.readlink(destination / "new-link.mp4"), "new.mp4")

    def test_same_named_directories_merge_recursively_when_children_do_not_collide(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        (source / "2026 - Archive").mkdir(parents=True)
        (destination / "2026 - Archive").mkdir(parents=True)
        moved = source / "2026 - Archive" / "new.mp4"
        kept = destination / "2026 - Archive" / "existing.mp4"
        moved.write_bytes(b"new")
        kept.write_bytes(b"existing")
        moved_identity = media_identity(os.lstat(moved))

        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        self.assertTrue(plan["safe_to_apply"])
        self.assertEqual(
            plan["move_items"][0]["relative_path"],
            "2026 - Archive/new.mp4",
        )
        self._apply(plan, lambda _mutation: None)
        self.assertFalse(source.exists())
        self.assertEqual(kept.read_bytes(), b"existing")
        moved = destination / "2026 - Archive" / "new.mp4"
        self.assertEqual(moved.read_bytes(), b"new")
        self.assertEqual(media_identity(os.lstat(moved)), moved_identity)

    def test_database_failure_restores_recursively_merged_directories(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        (source / "year" / "month").mkdir(parents=True)
        (destination / "year" / "month").mkdir(parents=True)
        moved = source / "year" / "month" / "video.mp4"
        moved.write_bytes(b"media")
        identity = media_identity(os.lstat(moved))
        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        with self.assertRaises(channel_designation.ChannelDesignationError):
            self._apply(
                plan,
                lambda _mutation: (_ for _ in ()).throw(RuntimeError("DB")),
            )

        restored = source / "year" / "month" / "video.mp4"
        self.assertEqual(restored.read_bytes(), b"media")
        self.assertEqual(media_identity(os.lstat(restored)), identity)
        self.assertFalse((destination / "year" / "month" / "video.mp4").exists())

    def test_any_relative_path_collision_blocks_complete_consolidation(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        destination.mkdir()
        (source / "same.mp4").write_bytes(b"source")
        (source / "otherwise-safe.mp4").write_bytes(b"safe")
        (destination / "same.mp4").write_bytes(b"destination")

        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        self.assertFalse(plan["safe_to_apply"])
        self.assertEqual(plan["conflicts"][0]["relative_path"], "same.mp4")
        with self.assertRaises(channel_designation.ChannelDesignationError):
            self._apply(plan, lambda _mutation: None)
        self.assertEqual((source / "same.mp4").read_bytes(), b"source")
        self.assertEqual((destination / "same.mp4").read_bytes(), b"destination")
        self.assertTrue((source / "otherwise-safe.mp4").exists())

    def test_database_failure_rolls_back_whole_folder_rename(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        real = source / "video.mp4"
        real.write_bytes(b"media")
        identity = media_identity(os.lstat(real))
        plan = self._plan("Channel")

        def fail(_mutation):
            raise RuntimeError("CAS failed")

        with self.assertRaises(channel_designation.ChannelDesignationError):
            self._apply(plan, fail)

        self.assertTrue(source.is_dir())
        self.assertFalse((self.target / "Channel [Terminated]").exists())
        self.assertEqual(media_identity(os.lstat(real)), identity)
        journals = channel_designation.list_designation_journals(
            journal_dir=self.journals, include_terminal=True
        )
        self.assertEqual(journals[0]["status"], "rolled_back")

    def test_database_failure_rolls_back_partial_safe_consolidation(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        destination.mkdir()
        (source / "one.mp4").write_bytes(b"one")
        (source / "two.mp4").write_bytes(b"two")
        (destination / "keep.mp4").write_bytes(b"keep")
        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        with self.assertRaises(channel_designation.ChannelDesignationError):
            self._apply(plan, lambda _mutation: (_ for _ in ()).throw(RuntimeError("DB")))

        self.assertEqual((source / "one.mp4").read_bytes(), b"one")
        self.assertEqual((source / "two.mp4").read_bytes(), b"two")
        self.assertEqual((destination / "keep.mp4").read_bytes(), b"keep")
        self.assertFalse((destination / "one.mp4").exists())
        self.assertFalse((destination / "two.mp4").exists())

    def test_failed_consolidation_preserves_original_directory_inode_and_mode(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir(mode=0o750)
        destination.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        original = os.lstat(source)
        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        with self.assertRaises(channel_designation.ChannelDesignationError):
            self._apply(
                plan,
                lambda _mutation: (_ for _ in ()).throw(RuntimeError("DB")),
            )

        restored = os.lstat(source)
        self.assertEqual((restored.st_dev, restored.st_ino), (original.st_dev, original.st_ino))
        self.assertEqual(restored.st_mode & 0o777, original.st_mode & 0o777)

    def test_foreign_old_directory_is_never_accepted_for_rollback(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        displaced = self.target / "displaced-original"
        source.mkdir()
        destination.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        def swap_then_fail(_mutation):
            source.rename(displaced)
            source.mkdir()
            (source / "foreign.txt").write_text("foreign", encoding="utf-8")
            raise RuntimeError("DB failed")

        with self.assertRaises(channel_designation.ChannelDesignationError) as raised:
            self._apply(plan, swap_then_fail)

        self.assertEqual(raised.exception.code, "recovery_blocked")
        self.assertEqual((source / "foreign.txt").read_text(encoding="utf-8"), "foreign")
        self.assertTrue(displaced.is_dir())
        self.assertTrue((destination / "video.mp4").exists())

    def test_entry_added_between_revalidation_and_rename_is_detected_and_rolled_back(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")

        def race(_plan):
            (source / "late-link.mp4").symlink_to(source / "video.mp4")

        with self.assertRaises(channel_designation.ChannelDesignationError) as raised:
            self._apply(plan, lambda _mutation: None, validate_precondition=race)

        self.assertEqual(raised.exception.code, "inventory_changed")
        self.assertTrue(source.is_dir())
        self.assertTrue((source / "late-link.mp4").is_symlink())
        self.assertFalse((self.target / "Channel [Terminated]").exists())

    def test_commit_then_raise_uses_mandatory_database_state_and_never_rolls_back(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")

        def committed_then_disconnect(_mutation):
            raise RuntimeError("connection dropped after commit")

        result = self._apply(
            plan,
            committed_then_disconnect,
            database_state=lambda _journal: "new",
        )

        self.assertTrue(result["success"])
        self.assertFalse(source.exists())
        self.assertTrue((self.target / "Channel [Terminated]" / "video.mp4").exists())

    def test_casefold_collision_blocks_consolidation(self) -> None:
        source = self.target / "Channel"
        destination = self.target / "Channel [Terminated]"
        source.mkdir()
        destination.mkdir()
        (source / "Video.mp4").write_bytes(b"source")
        (destination / "video.mp4").write_bytes(b"destination")

        plan = self._plan("Channel", destination_channel_id=CHANNEL_ID)

        self.assertFalse(plan["safe_to_apply"])
        self.assertIn("casefold_collision", {item["code"] for item in plan["blockers"]})

    def test_multihop_inbound_symlink_is_detected(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        media = source / "video.mp4"
        media.write_bytes(b"media")
        other = self.target / "Other"
        other.mkdir()
        first = other / "first.mp4"
        second = other / "second.mp4"
        first.symlink_to(media)
        second.symlink_to(first)

        plan = self._plan("Channel")

        inbound_paths = {
            item["path"] for item in plan["blockers"] if item["code"] == "inbound_symlink"
        }
        self.assertIn(str(first), inbound_paths)
        self.assertIn(str(second), inbound_paths)

    def test_media_directory_fsync_precedes_database_update(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        events = []
        real_fsync = channel_designation._fsync_fd

        def record_fsync(descriptor):
            events.append("fsync")
            real_fsync(descriptor)

        def updater(_mutation):
            events.append("database")

        with patch.object(channel_designation, "_fsync_fd", side_effect=record_fsync):
            self._apply(plan, updater)

        self.assertIn("fsync", events)
        self.assertLess(events.index("fsync"), events.index("database"))

    def test_media_fsync_failure_rolls_back_before_database_update(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        calls = 0
        database_called = False
        real_fsync = channel_designation._fsync_fd

        def fail_first(descriptor):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("durability barrier failed")
            real_fsync(descriptor)

        def updater(_mutation):
            nonlocal database_called
            database_called = True

        with patch.object(channel_designation, "_fsync_fd", side_effect=fail_first):
            with self.assertRaises(channel_designation.ChannelDesignationError):
                self._apply(plan, updater)

        self.assertFalse(database_called)
        self.assertTrue(source.is_dir())
        self.assertFalse((self.target / "Channel [Terminated]").exists())

    def test_inbound_symlink_blocks_folder_rename(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        media = source / "video.mp4"
        media.write_bytes(b"media")
        outside = self.target / "Other"
        outside.mkdir()
        (outside / "alias.mp4").symlink_to(media)

        plan = self._plan("Channel")

        self.assertIn("inbound_symlink", {item["code"] for item in plan["blockers"]})
        self.assertFalse(plan["safe_to_apply"])

    def test_active_media_reservation_blocks_apply_and_preserves_folder(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        media = source / "video.mp4"
        media.write_bytes(b"media")
        plan = self._plan("Channel")
        reservation = reserve_media(
            (media_identity(media.stat()),), paths=(media,), owner="salvage test"
        )
        try:
            with self.assertRaises(channel_designation.ChannelDesignationError) as raised:
                self._apply(plan, lambda _mutation: None)
        finally:
            release_media(reservation)

        self.assertEqual(raised.exception.code, "media_busy")
        self.assertTrue(source.exists())

    def test_recovery_uses_database_state_to_finish_or_roll_back(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        journal = channel_designation._journal_from_plan(plan)
        self.journals.mkdir(mode=0o700)
        channel_designation._write_journal(self.journals, journal)
        roots = channel_designation._normalize_managed_roots(self.roots)
        with channel_designation._open_root(self.target, roots) as (_root, root_fd, _stat):
            channel_designation._apply_filesystem(
                root_fd, self.journals, journal, roots
            )

        result = channel_designation.recover_designation_journals(
            lambda _journal: "old",
            managed_roots=self.roots,
            journal_dir=self.journals,
        )
        self.assertEqual(result[0]["status"], "rolled_back")
        self.assertTrue(source.exists())

        # Simulate the complementary crash window: filesystem + DB are new,
        # while the durable journal still says only filesystem_applied.
        plan = self._plan("Channel", revision=8)
        journal = channel_designation._journal_from_plan(plan)
        channel_designation._write_journal(self.journals, journal)
        with channel_designation._open_root(self.target, roots) as (_root, root_fd, _stat):
            channel_designation._apply_filesystem(
                root_fd, self.journals, journal, roots
            )
        result = channel_designation.recover_designation_journals(
            lambda current: "new" if current["journal_id"] == journal["journal_id"] else "old",
            managed_roots=self.roots,
            journal_dir=self.journals,
        )
        by_id = {item["journal_id"]: item for item in result}
        self.assertEqual(by_id[journal["journal_id"]]["status"], "committed")
        self.assertTrue((self.target / "Channel [Terminated]").exists())

    def test_no_pending_journal_does_not_require_media_roots_to_exist(self) -> None:
        missing_roots = (self.base / "missing-target", self.base / "missing-hidden")

        result = channel_designation.recover_designation_journals(
            lambda _journal: "unknown",
            managed_roots=missing_roots,
            journal_dir=self.journals,
        )

        self.assertEqual(result, [])

    def test_journal_contains_no_media_bytes_and_is_valid_json(self) -> None:
        source = self.hidden / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"secret-media-bytes")
        plan = self._plan("Channel", root=self.hidden)
        self._apply(plan, lambda _mutation: None)
        path = next(self.journals.glob("*.json"))
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertNotIn("secret-media-bytes", raw)
        self.assertEqual(payload["root"], str(self.hidden))
        self.assertEqual(payload["status"], "committed")

    def test_unresolved_journal_blocks_every_new_designation(self) -> None:
        source = self.target / "Channel"
        source.mkdir()
        (source / "video.mp4").write_bytes(b"media")
        plan = self._plan("Channel")
        pending = channel_designation._journal_from_plan(plan)
        self.journals.mkdir(mode=0o700)
        channel_designation._write_journal(self.journals, pending)

        with self.assertRaises(channel_designation.ChannelDesignationError) as raised:
            self._apply(plan, lambda _mutation: None)

        self.assertEqual(raised.exception.code, "unresolved_journal")
        self.assertTrue(source.is_dir())
        self.assertFalse((self.target / "Channel [Terminated]").exists())


if __name__ == "__main__":
    unittest.main()
