from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ta_organizerr import (
    db,
    reconciliation,
    recovered_channels,
    recovery,
    symlinks,
)
from ta_organizerr.media_reservations import (
    media_identity,
    release_media,
    reserve_media,
)

try:
    from flask import Flask
    from ta_organizerr.routes import (
        recovered_channels as recovered_channel_routes,
        recovery as recovery_routes,
        status as status_routes,
        transcode as transcode_routes,
    )
except ModuleNotFoundError:  # The lightweight host interpreter lacks Flask.
    Flask = None  # type: ignore[assignment,misc]
    recovered_channel_routes = None  # type: ignore[assignment]
    recovery_routes = None  # type: ignore[assignment]
    status_routes = None  # type: ignore[assignment]
    transcode_routes = None  # type: ignore[assignment]


SOURCE_CHANNEL_ID = "UCtbyZ5js_2m1LLi4nrrnFfw"
OTHER_CHANNEL_ID = "UCaaaaaaaaaaaaaaaaaaaaaa"
VIDEO_ID = "g58pQud8er0"
SECOND_VIDEO_ID = "abcdefghijk"


class RecoveredChannelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_dir = self.root / "source"
        self.target_dir = self.root / "target"
        self.hidden_dir = self.root / "hidden"
        self.host_source_dir = self.root / "host-source"
        self.data_dir = self.root / "data"
        for directory in (
            self.source_dir,
            self.target_dir,
            self.hidden_dir,
            self.host_source_dir,
            self.data_dir,
        ):
            directory.mkdir()

        self.patchers = (
            patch.object(db, "DB_PATH", self.data_dir / "videos.db"),
            patch.object(recovered_channels, "SOURCE_DIR", self.source_dir),
            patch.object(recovered_channels, "TARGET_DIR", self.target_dir),
            patch.object(
                recovered_channels,
                "HOST_SOURCE_BASE",
                self.host_source_dir,
            ),
            patch.object(recovery, "SOURCE_DIR", self.source_dir),
            patch.object(symlinks, "SOURCE_DIR", self.source_dir),
            patch.object(symlinks, "TARGET_DIR", self.target_dir),
            patch.object(symlinks, "HIDDEN_DIR", self.hidden_dir),
            patch.object(symlinks, "HOST_SOURCE_BASE", self.host_source_dir),
            patch.object(reconciliation, "SOURCE_DIR", self.source_dir),
            patch.object(reconciliation, "TARGET_DIR", self.target_dir),
            patch.object(reconciliation, "HIDDEN_DIR", self.hidden_dir),
            patch.object(
                reconciliation,
                "HOST_SOURCE_BASE",
                self.host_source_dir,
            ),
            patch.object(
                reconciliation,
                "MANAGED_ROOTS",
                (self.target_dir, self.hidden_dir),
            ),
            patch.object(
                reconciliation,
                "MEDIA_ROOTS",
                (self.source_dir, self.target_dir, self.hidden_dir),
            ),
        )
        for patcher in self.patchers:
            patcher.start()
        db.init_db()
        recovered_channels.reset_runtime_for_tests()
        reconciliation._reset_scan_runtime_for_tests()

    def tearDown(self) -> None:
        recovered_channels.reset_runtime_for_tests()
        reconciliation._reset_scan_runtime_for_tests()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def make_media(
        self,
        video_id: str = VIDEO_ID,
        *,
        channel_id: str = SOURCE_CHANNEL_ID,
        suffix: str = ".mp4",
        payload: bytes | None = None,
        filename: str | None = None,
    ) -> Path:
        channel = self.source_dir / channel_id
        channel.mkdir(exist_ok=True)
        media = channel / (filename or f"{video_id}{suffix}")
        media.write_bytes(payload or f"media:{video_id}:{suffix}".encode())
        return media

    def create_case(self, name: str = "The Oversphere") -> dict:
        return recovered_channels.create_case(
            SOURCE_CHANNEL_ID,
            name,
            actor="tester",
        )

    def items(self, case_id: str) -> list[dict]:
        return recovered_channels.list_items(
            case_id,
            page=1,
            per_page=500,
        )["items"]

    @staticmethod
    def case_id(case: dict) -> str:
        return str(case["case_id"])

    @staticmethod
    def item_id(item: dict) -> str:
        return str(item["item_id"])

    def seed_completed_inspection(
        self,
        item: dict,
        *,
        health_tier: str = "minimal",
    ) -> str:
        source = Path(item["source_path"])
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        diagnosis = {
            "assessment": "healthy" if health_tier in {"clear", "minimal"} else "damaged",
            "input_sha256": checksum,
            "source": {
                "path": str(source),
                "sha256": checksum,
                "warnings": [] if health_tier in {"clear", "minimal"} else ["decode warning"],
            },
            "warnings": [] if health_tier in {"clear", "minimal"} else ["decode warning"],
        }
        with db.get_db() as connection:
            connection.execute(
                """
                UPDATE recovered_channel_items
                SET inspection_status = 'completed', health_tier = ?,
                    sha256 = ?, diagnosis_json = ?, error = NULL
                WHERE item_id = ?
                """,
                (
                    health_tier,
                    checksum,
                    json.dumps(diagnosis),
                    self.item_id(item),
                ),
            )
            connection.commit()
        return checksum

    def approve(self, case_id: str, *items: dict) -> None:
        updated = recovered_channels.bulk_decision(
            case_id,
            [self.item_id(item) for item in items],
            "approved",
            actor="tester",
        )
        self.assertEqual(updated, len(items))

    def organize(self, case_id: str) -> tuple[dict, dict]:
        plan = recovered_channels.build_organization_plan(case_id)
        job = recovered_channels.start_organization(
            case_id,
            plan["plan_token"],
            actor="tester",
            background=False,
        )
        current = recovered_channels.get_job(case_id, job["job_id"])
        if current["status"] in {"queued", "running"}:
            recovered_channels.run_organization_job(job["job_id"])
            current = recovered_channels.get_job(case_id, job["job_id"])
        return plan, current

    @staticmethod
    def source_signature(path: Path) -> tuple[bytes, int, int, int, int, int]:
        state = os.stat(path)
        return (
            path.read_bytes(),
            state.st_dev,
            state.st_ino,
            state.st_nlink,
            state.st_size,
            state.st_ctime_ns,
        )

    @staticmethod
    def valid_probe() -> dict:
        return {
            "valid": True,
            "valid_media": True,
            "error": None,
            "duration": 60.0,
            "streams": [
                {"index": 0, "type": "video", "codec": "h264", "duration": "60"},
                {"index": 1, "type": "audio", "codec": "aac", "duration": "60"},
            ],
            "has_video": True,
            "has_audio": True,
        }


class RecoveredChannelInventoryTests(RecoveredChannelTestCase):
    def test_case_inventory_is_exactly_confined_to_one_real_raw_channel(self) -> None:
        selected = self.make_media()
        self.make_media(SECOND_VIDEO_ID, channel_id=OTHER_CHANNEL_ID)
        alias = selected.parent / f"alias [{SECOND_VIDEO_ID}].mp4"
        alias.symlink_to(selected)
        nested = selected.parent / "nested"
        nested.mkdir()
        nested_media = nested / f"{SECOND_VIDEO_ID}.mkv"
        nested_media.write_bytes(b"nested-media")

        case = self.create_case()
        items = self.items(self.case_id(case))

        self.assertEqual(
            {item["source_path"] for item in items},
            {str(selected), str(nested_media)},
        )
        self.assertTrue(all(item["source_channel_id"] == SOURCE_CHANNEL_ID for item in items))
        self.assertTrue(alias.is_symlink())

    def test_inventory_and_case_creation_never_modify_source_media(self) -> None:
        source = self.make_media(payload=b"source-archive-bytes")
        before = os.stat(source)
        before_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        case = self.create_case()

        after = os.stat(source)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before_hash)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(after.st_nlink, before.st_nlink)
        self.assertEqual(self.items(self.case_id(case))[0]["source_path"], str(source))
        self.assertEqual(list(self.target_dir.iterdir()), [])

    def test_channel_id_and_display_name_validation_fail_closed(self) -> None:
        self.make_media()
        invalid_channel_ids = (
            "",
            "not-a-channel",
            "../" + SOURCE_CHANNEL_ID,
            "UCshort",
        )
        for channel_id in invalid_channel_ids:
            with self.subTest(channel_id=channel_id):
                with self.assertRaises(recovered_channels.RecoveredChannelError):
                    recovered_channels.create_case(channel_id, "Safe Name")

        for name in ("", "   ", "../escape", "A/B", r"A\B", ".", "..", "CON", "Name."):
            with self.subTest(name=name):
                with self.assertRaises(recovered_channels.RecoveredChannelError):
                    recovered_channels.create_case(SOURCE_CHANNEL_ID, name)

    def test_recovered_suffix_is_normalized_exactly_once(self) -> None:
        self.make_media()
        case = self.create_case("The Oversphere [Recovered]")

        self.assertEqual(case["target_channel_name"], "The Oversphere")
        self.assertEqual(case["target_component"], "The Oversphere [Recovered]")
        self.assertNotIn("[Recovered] [Recovered]", case["target_component"])

    def test_case_claim_refuses_media_already_reserved_by_a_mutating_worker(self) -> None:
        source = self.make_media(payload=b"about to be transcoded")
        before = self.source_signature(source)
        reservation = reserve_media(
            {media_identity(source.stat())},
            paths=(source,),
            owner="transcode job already in progress",
        )
        try:
            with self.assertRaises(recovered_channels.RecoveredChannelError) as raised:
                self.create_case()
        finally:
            release_media(reservation)

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.source_signature(source), before)
        with db.get_db() as connection:
            case_count = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_cases"
            ).fetchone()["count"]
            item_count = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_items"
            ).fetchone()["count"]
        self.assertEqual(case_count, 0)
        self.assertEqual(item_count, 0)


class RecoveredOwnershipBootstrapTests(RecoveredChannelTestCase):
    def test_absent_database_or_pre_migration_schema_is_the_only_fail_open_case(self) -> None:
        media = self.make_media()
        missing = self.root / "bootstrap" / "missing.db"
        with patch.object(db, "DB_PATH", missing):
            self.assertFalse(recovered_channels.is_recovered_media_path(media))
            self.assertEqual(recovered_channels.organized_recovered_video_ids(), set())

        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        with patch.object(db, "DB_PATH", empty):
            self.assertFalse(recovered_channels.is_recovered_media_path(media))
            self.assertEqual(recovered_channels.organized_recovered_video_ids(), set())

    def test_locked_unreadable_or_corrupt_ownership_database_never_looks_unowned(self) -> None:
        media = self.make_media()
        for message in (
            "database is locked",
            "unable to open database file",
            "disk I/O error",
        ):
            with self.subTest(message=message):
                with patch.object(
                    recovered_channels,
                    "get_db",
                    side_effect=sqlite3.OperationalError(message),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        recovered_channels.is_recovered_media_path(media)

        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        with patch.object(db, "DB_PATH", corrupt):
            with self.assertRaises(sqlite3.DatabaseError):
                recovered_channels.is_recovered_media_path(media)


class RecoveredChannelOrganizationTests(RecoveredChannelTestCase):
    def test_direct_raw_id_symlink_layout_preserves_source(self) -> None:
        source = self.make_media(suffix=".mkv", payload=b"archive-copy")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        source_stat = os.stat(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)

        _plan, job = self.organize(case_id)

        self.assertIn(job["status"], {"completed", "partial"})
        destination = self.target_dir / "The Oversphere [Recovered]" / f"{VIDEO_ID}.mkv"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            os.readlink(destination),
            str(self.host_source_dir / SOURCE_CHANNEL_ID / f"{VIDEO_ID}.mkv"),
        )
        after = os.stat(source)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
        self.assertEqual((after.st_dev, after.st_ino), (source_stat.st_dev, source_stat.st_ino))
        self.assertEqual(after.st_nlink, source_stat.st_nlink)

    def test_mixed_case_media_suffix_is_preserved_exactly_in_recovered_link(self) -> None:
        source = self.make_media(
            suffix=".Mp4",
            payload=b"mixed-case container suffix",
        )
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.assertEqual(item["suffix"], ".Mp4")
        self.seed_completed_inspection(item)
        self.approve(case_id, item)

        _plan, job = self.organize(case_id)

        self.assertEqual(job["status"], "completed")
        exact = (
            self.target_dir
            / "The Oversphere [Recovered]"
            / f"{VIDEO_ID}.Mp4"
        )
        lower = exact.with_suffix(".mp4")
        self.assertTrue(exact.is_symlink())
        self.assertFalse(os.path.lexists(lower))
        self.assertEqual(
            os.readlink(exact),
            str(self.host_source_dir / SOURCE_CHANNEL_ID / f"{VIDEO_ID}.Mp4"),
        )
        self.assertEqual(self.items(case_id)[0]["organized_path"], str(exact))
        self.assertEqual(self.source_signature(source), before)

    def test_organization_requires_completed_diagnosis_and_approval(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]

        plan = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(plan["ready_count"], 0)

        self.seed_completed_inspection(item)
        still_unapproved = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(still_unapproved["ready_count"], 0)

        self.approve(case_id, item)
        approved = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(approved["ready_count"], 1)

    def test_existing_real_file_or_foreign_link_is_never_replaced(self) -> None:
        for existing_kind in ("file", "symlink"):
            with self.subTest(existing_kind=existing_kind):
                recovered_channels.reset_runtime_for_tests()
                with db.get_db() as connection:
                    for table in (
                        "recovered_channel_events",
                        "recovered_channel_jobs",
                        "recovered_channel_items",
                        "recovered_channel_cases",
                    ):
                        connection.execute(f"DELETE FROM {table}")
                    connection.commit()
                for child in self.target_dir.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        for entry in child.iterdir():
                            entry.unlink()
                        child.rmdir()

                source = self.make_media(payload=b"source-original")
                case = self.create_case()
                case_id = self.case_id(case)
                item = self.items(case_id)[0]
                self.seed_completed_inspection(item)
                self.approve(case_id, item)
                destination_dir = self.target_dir / "The Oversphere [Recovered]"
                destination_dir.mkdir()
                destination = destination_dir / f"{VIDEO_ID}.mp4"
                if existing_kind == "file":
                    destination.write_bytes(b"foreign-real-file")
                else:
                    foreign = self.root / "foreign.mp4"
                    foreign.write_bytes(b"foreign-target")
                    destination.symlink_to(foreign)
                existing_target = os.readlink(destination) if destination.is_symlink() else None

                plan = recovered_channels.build_organization_plan(case_id)

                self.assertEqual(plan["ready_count"], 0)
                self.assertEqual(len(plan["conflicts"]), 1)
                with self.assertRaises(recovered_channels.RecoveredChannelError):
                    recovered_channels.start_organization(
                        case_id,
                        plan["token"],
                        actor="tester",
                        background=False,
                    )
                if existing_kind == "file":
                    self.assertEqual(destination.read_bytes(), b"foreign-real-file")
                else:
                    self.assertEqual(os.readlink(destination), existing_target)
                self.assertEqual(source.read_bytes(), b"source-original")

    def test_existing_database_owner_is_never_replaced(self) -> None:
        self.make_media(payload=b"recovered-source")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        with db.get_db() as connection:
            connection.execute(
                """
                INSERT INTO videos (video_id, title, channel, symlink, status)
                VALUES (?, 'existing', 'Existing Channel', '/existing/video.mp4', 'linked')
                """,
                (VIDEO_ID,),
            )
            connection.commit()

        plan = recovered_channels.build_organization_plan(case_id)

        self.assertEqual(plan["ready_count"], 0)
        self.assertIn("database", plan["conflicts"][0]["conflict"].lower())
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT title, channel, symlink, status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
        self.assertEqual(dict(owner), {
            "title": "existing",
            "channel": "Existing Channel",
            "symlink": "/existing/video.mp4",
            "status": "linked",
        })
        self.assertFalse(
            (self.target_dir / "The Oversphere [Recovered]" / f"{VIDEO_ID}.mp4").exists()
        )

    def test_stale_source_fingerprint_or_plan_token_blocks_publication(self) -> None:
        source = self.make_media(payload=b"diagnosed")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        plan = recovered_channels.build_organization_plan(case_id)

        source.write_bytes(b"changed-after-review")
        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                actor="tester",
                background=False,
            )
        self.assertFalse(
            (self.target_dir / "The Oversphere [Recovered]" / f"{VIDEO_ID}.mp4").exists()
        )

        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.start_organization(
                case_id,
                "0" * 64,
                background=False,
            )

    def test_source_replacement_during_link_publication_rolls_back_everything(self) -> None:
        source = self.make_media(payload=b"checksum-bound-original")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        original_symlink = os.symlink
        displaced = source.with_name(f"{source.name}.displaced")
        race_fired = False

        def replace_source_then_link(target, link_name, *args, **kwargs):
            nonlocal race_fired
            if not race_fired:
                race_fired = True
                source.rename(displaced)
                source.write_bytes(b"uninspected-replacement")
            return original_symlink(target, link_name, *args, **kwargs)

        with patch.object(
            recovered_channels.os,
            "symlink",
            side_effect=replace_source_then_link,
        ):
            _plan, job = self.organize(case_id)

        destination = (
            self.target_dir
            / "The Oversphere [Recovered]"
            / f"{VIDEO_ID}.mp4"
        )
        self.assertTrue(race_fired)
        self.assertEqual(displaced.read_bytes(), b"checksum-bound-original")
        self.assertEqual(source.read_bytes(), b"uninspected-replacement")
        self.assertIn(job["status"], {"failed", "partial"})
        self.assertFalse(os.path.lexists(destination))
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            persisted_item = connection.execute(
                "SELECT organized_path, error FROM recovered_channel_items WHERE item_id = ?",
                (self.item_id(item),),
            ).fetchone()
        self.assertIsNone(owner)
        self.assertIsNone(persisted_item["organized_path"])
        self.assertTrue(persisted_item["error"])

    def test_target_root_swap_cannot_redirect_recovered_link_publication(self) -> None:
        source = self.make_media(payload=b"checksum-bound-original")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        original_symlink = os.symlink
        displaced_root = self.root / "displaced-target-root"
        attacker_root = self.root / "attacker-target-root"
        attacker_root.mkdir()
        # Make the redirected pathname fully writable so a defense cannot
        # pass merely because os.symlink encounters a missing parent after
        # the root swap.
        (attacker_root / "The Oversphere [Recovered]").mkdir()
        race_fired = False

        def swap_target_root_then_link(target, link_name, *args, **kwargs):
            nonlocal race_fired
            if not race_fired:
                race_fired = True
                self.target_dir.rename(displaced_root)
                original_symlink(
                    attacker_root,
                    self.target_dir,
                    target_is_directory=True,
                )
            return original_symlink(target, link_name, *args, **kwargs)

        with patch.object(
            recovered_channels.os,
            "symlink",
            side_effect=swap_target_root_then_link,
        ):
            _plan, job = self.organize(case_id)

        self.assertTrue(race_fired)
        self.assertIn(job["status"], {"failed", "partial"})
        self.assertEqual(
            [entry for entry in attacker_root.rglob("*") if entry.is_symlink()],
            [],
            "a swapped configured root must never receive the recovered link",
        )
        self.assertEqual(
            [entry for entry in displaced_root.rglob("*") if entry.is_symlink()],
            [],
            "a failed publication must not leave an untracked link behind",
        )
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            persisted_item = connection.execute(
                "SELECT organized_path, error FROM recovered_channel_items WHERE item_id = ?",
                (self.item_id(item),),
            ).fetchone()
        self.assertIsNone(owner)
        self.assertIsNone(persisted_item["organized_path"])
        self.assertTrue(persisted_item["error"])
        self.assertEqual(self.source_signature(source), before)

    def test_missing_configured_target_root_is_never_created_by_plan_or_organization(self) -> None:
        source = self.make_media(payload=b"checksum-bound-original")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        stale_ready_plan = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(stale_ready_plan["ready_count"], 1)
        self.target_dir.rmdir()

        missing_root_plan = recovered_channels.build_organization_plan(case_id)

        self.assertEqual(missing_root_plan["ready_count"], 0, missing_root_plan)
        self.assertTrue(missing_root_plan["conflicts"])
        self.assertFalse(
            self.target_dir.exists(),
            "planning must not mask a mount outage by creating TARGET_DIR",
        )
        with self.assertRaises(recovered_channels.RecoveredChannelError) as raised:
            recovered_channels.start_organization(
                case_id,
                stale_ready_plan["plan_token"],
                actor="tester",
                background=False,
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(self.target_dir.exists())
        with db.get_db() as connection:
            jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()["count"]
            owner = connection.execute(
                "SELECT 1 FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
        self.assertEqual(jobs, 0)
        self.assertIsNone(owner)
        self.assertEqual(self.source_signature(source), before)

    def test_target_channel_folder_swap_cannot_redirect_link_publication(self) -> None:
        source = self.make_media(payload=b"checksum-bound-original")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        target_folder = self.target_dir / "The Oversphere [Recovered]"
        displaced_folder = self.root / "displaced-channel-folder"
        attacker_folder = self.root / "attacker-channel-folder"
        attacker_folder.mkdir()
        original_symlink = os.symlink
        race_fired = False

        def swap_channel_folder_then_link(target, link_name, *args, **kwargs):
            nonlocal race_fired
            if not race_fired:
                race_fired = True
                target_folder.rename(displaced_folder)
                original_symlink(
                    attacker_folder,
                    target_folder,
                    target_is_directory=True,
                )
            return original_symlink(target, link_name, *args, **kwargs)

        with patch.object(
            recovered_channels.os,
            "symlink",
            side_effect=swap_channel_folder_then_link,
        ):
            _plan, job = self.organize(case_id)

        self.assertTrue(race_fired)
        self.assertIn(job["status"], {"failed", "partial"})
        self.assertEqual(
            [entry for entry in attacker_folder.rglob("*") if entry.is_symlink()],
            [],
            "a swapped channel directory must never receive the recovered link",
        )
        self.assertEqual(
            [entry for entry in displaced_folder.rglob("*") if entry.is_symlink()],
            [],
            "a rejected publication must not leave a link in the displaced folder",
        )
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            persisted_item = connection.execute(
                "SELECT organized_path, error FROM recovered_channel_items WHERE item_id = ?",
                (self.item_id(item),),
            ).fetchone()
        self.assertIsNone(owner)
        self.assertIsNone(persisted_item["organized_path"])
        self.assertTrue(persisted_item["error"])
        self.assertEqual(self.source_signature(source), before)

    def test_exact_expected_crash_left_link_is_safely_adopted_on_retry(self) -> None:
        source = self.make_media(payload=b"checksum-bound-original")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        destination = (
            self.target_dir
            / "The Oversphere [Recovered]"
            / f"{VIDEO_ID}.mp4"
        )
        destination.parent.mkdir()
        expected_target = (
            self.host_source_dir
            / SOURCE_CHANNEL_ID
            / f"{VIDEO_ID}.mp4"
        )
        destination.symlink_to(expected_target)
        existing_identity = (
            os.lstat(destination).st_dev,
            os.lstat(destination).st_ino,
        )

        plan = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(plan["ready_count"], 1, plan)
        job = recovered_channels.start_organization(
            case_id,
            plan["plan_token"],
            actor="tester",
            background=False,
        )

        self.assertEqual(job["status"], "completed")
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), str(expected_target))
        self.assertEqual(
            (os.lstat(destination).st_dev, os.lstat(destination).st_ino),
            existing_identity,
            "retry should adopt the exact crash-left link without replacing it",
        )
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT symlink, status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            persisted_item = connection.execute(
                "SELECT organized_path FROM recovered_channel_items WHERE item_id = ?",
                (self.item_id(item),),
            ).fetchone()
        self.assertEqual(dict(owner), {
            "symlink": str(destination),
            "status": "recovered",
        })
        self.assertEqual(persisted_item["organized_path"], str(destination))
        self.assertEqual(self.source_signature(source), before)

    def test_plan_token_is_invalid_after_recovered_channel_rename(self) -> None:
        self.make_media(payload=b"checksum-bound-original")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        stale_plan = recovered_channels.build_organization_plan(case_id)

        renamed = recovered_channels.update_case(
            case_id,
            {"target_channel_name": "Renamed Investigation"},
            actor="tester",
        )
        self.assertEqual(renamed["target_component"], "Renamed Investigation [Recovered]")
        with self.assertRaises(recovered_channels.RecoveredChannelError) as raised:
            recovered_channels.start_organization(
                case_id,
                stale_plan["plan_token"],
                background=False,
                actor="tester",
            )
        self.assertEqual(raised.exception.status, 409)
        with db.get_db() as connection:
            jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()["count"]
        self.assertEqual(jobs, 0)

    def test_concurrent_rename_and_organization_plan_have_one_serialized_winner(self) -> None:
        self.make_media(payload=b"checksum-bound-original")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        plan = recovered_channels.build_organization_plan(case_id)

        original_build = recovered_channels.build_organization_plan
        plan_built = threading.Event()
        allow_start_to_continue = threading.Event()
        outcomes: dict[str, tuple[str, object]] = {}

        def paused_build(selected_case_id: str):
            current = original_build(selected_case_id)
            plan_built.set()
            self.assertTrue(
                allow_start_to_continue.wait(timeout=3),
                "organization start was never released",
            )
            return current

        def organize() -> None:
            try:
                outcomes["organize"] = (
                    "success",
                    recovered_channels.start_organization(
                        case_id,
                        plan["plan_token"],
                        actor="organization contender",
                        background=True,
                    ),
                )
            except Exception as exc:  # capture unexpected storage failures too
                outcomes["organize"] = ("error", exc)

        def rename() -> None:
            try:
                outcomes["rename"] = (
                    "success",
                    recovered_channels.update_case(
                        case_id,
                        {"target_channel_name": "Concurrent Rename"},
                        actor="rename contender",
                    ),
                )
            except Exception as exc:  # capture unexpected storage failures too
                outcomes["rename"] = ("error", exc)

        with (
            patch.object(recovered_channels, "build_organization_plan", paused_build),
            patch.object(recovered_channels, "_launch"),
        ):
            organization_thread = threading.Thread(target=organize)
            organization_thread.start()
            self.assertTrue(plan_built.wait(timeout=3), "plan was never built")
            rename_thread = threading.Thread(target=rename)
            rename_thread.start()
            # An implementation that serializes on a lock may legitimately
            # leave rename waiting. An unsafe one normally commits the rename
            # here, before the stale plan is allowed to enqueue.
            rename_thread.join(timeout=0.15)
            allow_start_to_continue.set()
            organization_thread.join(timeout=3)
            rename_thread.join(timeout=3)

        self.assertFalse(organization_thread.is_alive())
        self.assertFalse(rename_thread.is_alive())
        self.assertEqual(set(outcomes), {"organize", "rename"}, outcomes)
        winners = [name for name, outcome in outcomes.items() if outcome[0] == "success"]
        self.assertEqual(
            len(winners),
            1,
            f"rename and stale-plan enqueue must not both commit: {outcomes}",
        )
        loser = "rename" if winners[0] == "organize" else "organize"
        self.assertIsInstance(outcomes[loser][1], recovered_channels.RecoveredChannelError)
        self.assertEqual(outcomes[loser][1].status, 409)

        with db.get_db() as connection:
            persisted_case = connection.execute(
                "SELECT target_component FROM recovered_channel_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            jobs = connection.execute(
                "SELECT payload_json FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchall()
        if winners[0] == "rename":
            self.assertEqual(
                persisted_case["target_component"],
                "Concurrent Rename [Recovered]",
            )
            self.assertEqual(jobs, [])
        else:
            self.assertEqual(
                persisted_case["target_component"],
                "The Oversphere [Recovered]",
            )
            self.assertEqual(len(jobs), 1)

    def test_partial_publication_is_retryable_and_success_is_idempotent(self) -> None:
        first = self.make_media(VIDEO_ID, payload=b"first")
        second = self.make_media(SECOND_VIDEO_ID, suffix=".mkv", payload=b"second")
        case = self.create_case()
        case_id = self.case_id(case)
        items = self.items(case_id)
        for item in items:
            self.seed_completed_inspection(item)
        self.approve(case_id, *items)

        destination_dir = self.target_dir / "The Oversphere [Recovered]"
        destination_dir.mkdir()
        blocked = destination_dir / f"{SECOND_VIDEO_ID}.mkv"
        blocked.write_bytes(b"foreign")

        _plan, first_job = self.organize(case_id)

        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")
        self.assertTrue((destination_dir / f"{VIDEO_ID}.mp4").is_symlink())
        self.assertEqual(blocked.read_bytes(), b"foreign")
        self.assertIn(first_job["status"], {"partial", "failed"})

        blocked.unlink()
        _plan, retry_job = self.organize(case_id)
        self.assertIn(retry_job["status"], {"completed", "partial"})
        self.assertTrue((destination_dir / f"{SECOND_VIDEO_ID}.mkv").is_symlink())
        self.assertEqual(
            len(list(destination_dir.iterdir())),
            2,
            "retry must not duplicate an already-published link",
        )

    def test_duplicate_exact_video_ids_require_one_human_choice(self) -> None:
        self.make_media(VIDEO_ID, suffix=".mp4", payload=b"copy-one")
        self.make_media(
            VIDEO_ID,
            suffix=".mkv",
            filename=f"Alternate [{VIDEO_ID}].mkv",
            payload=b"copy-two",
        )
        case = self.create_case()
        case_id = self.case_id(case)
        items = self.items(case_id)
        self.assertEqual(len(items), 2)
        for item in items:
            self.seed_completed_inspection(item)

        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.bulk_decision(
                case_id,
                [self.item_id(item) for item in items],
                "approved",
                actor="tester",
            )

    def test_unindexed_suppression_requires_a_current_successfully_owned_link(self) -> None:
        self.make_media(payload=b"source-remains-in-place")
        hidden_dir = self.hidden_dir
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]

        scan_patches = (
            patch.object(recovery, "SOURCE_DIR", self.source_dir),
            patch.object(recovery, "TARGET_DIR", self.target_dir),
            patch.object(recovery, "HIDDEN_DIR", hidden_dir),
            patch.object(recovery, "fetch_all_metadata", return_value={}),
        )

        for patcher in scan_patches:
            patcher.start()
        try:
            before = recovery.scan_for_unindexed_videos()
            self.assertEqual(
                {entry["video_id"] for entry in before["unindexed"]},
                {VIDEO_ID},
            )

            self.seed_completed_inspection(item)
            self.approve(case_id, item)
            self.organize(case_id)
            after = recovery.scan_for_unindexed_videos()
            self.assertNotIn(
                VIDEO_ID,
                {entry["video_id"] for entry in after["unindexed"]},
            )

            owned_link = (
                self.target_dir
                / "The Oversphere [Recovered]"
                / f"{VIDEO_ID}.mp4"
            )
            self.assertTrue(owned_link.is_symlink())
            owned_link.unlink()
            missing_link = recovery.scan_for_unindexed_videos()
            self.assertIn(
                VIDEO_ID,
                {entry["video_id"] for entry in missing_link["unindexed"]},
            )
        finally:
            for patcher in reversed(scan_patches):
                patcher.stop()

    def test_unindexed_suppression_rejects_outside_or_symlink_parent_ownership(self) -> None:
        source = self.make_media(payload=b"source-remains-in-place")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        self.organize(case_id)
        owned_link = (
            self.target_dir
            / "The Oversphere [Recovered]"
            / f"{VIDEO_ID}.mp4"
        )
        expected_target = os.readlink(owned_link)
        item_id = self.item_id(item)

        outside_dir = self.root / "outside-managed-roots"
        outside_dir.mkdir()
        outside_link = outside_dir / owned_link.name
        outside_link.symlink_to(expected_target)
        with db.get_db() as connection:
            connection.execute(
                "UPDATE recovered_channel_items SET organized_path = ? WHERE item_id = ?",
                (str(outside_link), item_id),
            )
            connection.execute(
                "UPDATE videos SET symlink = ? WHERE video_id = ?",
                (str(outside_link), VIDEO_ID),
            )
            connection.commit()
        self.assertNotIn(
            VIDEO_ID,
            recovered_channels.organized_recovered_video_ids(),
            "a tampered outside DB path must never suppress Unindexed",
        )

        with db.get_db() as connection:
            connection.execute(
                "UPDATE recovered_channel_items SET organized_path = ? WHERE item_id = ?",
                (str(owned_link), item_id),
            )
            connection.execute(
                "UPDATE videos SET symlink = ? WHERE video_id = ?",
                (str(owned_link), VIDEO_ID),
            )
            connection.commit()
        component = owned_link.parent
        displaced = self.root / "displaced-owned-component"
        attacker = self.root / "attacker-owned-component"
        attacker.mkdir()
        component.rename(displaced)
        component.symlink_to(attacker, target_is_directory=True)
        (attacker / owned_link.name).symlink_to(expected_target)
        self.assertNotIn(
            VIDEO_ID,
            recovered_channels.organized_recovered_video_ids(),
            "an expected-looking link below a symlinked component is not owned",
        )
        self.assertEqual(source.read_bytes(), b"source-remains-in-place")

    def test_likely_scope_is_case_wide_and_leaves_review_and_holds_for_later(self) -> None:
        sources = {
            VIDEO_ID: self.make_media(VIDEO_ID, payload=b"clear-source"),
            SECOND_VIDEO_ID: self.make_media(
                SECOND_VIDEO_ID, suffix=".mkv", payload=b"minimal-source"
            ),
            "thirdvideo1": self.make_media(
                "thirdvideo1", payload=b"serious-source"
            ),
            "fourthvid02": self.make_media(
                "fourthvid02", payload=b"held-source"
            ),
        }
        before = {
            video_id: self.source_signature(path)
            for video_id, path in sources.items()
        }
        case = self.create_case()
        case_id = self.case_id(case)
        items = {item["video_id"]: item for item in self.items(case_id)}
        self.seed_completed_inspection(items[VIDEO_ID], health_tier="clear")
        self.seed_completed_inspection(
            items[SECOND_VIDEO_ID], health_tier="minimal"
        )
        self.seed_completed_inspection(
            items["thirdvideo1"], health_tier="serious"
        )
        self.seed_completed_inspection(
            items["fourthvid02"], health_tier="clear"
        )
        self.approve(case_id, items["thirdvideo1"])
        recovered_channels.bulk_decision(
            case_id,
            [self.item_id(items["fourthvid02"])],
            "hold",
            actor="tester",
        )

        first_page = recovered_channels.list_items(
            case_id, page=1, per_page=1
        )
        self.assertEqual(len(first_page["items"]), 1)
        default_plan = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(default_plan["scope"], "approved")
        self.assertEqual(
            {item["video_id"] for item in default_plan["items"]},
            {"thirdvideo1"},
            "the default scope must retain the prior approved behavior",
        )

        plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )

        self.assertEqual(plan["scope"], "likely_usable")
        self.assertEqual(plan["selected_count"], 2)
        self.assertEqual(plan["ready_count"], 2)
        self.assertEqual(plan["conflict_count"], 0)
        self.assertEqual(plan["excluded_needs_review_count"], 1)
        self.assertEqual(plan["held_count"], 1)
        self.assertEqual(plan["remaining_count"], 2)
        self.assertEqual(plan["summary"]["remaining_count"], 2)
        self.assertEqual(
            {item["video_id"] for item in plan["items"]},
            {VIDEO_ID, SECOND_VIDEO_ID},
            "scope selection must not depend on the currently loaded page",
        )
        self.assertTrue(
            all(item["decision"] == "pending" for item in plan["items"]),
            "dry-run planning must not auto-approve pending items",
        )
        self.assertTrue(
            all(
                item["health_tier"] in {"clear", "minimal"}
                and item["scope"] == "likely_usable"
                for item in plan["items"]
            )
        )

        job = recovered_channels.start_organization(
            case_id,
            plan["plan_token"],
            actor="tester",
            background=False,
            scope="likely_usable",
        )

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["scope"], "likely_usable")
        refreshed = {
            item["video_id"]: item for item in self.items(case_id)
        }
        for video_id in (VIDEO_ID, SECOND_VIDEO_ID):
            self.assertEqual(refreshed[video_id]["decision"], "approved")
            self.assertTrue(Path(refreshed[video_id]["organized_path"]).is_symlink())
        self.assertEqual(refreshed["thirdvideo1"]["decision"], "approved")
        self.assertIsNone(refreshed["thirdvideo1"]["organized_path"])
        self.assertEqual(refreshed["fourthvid02"]["decision"], "hold")
        self.assertIsNone(refreshed["fourthvid02"]["organized_path"])
        investigation = recovered_channels.get_case(case_id)
        self.assertEqual(investigation["case"]["status"], "review")
        self.assertEqual(investigation["counts"]["remaining"], 2)
        for video_id, path in sources.items():
            self.assertEqual(self.source_signature(path), before[video_id])

    def test_likely_scope_blocks_nonheld_duplicates_and_hold_resolves_one(self) -> None:
        source_specs = (
            (VIDEO_ID, ".mp4", b"clear-a"),
            (VIDEO_ID, ".mkv", b"clear-b"),
            (SECOND_VIDEO_ID, ".mp4", b"mixed-clear"),
            (SECOND_VIDEO_ID, ".mkv", b"mixed-serious-approved"),
            ("thirdvideo1", ".mp4", b"held-clear"),
            ("thirdvideo1", ".mkv", b"held-serious"),
        )
        sources = [
            self.make_media(video_id, suffix=suffix, payload=payload)
            for video_id, suffix, payload in source_specs
        ]
        before = [self.source_signature(path) for path in sources]
        case = self.create_case()
        case_id = self.case_id(case)
        items = self.items(case_id)
        by_key = {
            (item["video_id"], item["suffix"]): item for item in items
        }
        for item in items:
            health = (
                "serious"
                if (item["video_id"], item["suffix"])
                in {
                    (SECOND_VIDEO_ID, ".mkv"),
                    ("thirdvideo1", ".mkv"),
                }
                else "clear"
            )
            self.seed_completed_inspection(item, health_tier=health)
        self.approve(case_id, by_key[(SECOND_VIDEO_ID, ".mkv")])
        recovered_channels.bulk_decision(
            case_id,
            [self.item_id(by_key[("thirdvideo1", ".mkv")])],
            "hold",
            actor="tester",
        )

        plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )

        self.assertEqual(plan["selected_count"], 4)
        self.assertEqual(plan["ready_count"], 1)
        self.assertEqual(plan["conflict_count"], 3)
        self.assertEqual(plan["held_count"], 1)
        self.assertEqual(plan["excluded_needs_review_count"], 1)
        self.assertEqual(plan["remaining_count"], 5)
        self.assertEqual(
            {item["video_id"] for item in plan["conflicts"]},
            {VIDEO_ID, SECOND_VIDEO_ID},
        )
        self.assertEqual(
            [item["video_id"] for item in plan["items"] if item["ready"]],
            ["thirdvideo1"],
            "a held sibling is the only duplicate state that disambiguates",
        )

        job = recovered_channels.start_organization(
            case_id,
            plan["plan_token"],
            actor="tester",
            background=False,
            scope="likely_usable",
        )

        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["success_count"], 1)
        self.assertEqual(job["fail_count"], 3)
        refreshed = self.items(case_id)
        published = [item for item in refreshed if item["organized_path"]]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["video_id"], "thirdvideo1")
        self.assertEqual(published[0]["decision"], "approved")
        serious_held = next(
            item
            for item in refreshed
            if item["video_id"] == "thirdvideo1" and item["suffix"] == ".mkv"
        )
        self.assertEqual(serious_held["decision"], "hold")
        serious_approved = next(
            item
            for item in refreshed
            if item["video_id"] == SECOND_VIDEO_ID and item["suffix"] == ".mkv"
        )
        self.assertEqual(serious_approved["decision"], "approved")
        self.assertIsNone(serious_approved["organized_path"])
        self.assertEqual(
            recovered_channels.get_case(case_id)["counts"]["remaining"], 5
        )
        for path, signature in zip(sources, before):
            self.assertEqual(self.source_signature(path), signature)

    def test_likely_scope_token_binds_decision_and_expected_duration(self) -> None:
        self.make_media(payload=b"likely-token-source")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item, health_tier="clear")
        plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )

        recovered_channels.update_item(
            case_id,
            self.item_id(item),
            {"decision": "hold"},
            actor="tester",
        )
        with self.assertRaises(recovered_channels.RecoveredChannelError) as held:
            recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                background=False,
                scope="likely_usable",
            )
        self.assertEqual(held.exception.status, 409)

        recovered_channels.update_item(
            case_id,
            self.item_id(item),
            {"decision": "pending"},
            actor="tester",
        )
        duration_plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )
        with db.get_db() as connection:
            connection.execute(
                "UPDATE recovered_channel_items SET expected_duration_seconds = 90 "
                "WHERE item_id = ?",
                (self.item_id(item),),
            )
            connection.commit()
        with self.assertRaises(recovered_channels.RecoveredChannelError) as changed:
            recovered_channels.start_organization(
                case_id,
                duration_plan["plan_token"],
                background=False,
                scope="likely_usable",
            )
        self.assertEqual(changed.exception.status, 409)
        self.assertFalse(
            (self.target_dir / "The Oversphere [Recovered]").exists()
        )

    def test_explicit_null_organization_scope_is_never_defaulted(self) -> None:
        self.make_media(payload=b"null-scope-source")
        case = self.create_case()
        case_id = self.case_id(case)
        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.build_organization_plan(case_id, scope=None)
        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.start_organization(
                case_id,
                "0" * 64,
                background=False,
                scope=None,
            )

    def test_likely_worker_rechecks_hold_and_does_not_approve_failed_item(self) -> None:
        source = self.make_media(payload=b"late-held-source")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item, health_tier="minimal")
        plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )
        with patch.object(recovered_channels, "_launch"):
            job = recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                actor="tester",
                background=True,
                scope="likely_usable",
            )
        with db.get_db() as connection:
            connection.execute(
                "UPDATE recovered_channel_items SET decision = 'hold' "
                "WHERE item_id = ?",
                (self.item_id(item),),
            )
            connection.commit()

        recovered_channels.run_organization_job(job["job_id"])

        result = recovered_channels.get_job(case_id, job["job_id"])
        self.assertEqual(result["status"], "failed")
        persisted = self.items(case_id)[0]
        self.assertEqual(persisted["decision"], "hold")
        self.assertIsNone(persisted["organized_path"])
        self.assertEqual(self.source_signature(source), before)
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT 1 FROM videos WHERE video_id = ?", (VIDEO_ID,)
            ).fetchone()
        self.assertIsNone(owner)

    def test_failed_likely_publication_keeps_pending_decision(self) -> None:
        source = self.make_media(payload=b"pending-source-before-drift")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item, health_tier="clear")
        plan = recovered_channels.build_organization_plan(
            case_id, scope="likely_usable"
        )
        with patch.object(recovered_channels, "_launch"):
            job = recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                actor="tester",
                background=True,
                scope="likely_usable",
            )
        source.write_bytes(b"source-drift-after-job-start")

        recovered_channels.run_organization_job(job["job_id"])

        result = recovered_channels.get_job(case_id, job["job_id"])
        self.assertEqual(result["status"], "failed")
        persisted = self.items(case_id)[0]
        self.assertEqual(persisted["decision"], "pending")
        self.assertIsNone(persisted["organized_path"])
        with db.get_db() as connection:
            owner = connection.execute(
                "SELECT 1 FROM videos WHERE video_id = ?", (VIDEO_ID,)
            ).fetchone()
        self.assertIsNone(owner)


class RecoveredChannelIntegrationTests(RecoveredChannelTestCase):
    def publish_one(self) -> tuple[Path, dict, dict, Path]:
        source = self.make_media(payload=b"immutable recovered archive")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        _plan, job = self.organize(case_id)
        self.assertIn(job["status"], {"completed", "partial"})
        destination = (
            self.target_dir
            / "The Oversphere [Recovered]"
            / f"{VIDEO_ID}.mp4"
        )
        self.assertTrue(destination.is_symlink())
        return source, case, item, destination

    def recovered_rows(self, item_id: str) -> tuple[dict, dict]:
        with db.get_db() as connection:
            video = connection.execute(
                "SELECT video_id, symlink, status FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            item = connection.execute(
                "SELECT item_id, source_path, organized_path FROM recovered_channel_items "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        self.assertIsNotNone(video)
        self.assertIsNotNone(item)
        return dict(video), dict(item)

    def test_hidden_public_migration_updates_recovered_paths_without_touching_source(self) -> None:
        source, _case, original_item, target_link = self.publish_one()
        before = self.source_signature(source)
        component = "The Oversphere [Recovered]"

        symlinks._enforce_hidden_locations({component})

        hidden_link = self.hidden_dir / component / target_link.name
        self.assertTrue(hidden_link.is_symlink())
        self.assertFalse(os.path.lexists(target_link))
        video, item = self.recovered_rows(self.item_id(original_item))
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(video["symlink"], str(hidden_link))
        self.assertEqual(item["organized_path"], str(hidden_link))
        self.assertEqual(self.source_signature(source), before)

        symlinks._enforce_hidden_locations(set())

        self.assertTrue(target_link.is_symlink())
        self.assertFalse(os.path.lexists(hidden_link))
        video, item = self.recovered_rows(self.item_id(original_item))
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(video["symlink"], str(target_link))
        self.assertEqual(item["organized_path"], str(target_link))
        self.assertEqual(self.source_signature(source), before)

    def test_normal_metadata_scan_never_supersedes_a_recovered_owner(self) -> None:
        source, _case, original_item, recovered_link = self.publish_one()
        before = self.source_signature(source)
        original_target = os.readlink(recovered_link)
        metadata = {
            VIDEO_ID: {
                "channel_name": "Later TubeArchivist Channel",
                "published": "2026-07-16",
                "title": "A newly indexed title",
                "is_live": False,
            }
        }

        with patch.object(symlinks, "fetch_all_metadata", return_value=metadata):
            result = symlinks._process_videos_unlocked()

        self.assertIsNone(result)
        self.assertTrue(recovered_link.is_symlink())
        self.assertEqual(os.readlink(recovered_link), original_target)
        self.assertEqual(
            [entry for entry in self.target_dir.rglob("*") if entry.is_symlink()],
            [recovered_link],
            "a normal scan must not create a second owner or retire recovered history",
        )
        video, item = self.recovered_rows(self.item_id(original_item))
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(video["symlink"], str(recovered_link))
        self.assertEqual(item["organized_path"], str(recovered_link))
        self.assertEqual(self.source_signature(source), before)

    def test_orphan_check_reports_but_never_downgrades_recovered_ownership(self) -> None:
        source, _case, original_item, recovered_link = self.publish_one()
        before = self.source_signature(source)
        original_target = os.readlink(recovered_link)
        recovered_link.unlink()
        recovered_link.symlink_to(
            self.host_source_dir
            / "temporarily-unavailable"
            / f"{VIDEO_ID}.mp4"
        )

        orphaned = symlinks.check_orphaned_links()

        self.assertEqual([entry["video_id"] for entry in orphaned], [VIDEO_ID])
        video, item = self.recovered_rows(self.item_id(original_item))
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(video["symlink"], str(recovered_link))
        self.assertEqual(item["organized_path"], str(recovered_link))
        self.assertEqual(self.source_signature(source), before)

        recovered_link.unlink()
        recovered_link.symlink_to(original_target)
        self.assertIn(VIDEO_ID, recovered_channels.organized_recovered_video_ids())

    def test_reconciliation_recovered_rows_offer_preview_but_no_generic_mutations(self) -> None:
        source, _case, original_item, recovered_link = self.publish_one()
        before = self.source_signature(source)
        empty_recovery = {
            "unindexed": [],
            "lost": [],
            "rescue": [],
            "redundant": [],
        }

        with (
            patch.object(reconciliation, "probe_media", return_value=self.valid_probe()),
            patch.object(
                reconciliation,
                "scan_for_unindexed_videos",
                return_value=empty_recovery,
            ),
        ):
            # A broken recovered link with an exact valid source remains
            # previewable, but Repair Link belongs only to its case workflow.
            recovered_link.unlink()
            broken_target = (
                self.host_source_dir
                / "unavailable"
                / f"{VIDEO_ID}.mp4"
            )
            recovered_link.symlink_to(broken_target)
            reconciliation._clear_probe_cache()
            inventory = reconciliation.build_inventory(lambda: empty_recovery)
            entry = next(
                item
                for item in inventory["entries"]
                if item["video_id"] == VIDEO_ID
                and item["link_path"] == str(recovered_link)
            )
            self.assertEqual(entry["status"], "repair_available")
            self.assertIn("preview_candidate", entry["allowed_actions"])
            self.assertNotIn("repair_link", entry["allowed_actions"])
            repair_payload = {
                "video_id": VIDEO_ID,
                "link_path": str(recovered_link),
                "stored_target_path": str(broken_target),
                "candidate_path": str(source),
                "scan_token": inventory["scan_token"],
            }
            with self.assertRaises(reconciliation.ReconciliationError):
                reconciliation.repair_link(repair_payload)
            self.assertTrue(recovered_link.is_symlink())
            self.assertEqual(os.readlink(recovered_link), str(broken_target))

            # Replacing the entry with a real media file must not make a
            # recovered item eligible for generic adoption.
            recovered_link.unlink()
            recovered_link.write_bytes(b"operator supplied managed copy")
            reconciliation._clear_probe_cache()
            inventory = reconciliation.build_inventory(lambda: empty_recovery)
            entry = next(
                item
                for item in inventory["entries"]
                if item["video_id"] == VIDEO_ID
                and item["link_path"] == str(recovered_link)
            )
            self.assertEqual(entry["status"], "real_file_expected_symlink")
            self.assertIn("preview_candidate", entry["allowed_actions"])
            self.assertNotIn("adopt_existing_real_file", entry["allowed_actions"])
            with self.assertRaises(reconciliation.ReconciliationError):
                reconciliation.adopt_existing_real_file({
                    "video_id": VIDEO_ID,
                    "link_path": str(recovered_link),
                    "scan_token": inventory["scan_token"],
                })
            self.assertEqual(recovered_link.read_bytes(), b"operator supplied managed copy")

            # Even a synthetic Truly Missing view cannot remove a broken link
            # or forget the durable recovered item/row.
            recovered_link.unlink()
            recovered_link.symlink_to(broken_target)
            with patch.object(reconciliation, "_walk_real_source_files", return_value=[]):
                reconciliation._clear_probe_cache()
                inventory = reconciliation.build_inventory(lambda: empty_recovery)
                entry = next(
                    item
                    for item in inventory["entries"]
                    if item["video_id"] == VIDEO_ID
                    and item["link_path"] == str(recovered_link)
                )
                self.assertEqual(entry["status"], "truly_missing")
                self.assertNotIn("remove_broken_link", entry["allowed_actions"])
                remove_payload = {
                    "video_id": VIDEO_ID,
                    "link_path": str(recovered_link),
                    "stored_target_path": str(broken_target),
                    "scan_token": inventory["scan_token"],
                }
                with self.assertRaises(reconciliation.ReconciliationError):
                    reconciliation.remove_broken_link(remove_payload)
                self.assertTrue(recovered_link.is_symlink())

                recovered_link.unlink()
                inventory = reconciliation.build_inventory(lambda: empty_recovery)
                entry = next(
                    item
                    for item in inventory["entries"]
                    if item["video_id"] == VIDEO_ID
                    and item["link_path"] == str(recovered_link)
                )
                self.assertEqual(entry["status"], "truly_missing")
                self.assertNotIn("forget_stale_record", entry["allowed_actions"])
                with self.assertRaises(reconciliation.ReconciliationError):
                    reconciliation.forget_stale_record({
                        "video_id": VIDEO_ID,
                        "link_path": str(recovered_link),
                        "scan_token": inventory["scan_token"],
                    })

        video, item = self.recovered_rows(self.item_id(original_item))
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(video["symlink"], str(recovered_link))
        self.assertEqual(item["organized_path"], str(recovered_link))
        self.assertEqual(self.source_signature(source), before)

    def test_reconciliation_never_uses_an_alternate_exact_id_for_recovered_repair(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        before = self.source_signature(source)
        alternate = self.make_media(
            VIDEO_ID,
            channel_id=OTHER_CHANNEL_ID,
            filename=f"Alternate [{VIDEO_ID}].mp4",
            payload=b"alternate exact-id bytes",
        )
        alternate_before = self.source_signature(alternate)
        recovered_link.unlink()
        empty_recovery = {
            "unindexed": [],
            "lost": [],
            "rescue": [],
            "redundant": [],
        }

        with (
            patch.object(reconciliation, "probe_media", return_value=self.valid_probe()),
            patch.object(
                reconciliation,
                "scan_for_unindexed_videos",
                return_value=empty_recovery,
            ),
        ):
            reconciliation._clear_probe_cache()
            inventory = reconciliation.build_inventory(lambda: empty_recovery)
            entry = next(
                item
                for item in inventory["entries"]
                if item["video_id"] == VIDEO_ID
                and item["link_path"] == str(recovered_link)
            )
            self.assertGreaterEqual(len(entry["candidates"]), 2)
            self.assertNotIn("repair_link", entry["allowed_actions"])
            with self.assertRaises(reconciliation.ReconciliationError):
                reconciliation.repair_link({
                    "video_id": VIDEO_ID,
                    "link_path": str(recovered_link),
                    "stored_target_path": None,
                    "candidate_path": str(alternate),
                    "scan_token": inventory["scan_token"],
                })

        self.assertFalse(os.path.lexists(recovered_link))
        # The case-specific action has no candidate parameter and continues to
        # recover exclusively from its durable original despite the alternate.
        repaired = recovered_channels.repair_recovered_link(
            self.case_id(case),
            self.item_id(original_item),
            actor="tester",
        )
        self.assertTrue(repaired["link_repaired"])
        self.assertEqual(self.source_signature(source), before)
        self.assertEqual(self.source_signature(alternate), alternate_before)

    def test_missing_recovered_link_repairs_from_only_durable_source_and_keeps_history(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        item_id = self.item_id(original_item)
        before = self.source_signature(source)
        expected_target = os.readlink(recovered_link)
        with db.get_db() as connection:
            before_item = connection.execute(
                "SELECT organized_path, organized_at FROM recovered_channel_items "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        recovered_link.unlink()

        listed = self.items(case_id)[0]
        self.assertEqual(listed["link_status"], "missing")
        self.assertTrue(listed["repairable_link"])
        self.assertEqual(recovered_channels.get_case(case_id)["counts"]["repair_needed"], 1)

        result = recovered_channels.repair_recovered_link(
            case_id,
            item_id,
            actor="repair tester",
        )

        self.assertTrue(result["link_repaired"])
        self.assertEqual(result["item"]["link_status"], "healthy")
        self.assertFalse(result["item"]["repairable_link"])
        self.assertTrue(recovered_link.is_symlink())
        self.assertEqual(os.readlink(recovered_link), expected_target)
        self.assertEqual(recovered_channels.get_case(case_id)["counts"]["repair_needed"], 0)
        with db.get_db() as connection:
            video = connection.execute(
                "SELECT status, symlink FROM videos WHERE video_id = ?",
                (VIDEO_ID,),
            ).fetchone()
            after_item = connection.execute(
                "SELECT organized_path, organized_at FROM recovered_channel_items "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            event = connection.execute(
                "SELECT actor, details_json FROM recovered_channel_events "
                "WHERE case_id = ? AND item_id = ? AND event = 'recovered_link_repaired' "
                "ORDER BY event_id DESC LIMIT 1",
                (case_id, item_id),
            ).fetchone()
        self.assertEqual(dict(video), {"status": "recovered", "symlink": str(recovered_link)})
        self.assertEqual(tuple(after_item), tuple(before_item))
        self.assertIsNotNone(event)
        self.assertEqual(event["actor"], "repair tester")
        self.assertEqual(json.loads(event["details_json"])["sha256"], self.seed_sha(source))
        self.assertEqual(self.source_signature(source), before)

    @staticmethod
    def seed_sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_repair_refuses_real_file_and_foreign_symlink_without_touching_either(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        item_id = self.item_id(original_item)
        before = self.source_signature(source)

        recovered_link.unlink()
        recovered_link.write_bytes(b"operator real media")
        with self.assertRaises(recovered_channels.RecoveredChannelError) as real_error:
            recovered_channels.repair_recovered_link(case_id, item_id, actor="tester")
        self.assertEqual(real_error.exception.status, 409)
        self.assertEqual(recovered_link.read_bytes(), b"operator real media")
        self.assertEqual(self.items(case_id)[0]["link_status"], "unexpected_real_file")
        self.assertFalse(self.items(case_id)[0]["repairable_link"])

        recovered_link.unlink()
        foreign_target = self.root / "foreign" / f"{VIDEO_ID}.mp4"
        recovered_link.symlink_to(foreign_target)
        with self.assertRaises(recovered_channels.RecoveredChannelError) as link_error:
            recovered_channels.repair_recovered_link(case_id, item_id, actor="tester")
        self.assertEqual(link_error.exception.status, 409)
        self.assertEqual(os.readlink(recovered_link), str(foreign_target))
        self.assertEqual(self.items(case_id)[0]["link_status"], "wrong_symlink")
        self.assertFalse(self.items(case_id)[0]["repairable_link"])
        self.assertEqual(self.source_signature(source), before)

    def test_repair_refuses_source_fingerprint_and_checksum_drift(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        item_id = self.item_id(original_item)
        recovered_link.unlink()
        source.write_bytes(b"changed archive bytes")

        with self.assertRaises(recovered_channels.RecoveredChannelError) as fingerprint_error:
            recovered_channels.repair_recovered_link(case_id, item_id, actor="tester")
        self.assertEqual(fingerprint_error.exception.status, 409)
        self.assertFalse(os.path.lexists(recovered_link))
        self.assertFalse(self.items(case_id)[0]["repairable_link"])

        # Even if an external/manual database edit refreshes the cheap stat
        # fingerprint, the original inspection SHA-256 remains authoritative.
        changed = os.stat(source)
        with db.get_db() as connection:
            connection.execute(
                """
                UPDATE recovered_channel_items
                SET size_bytes = ?, device = ?, inode = ?, mtime_ns = ?, ctime_ns = ?
                WHERE item_id = ?
                """,
                (
                    changed.st_size,
                    changed.st_dev,
                    changed.st_ino,
                    changed.st_mtime_ns,
                    changed.st_ctime_ns,
                    item_id,
                ),
            )
            connection.commit()
        with self.assertRaises(recovered_channels.RecoveredChannelError) as checksum_error:
            recovered_channels.repair_recovered_link(case_id, item_id, actor="tester")
        self.assertEqual(checksum_error.exception.status, 409)
        self.assertIn("sha-256", str(checksum_error.exception).lower())
        self.assertFalse(os.path.lexists(recovered_link))

    def test_missing_link_is_not_offered_when_database_owner_is_not_exact(self) -> None:
        _source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        item_id = self.item_id(original_item)
        recovered_link.unlink()
        scenarios = (
            ("missing", "DELETE FROM videos WHERE video_id = ?", (VIDEO_ID,)),
            (
                "non-recovered",
                "UPDATE videos SET status = 'linked' WHERE video_id = ?",
                (VIDEO_ID,),
            ),
            (
                "other-path",
                "UPDATE videos SET status = 'recovered', symlink = ? WHERE video_id = ?",
                (str(self.target_dir / "someone-else.mp4"), VIDEO_ID),
            ),
        )
        for label, statement, parameters in scenarios:
            with self.subTest(label=label):
                with db.get_db() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO videos "
                        "(video_id, title, channel, symlink, status, is_live, adopted_real) "
                        "VALUES (?, ?, ?, ?, 'recovered', 0, 0)",
                        (
                            VIDEO_ID,
                            VIDEO_ID,
                            "The Oversphere [Recovered]",
                            str(recovered_link),
                        ),
                    )
                    connection.execute(statement, parameters)
                    connection.commit()
                listed = self.items(case_id)[0]
                self.assertEqual(listed["link_status"], "missing")
                self.assertFalse(listed["repairable_link"])
                self.assertEqual(
                    recovered_channels.get_case(case_id)["counts"]["repair_needed"],
                    0,
                )
                with self.assertRaises(recovered_channels.RecoveredChannelError):
                    recovered_channels.repair_recovered_link(case_id, item_id, actor="tester")

    def test_expected_link_is_not_healthy_when_database_owner_is_not_exact(self) -> None:
        _source, case, _item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        scenarios = (
            ("missing", "DELETE FROM videos WHERE video_id = ?", (VIDEO_ID,)),
            (
                "non-recovered",
                "UPDATE videos SET status = 'linked' WHERE video_id = ?",
                (VIDEO_ID,),
            ),
            (
                "other-path",
                "UPDATE videos SET status = 'recovered', symlink = ? WHERE video_id = ?",
                (str(self.target_dir / "someone-else.mp4"), VIDEO_ID),
            ),
        )
        for label, statement, parameters in scenarios:
            with self.subTest(label=label):
                with db.get_db() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO videos "
                        "(video_id, title, channel, symlink, status, is_live, adopted_real) "
                        "VALUES (?, ?, ?, ?, 'recovered', 0, 0)",
                        (
                            VIDEO_ID,
                            VIDEO_ID,
                            "The Oversphere [Recovered]",
                            str(recovered_link),
                        ),
                    )
                    connection.execute(statement, parameters)
                    connection.commit()
                listed = self.items(case_id)[0]
                self.assertNotEqual(listed["link_status"], "healthy")
                self.assertFalse(listed["repairable_link"])
                self.assertEqual(
                    recovered_channels.get_case(case_id)["counts"]["repair_needed"],
                    0,
                )
        self.assertTrue(recovered_link.is_symlink())

    def test_link_health_fails_closed_when_durable_source_is_missing_or_changed(self) -> None:
        source, case, _item, recovered_link = self.publish_one()
        case_id = self.case_id(case)

        source.unlink()
        missing_source = self.items(case_id)[0]
        self.assertNotEqual(missing_source["link_status"], "healthy")
        self.assertFalse(missing_source["repairable_link"])

        source.write_bytes(b"different bytes after source returned")
        changed_source = self.items(case_id)[0]
        self.assertNotEqual(changed_source["link_status"], "healthy")
        self.assertFalse(changed_source["repairable_link"])
        self.assertTrue(recovered_link.is_symlink())

    def test_link_health_rejects_swapped_channel_folder_even_with_expected_entry(self) -> None:
        source, case, _item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        before = self.source_signature(source)
        component = self.target_dir / "The Oversphere [Recovered]"
        displaced = self.root / "displaced-recovered-folder"
        attacker = self.root / "attacker-recovered-folder"
        attacker.mkdir()
        component.rename(displaced)
        component.symlink_to(attacker, target_is_directory=True)

        empty_attacker = self.items(case_id)[0]
        self.assertEqual(empty_attacker["link_status"], "unsafe")
        self.assertFalse(empty_attacker["repairable_link"])

        (attacker / recovered_link.name).symlink_to(
            self.host_source_dir / SOURCE_CHANNEL_ID / f"{VIDEO_ID}.mp4"
        )
        forged_expected = self.items(case_id)[0]
        self.assertEqual(forged_expected["link_status"], "unsafe")
        self.assertFalse(forged_expected["repairable_link"])
        self.assertEqual(self.source_signature(source), before)

    def test_repair_recreates_a_truly_missing_exact_channel_folder(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        before = self.source_signature(source)
        component = recovered_link.parent
        recovered_link.unlink()
        component.rmdir()

        listed = self.items(case_id)[0]
        self.assertEqual(listed["link_status"], "missing")
        self.assertTrue(listed["repairable_link"])
        result = recovered_channels.repair_recovered_link(
            case_id,
            self.item_id(original_item),
            actor="tester",
        )

        self.assertTrue(result["link_repaired"])
        self.assertTrue(component.is_dir())
        self.assertFalse(component.is_symlink())
        self.assertTrue(recovered_link.is_symlink())
        self.assertEqual(self.source_signature(source), before)

    def test_repair_never_recreates_a_missing_configured_managed_root(self) -> None:
        source, case, original_item, recovered_link = self.publish_one()
        case_id = self.case_id(case)
        before = self.source_signature(source)
        component = recovered_link.parent
        recovered_link.unlink()
        component.rmdir()
        self.target_dir.rmdir()

        listed = self.items(case_id)[0]
        self.assertNotEqual(
            listed["link_status"],
            "missing",
            "a missing mount/root is not equivalent to one missing channel folder",
        )
        self.assertFalse(listed["repairable_link"])
        with self.assertRaises(recovered_channels.RecoveredChannelError):
            recovered_channels.repair_recovered_link(
                case_id,
                self.item_id(original_item),
                actor="tester",
            )
        self.assertFalse(self.target_dir.exists())
        self.assertEqual(self.source_signature(source), before)

    def test_import_stage_cannot_publish_after_source_is_claimed_by_a_case(self) -> None:
        source = self.make_media(payload=b"source claimed during import")
        initial = os.stat(source)
        import_dir = self.root / "import-race"
        import_dir.mkdir()
        original_prepare = recovery._prepare_media_stage
        stage_ready = threading.Event()
        allow_publish = threading.Event()
        staged_paths: list[Path] = []

        def prepare_then_pause(source_path: Path, destination: Path, **kwargs):
            result = original_prepare(source_path, destination, **kwargs)
            if result[0] is not None:
                staged_paths.append(result[0])
            stage_ready.set()
            if not allow_publish.wait(3):
                raise RuntimeError("test timed out before import publication")
            return result

        with (
            patch.object(recovery, "IMPORT_DIR", import_dir),
            patch.object(recovery, "_prepare_media_stage", side_effect=prepare_then_pause),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(recovery.force_import, str(source))
            self.assertTrue(stage_ready.wait(3), "import never reached its private stage")
            self.assertTrue(staged_paths and staged_paths[0].exists())
            try:
                case = self.create_case()
                claimed_state = os.stat(source)
            finally:
                allow_publish.set()
            try:
                outcome = future.result(timeout=3)
            except recovered_channels.RecoveredChannelMutationError:
                outcome = (False, "immutable recovered source")

        self.assertFalse(outcome[0], outcome)
        self.assertEqual(self.items(self.case_id(case))[0]["source_path"], str(source))
        self.assertEqual(list(import_dir.iterdir()), [])
        after = os.stat(source)
        self.assertEqual(source.read_bytes(), b"source claimed during import")
        self.assertEqual((after.st_dev, after.st_ino), (initial.st_dev, initial.st_ino))
        self.assertEqual(after.st_nlink, initial.st_nlink)
        self.assertEqual(
            (after.st_nlink, after.st_ctime_ns),
            (claimed_state.st_nlink, claimed_state.st_ctime_ns),
            "aborting a staged import after the claim must not stale the case fingerprint",
        )
        persisted = self.items(self.case_id(case))[0]
        self.assertEqual(persisted["size_bytes"], after.st_size)
        with db.get_db() as connection:
            fingerprint = connection.execute(
                "SELECT device, inode, mtime_ns, ctime_ns "
                "FROM recovered_channel_items WHERE item_id = ?",
                (self.item_id(persisted),),
            ).fetchone()
        self.assertEqual(
            (
                fingerprint["device"],
                fingerprint["inode"],
                fingerprint["mtime_ns"],
                fingerprint["ctime_ns"],
            ),
            (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
            "the newly claimed case must not begin with a stale source fingerprint",
        )


class RecoveredChannelHealthTests(RecoveredChannelTestCase):
    @staticmethod
    def diagnosis(
        *,
        clean: bool = True,
        warnings: list[str] | None = None,
        decoded: float = 60.0,
        declared: float | None = 60.0,
        expected: float | None = None,
    ) -> dict:
        return {
            "assessment": "healthy" if clean and not warnings else "damaged",
            "expected_duration_seconds": expected,
            "source": {
                "readable_duration_seconds": decoded,
                "warnings": warnings or [],
                "decode": {
                    "streams": [
                        {
                            "index": 0,
                            "type": "video",
                            "clean": clean,
                            "messages": [] if clean else ["invalid NAL unit"],
                            "decoded_duration_seconds": decoded,
                            "declared_duration_seconds": declared,
                        }
                    ]
                },
            },
        }

    def test_health_tiers_do_not_equate_clean_decode_with_proven_authenticity(self) -> None:
        clear, clear_findings = recovered_channels._classify_diagnosis(
            self.diagnosis()
        )
        minimal, minimal_findings = recovered_channels._classify_diagnosis(
            self.diagnosis(warnings=["Audio and video endpoints differ slightly"])
        )
        serious_decode, decode_findings = recovered_channels._classify_diagnosis(
            self.diagnosis(clean=False)
        )
        serious_short, short_findings = recovered_channels._classify_diagnosis(
            self.diagnosis(decoded=240, declared=240, expected=600)
        )
        serious_empty, empty_findings = recovered_channels._classify_diagnosis(
            self.diagnosis(
                decoded=0,
                declared=None,
                warnings=[
                    "No positive decoded endpoint was reported; completeness is inconclusive."
                ],
            )
        )

        self.assertEqual(clear, "clear")
        self.assertEqual(clear_findings, [])
        self.assertEqual(minimal, "minimal")
        self.assertTrue(any(item["severity"] == "minimal" for item in minimal_findings))
        self.assertEqual(serious_decode, "serious")
        self.assertTrue(any(item["code"] == "decode_error" for item in decode_findings))
        self.assertEqual(serious_short, "serious")
        self.assertTrue(
            any(item["code"] == "expected_duration_shortfall" for item in short_findings)
        )
        self.assertEqual(
            serious_empty,
            "serious",
            "a zero-length decode with no declared duration must not enter the best list",
        )
        self.assertTrue(
            any(item["severity"] == "serious" for item in empty_findings)
        )

    def test_failed_diagnosis_is_persisted_as_inconclusive_not_healthy(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        failed_child = {
            "id": "child-job",
            "status": "failed",
            "error": "ffprobe could not read the container",
        }
        with (
            patch.object(
                recovered_channels.salvage,
                "start_job",
                return_value=({"id": "child-job", "status": "queued"}, None, 202),
            ),
            patch.object(
                recovered_channels.salvage,
                "get_job",
                return_value=failed_child,
            ),
        ):
            job = recovered_channels.start_inspection(
                case_id,
                background=False,
                actor="tester",
            )

        item = self.items(case_id)[0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["fail_count"], 1)
        self.assertEqual(item["inspection_status"], "inconclusive")
        self.assertEqual(item["health_tier"], "inconclusive")
        self.assertEqual(item["decision"], "pending")
        self.assertIn("ffprobe", item["error"])

    def test_material_audio_video_endpoint_divergence_enters_review_list(self) -> None:
        diagnosis = self.diagnosis(
            decoded=60,
            declared=None,
            warnings=[
                "Primary video and audio stream 1 have materially different decoded "
                "endpoints (60.000s vs 20.000s)."
            ],
        )
        diagnosis["source"]["decode"]["streams"].append({
            "index": 1,
            "type": "audio",
            "clean": True,
            "messages": [],
            "decoded_duration_seconds": 20,
            "declared_duration_seconds": None,
        })

        health, findings = recovered_channels._classify_diagnosis(diagnosis)

        self.assertEqual(
            health,
            "serious",
            "material audio cutoff must not be presented in the best list",
        )
        self.assertTrue(any(item["severity"] == "serious" for item in findings))


class RecoveredChannelJobTests(RecoveredChannelTestCase):
    def test_init_migrates_legacy_duplicate_active_jobs_before_unique_index(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        with db.get_db() as connection:
            connection.execute("DROP INDEX idx_recovered_one_active_job")
            connection.execute(
                """
                INSERT INTO recovered_channel_jobs
                    (job_id, case_id, kind, status, total, created_at)
                VALUES ('legacy-job-one', ?, 'inspect', 'running', 1,
                        '2026-07-16 01:00:00')
                """,
                (case_id,),
            )
            connection.execute(
                """
                INSERT INTO recovered_channel_jobs
                    (job_id, case_id, kind, status, total, created_at)
                VALUES ('legacy-job-two', ?, 'inspect', 'queued', 1,
                        '2026-07-16 02:00:00')
                """,
                (case_id,),
            )
            connection.commit()

        # This models upgrading a database created before active starts were
        # atomic. Startup must repair it once, not fail its retry loop forever
        # while trying to create the new partial unique index.
        db.init_db()

        with db.get_db() as connection:
            rows = connection.execute(
                "SELECT job_id, status, completed_at, error "
                "FROM recovered_channel_jobs WHERE case_id = ? ORDER BY job_id",
                (case_id,),
            ).fetchall()
            index = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_recovered_one_active_job'",
            ).fetchone()
        active = [row for row in rows if row["status"] in {"queued", "running", "cancelling"}]
        retired = [row for row in rows if row["status"] not in {"queued", "running", "cancelling"}]
        self.assertEqual(len(active), 1, [dict(row) for row in rows])
        self.assertEqual(len(retired), 1, [dict(row) for row in rows])
        self.assertTrue(retired[0]["completed_at"])
        self.assertTrue(retired[0]["error"])
        self.assertIsNotNone(index)

    def test_only_one_case_job_can_be_active_and_restart_marks_it_interrupted(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        with patch.object(recovered_channels, "_launch"):
            first = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )
            with self.assertRaises(recovered_channels.RecoveredChannelError):
                recovered_channels.start_inspection(
                    case_id,
                    background=True,
                    actor="tester",
                )

        self.assertEqual(first["status"], "queued")
        recovered_channels.reset_runtime_for_tests()
        interrupted = recovered_channels.get_job(case_id, first["job_id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertIn("restarted", interrupted["error"].lower())

        with patch.object(recovered_channels, "_launch"):
            resumed = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )
        self.assertNotEqual(resumed["job_id"], first["job_id"])
        self.assertEqual(resumed["status"], "queued")

    def test_late_cancel_cannot_turn_a_completed_job_back_into_cancelling(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        with patch.object(recovered_channels, "_launch"):
            job = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )

        selected_active_row = threading.Event()
        allow_cancel_update = threading.Event()
        original_get_db = recovered_channels.get_db
        intercept_once = threading.Event()

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                result = self.connection.execute(sql, parameters)
                if (
                    "SELECT * FROM recovered_channel_jobs WHERE case_id" in sql
                    and not intercept_once.is_set()
                ):
                    # Materialize the one-row result before pausing so the
                    # separate writer below is not blocked by an open SQLite
                    # read cursor. cancel_job still receives the row it read.
                    rows = result.fetchall()

                    class MaterializedCursor:
                        def fetchone(self):
                            return rows[0] if rows else None

                    intercept_once.set()
                    selected_active_row.set()
                    self_outer.assertTrue(
                        allow_cancel_update.wait(timeout=3),
                        "late cancel was never released",
                    )
                    return MaterializedCursor()
                return result

            def __getattr__(self, name):
                return getattr(self.connection, name)

        self_outer = self

        @contextmanager
        def paused_get_db():
            with original_get_db() as connection:
                yield ConnectionProxy(connection)

        outcome: dict[str, object] = {}

        def cancel() -> None:
            try:
                outcome["result"] = recovered_channels.cancel_job(
                    case_id,
                    job["job_id"],
                    actor="late cancel",
                )
            except Exception as exc:
                outcome["error"] = exc

        with patch.object(recovered_channels, "get_db", paused_get_db):
            cancel_thread = threading.Thread(target=cancel)
            cancel_thread.start()
            self.assertTrue(
                selected_active_row.wait(timeout=3),
                "cancel did not read the active job",
            )
            with db.get_db() as connection:
                connection.execute(
                    "UPDATE recovered_channel_jobs "
                    "SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ?",
                    (job["job_id"],),
                )
                connection.commit()
            allow_cancel_update.set()
            cancel_thread.join(timeout=3)

        self.assertFalse(cancel_thread.is_alive())
        self.assertNotIn("error", outcome, outcome)
        with db.get_db() as connection:
            persisted = connection.execute(
                "SELECT status FROM recovered_channel_jobs WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(outcome["result"]["status"], "completed")

    def test_cancelled_queued_inspection_becomes_terminal_when_worker_wakes(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        with patch.object(recovered_channels, "_launch"):
            job = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )

        cancelled = recovered_channels.cancel_job(
            case_id,
            job["job_id"],
            actor="cancel before worker scheduling",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["completed_at"])

        recovered_channels.run_inspection_job(job["job_id"])

        terminal = recovered_channels.get_job(case_id, job["job_id"])
        self.assertEqual(terminal["status"], "cancelled")
        self.assertTrue(terminal["completed_at"])
        self.assertIsNone(recovered_channels.get_case(case_id)["active_job"])

    def test_cancelled_queued_organization_becomes_terminal_without_publishing(self) -> None:
        source = self.make_media(payload=b"approved archive")
        before = self.source_signature(source)
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        plan = recovered_channels.build_organization_plan(case_id)
        with patch.object(recovered_channels, "_launch"):
            job = recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                background=True,
                actor="tester",
            )

        cancelled = recovered_channels.cancel_job(
            case_id,
            job["job_id"],
            actor="cancel before worker scheduling",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["completed_at"])

        recovered_channels.run_organization_job(job["job_id"])

        terminal = recovered_channels.get_job(case_id, job["job_id"])
        self.assertEqual(terminal["status"], "cancelled")
        self.assertTrue(terminal["completed_at"])
        self.assertIsNone(recovered_channels.get_case(case_id)["active_job"])
        self.assertEqual(
            [entry for entry in self.target_dir.rglob("*") if entry.is_symlink()],
            [],
        )
        persisted = self.items(case_id)[0]
        self.assertEqual(persisted["decision"], "approved")
        self.assertIsNone(persisted["organized_path"])
        self.assertEqual(self.source_signature(source), before)

    def test_thread_start_failure_never_leaves_a_permanently_queued_job(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)

        with patch.object(
            recovered_channels.threading.Thread,
            "start",
            side_effect=RuntimeError("thread quota exhausted"),
        ):
            with self.assertRaises(Exception):
                recovered_channels.start_inspection(
                    case_id,
                    background=True,
                    actor="tester",
                )

        with db.get_db() as connection:
            row = connection.execute(
                "SELECT status, error FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(row["status"], {"queued", "running", "cancelling"})
        self.assertTrue(row["error"])

    def test_unexpected_background_inspection_exception_is_durable_and_resumable(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        with patch.object(
            recovered_channels,
            "run_inspection_job",
            side_effect=RuntimeError("decoder worker exploded"),
        ):
            started = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )

        deadline = time.monotonic() + 2
        current = recovered_channels.get_job(case_id, started["job_id"])
        while current["status"] in {"queued", "running", "cancelling"}:
            self.assertLess(time.monotonic(), deadline, "worker failure never became terminal")
            time.sleep(0.01)
            current = recovered_channels.get_job(case_id, started["job_id"])

        self.assertEqual(current["status"], "failed")
        self.assertIn("decoder worker exploded", current["error"])
        item = self.items(case_id)[0]
        self.assertEqual(item["inspection_status"], "pending")
        self.assertEqual(item["decision"], "pending")
        self.assertEqual(recovered_channels.get_case(case_id)["case"]["status"], "review")

        with patch.object(recovered_channels, "_launch"):
            resumed = recovered_channels.start_inspection(
                case_id,
                background=True,
                actor="tester",
            )
        self.assertEqual(resumed["status"], "queued")

    def test_unexpected_background_organization_exception_keeps_approved_work_retryable(self) -> None:
        self.make_media(payload=b"approved archive")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        plan = recovered_channels.build_organization_plan(case_id)
        with patch.object(
            recovered_channels,
            "run_organization_job",
            side_effect=RuntimeError("publisher worker exploded"),
        ):
            started = recovered_channels.start_organization(
                case_id,
                plan["plan_token"],
                background=True,
                actor="tester",
            )

        deadline = time.monotonic() + 2
        current = recovered_channels.get_job(case_id, started["job_id"])
        while current["status"] in {"queued", "running", "cancelling"}:
            self.assertLess(time.monotonic(), deadline, "worker failure never became terminal")
            time.sleep(0.01)
            current = recovered_channels.get_job(case_id, started["job_id"])

        self.assertEqual(current["status"], "failed")
        self.assertIn("publisher worker exploded", current["error"])
        persisted = self.items(case_id)[0]
        self.assertEqual(persisted["decision"], "approved")
        self.assertIsNone(persisted["organized_path"])
        self.assertEqual(recovered_channels.get_case(case_id)["case"]["status"], "review")
        retry_plan = recovered_channels.build_organization_plan(case_id)
        self.assertEqual(retry_plan["ready_count"], 1)

    def test_two_concurrent_inspection_starts_create_exactly_one_active_job(self) -> None:
        self.make_media()
        case = self.create_case()
        case_id = self.case_id(case)
        barrier = threading.Barrier(2)

        def attempt_start():
            barrier.wait(timeout=3)
            try:
                return (
                    "started",
                    recovered_channels.start_inspection(
                        case_id,
                        background=True,
                        actor="concurrent tester",
                    ),
                )
            except recovered_channels.RecoveredChannelError as exc:
                return ("conflict", exc.status, str(exc))

        with (
            patch.object(recovered_channels, "_launch"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(lambda _index: attempt_start(), range(2)))

        self.assertEqual([result[0] for result in results].count("started"), 1, results)
        self.assertEqual([result[0] for result in results].count("conflict"), 1, results)
        conflict = next(result for result in results if result[0] == "conflict")
        self.assertEqual(conflict[1], 409)
        with db.get_db() as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs "
                "WHERE case_id = ? AND status IN ('queued', 'running', 'cancelling')",
                (case_id,),
            ).fetchone()["count"]
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()["count"]
        self.assertEqual(active, 1)
        self.assertEqual(total, 1)

    def test_two_concurrent_organization_starts_create_exactly_one_active_job(self) -> None:
        self.make_media(payload=b"approved archive")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item)
        self.approve(case_id, item)
        plan = recovered_channels.build_organization_plan(case_id)
        barrier = threading.Barrier(2)

        def attempt_start():
            barrier.wait(timeout=3)
            try:
                return (
                    "started",
                    recovered_channels.start_organization(
                        case_id,
                        plan["plan_token"],
                        background=True,
                        actor="concurrent tester",
                    ),
                )
            except recovered_channels.RecoveredChannelError as exc:
                return ("conflict", exc.status, str(exc))

        with (
            patch.object(recovered_channels, "_launch"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(lambda _index: attempt_start(), range(2)))

        self.assertEqual([result[0] for result in results].count("started"), 1, results)
        self.assertEqual([result[0] for result in results].count("conflict"), 1, results)
        conflict = next(result for result in results if result[0] == "conflict")
        self.assertEqual(conflict[1], 409)
        with db.get_db() as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs "
                "WHERE case_id = ? AND status IN ('queued', 'running', 'cancelling')",
                (case_id,),
            ).fetchone()["count"]
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM recovered_channel_jobs WHERE case_id = ?",
                (case_id,),
            ).fetchone()["count"]
        self.assertEqual(active, 1)
        self.assertEqual(total, 1)


@unittest.skipIf(Flask is None, "Flask is required for recovered-channel route tests")
class RecoveredChannelRouteTests(RecoveredChannelTestCase):
    def setUp(self) -> None:
        super().setUp()
        assert Flask is not None
        assert recovered_channel_routes is not None
        assert recovery_routes is not None
        assert status_routes is not None
        assert transcode_routes is not None
        self.import_dir = self.root / "import"
        self.import_dir.mkdir()
        allowed = (
            self.source_dir,
            self.target_dir,
            self.hidden_dir,
            self.import_dir,
        )
        self.route_patchers = (
            patch.object(recovery, "SOURCE_DIR", self.source_dir),
            patch.object(recovery, "TARGET_DIR", self.target_dir),
            patch.object(recovery, "HIDDEN_DIR", self.hidden_dir),
            patch.object(recovery, "IMPORT_DIR", self.import_dir),
            patch.object(recovery, "RECOVERY_ALLOWED_BASES", allowed),
            patch.object(recovery_routes, "SOURCE_DIR", self.source_dir),
            patch.object(recovery_routes, "TARGET_DIR", self.target_dir),
            patch.object(recovery_routes, "HIDDEN_DIR", self.hidden_dir),
            patch.object(recovery_routes, "RECOVERY_ALLOWED_BASES", allowed),
            patch.object(transcode_routes, "RECOVERY_ALLOWED_BASES", allowed),
        )
        for patcher in self.route_patchers:
            patcher.start()
        self.app = Flask(__name__, static_folder=None)
        self.app.secret_key = "recovered-channel-test-secret"
        self.app.register_blueprint(recovered_channel_routes.bp)
        self.app.register_blueprint(recovery_routes.bp)
        self.app.register_blueprint(status_routes.bp)
        self.app.register_blueprint(transcode_routes.bp)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        for patcher in reversed(self.route_patchers):
            patcher.stop()
        super().tearDown()

    def login(self) -> None:
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "route-tester"

    @staticmethod
    def equivalent_alias(path: Path) -> Path:
        detour = path.parent / "existing-detour"
        detour.mkdir(exist_ok=True)
        return detour / ".." / path.name

    def test_every_case_read_and_mutation_route_requires_authentication(self) -> None:
        endpoints = (
            ("get", "/api/recovery/channel-cases", None),
            ("post", "/api/recovery/channel-cases", {}),
            ("get", "/api/recovery/channel-cases/case", None),
            ("patch", "/api/recovery/channel-cases/case", {}),
            ("get", "/api/recovery/channel-cases/case/items", None),
            ("patch", "/api/recovery/channel-cases/case/items/item", {}),
            ("post", "/api/recovery/channel-cases/case/items/item/repair-link", {}),
            ("post", "/api/recovery/channel-cases/case/decisions", {}),
            ("post", "/api/recovery/channel-cases/case/inspect", {}),
            ("get", "/api/recovery/channel-cases/case/jobs/job", None),
            ("post", "/api/recovery/channel-cases/case/jobs/job/cancel", {}),
            ("post", "/api/recovery/channel-cases/case/organization-plan", {}),
            ("post", "/api/recovery/channel-cases/case/organize", {}),
        )
        for method, path, body in endpoints:
            with self.subTest(method=method, path=path):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 401)

    def test_create_and_mutation_payloads_are_strictly_validated(self) -> None:
        self.login()
        self.make_media()

        non_object = self.client.post(
            "/api/recovery/channel-cases",
            json=["not", "an", "object"],
        )
        self.assertEqual(non_object.status_code, 400)

        invalid_channel = self.client.post(
            "/api/recovery/channel-cases",
            json={
                "source_channel_id": "../source",
                "target_channel_name": "The Oversphere",
            },
        )
        self.assertEqual(invalid_channel.status_code, 400)

        created = self.client.post(
            "/api/recovery/channel-cases",
            json={
                "source_channel_id": SOURCE_CHANNEL_ID,
                "target_channel_name": "The Oversphere",
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        case_id = created.get_json()["case"]["case_id"]

        bad_page = self.client.get(
            f"/api/recovery/channel-cases/{case_id}/items?page=not-a-number"
        )
        self.assertEqual(bad_page.status_code, 400)
        bad_rescan = self.client.post(
            f"/api/recovery/channel-cases/{case_id}/inspect",
            json={"rescan": "false"},
        )
        self.assertEqual(bad_rescan.status_code, 400)
        bad_plan_token = self.client.post(
            f"/api/recovery/channel-cases/{case_id}/organize",
            json={"plan_token": "not-a-token"},
        )
        self.assertEqual(bad_plan_token.status_code, 400)

        with db.get_db() as connection:
            event = connection.execute(
                """
                SELECT actor FROM recovered_channel_events
                WHERE case_id = ? AND event = 'case_created'
                """,
                (case_id,),
            ).fetchone()
        self.assertEqual(event["actor"], "route-tester")

    def test_scoped_organization_routes_bind_scope_and_reject_extra_keys(self) -> None:
        self.login()
        self.make_media(payload=b"route-likely-source")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        self.seed_completed_inspection(item, health_tier="clear")
        plan_path = f"/api/recovery/channel-cases/{case_id}/organization-plan"

        unknown = self.client.post(plan_path, json={"scope": "everything"})
        self.assertEqual(unknown.status_code, 400, unknown.get_json())
        null_plan = self.client.post(plan_path, json={"scope": None})
        self.assertEqual(null_plan.status_code, 400, null_plan.get_json())
        injected_plan = self.client.post(
            plan_path,
            json={"scope": "likely_usable", "item_ids": [self.item_id(item)]},
        )
        self.assertEqual(injected_plan.status_code, 400, injected_plan.get_json())
        planned = self.client.post(
            plan_path, json={"scope": "likely_usable"}
        )
        self.assertEqual(planned.status_code, 200, planned.get_json())
        plan = planned.get_json()["plan"]
        self.assertEqual(plan["scope"], "likely_usable")
        self.assertEqual(plan["ready_count"], 1)
        self.assertEqual(plan["items"][0]["decision"], "pending")

        organize_path = f"/api/recovery/channel-cases/{case_id}/organize"
        null_organize = self.client.post(
            organize_path,
            json={"plan_token": plan["plan_token"], "scope": None},
        )
        self.assertEqual(
            null_organize.status_code, 400, null_organize.get_json()
        )
        missing_scope = self.client.post(
            organize_path, json={"plan_token": plan["plan_token"]}
        )
        self.assertEqual(missing_scope.status_code, 409, missing_scope.get_json())
        injected_organize = self.client.post(
            organize_path,
            json={
                "plan_token": plan["plan_token"],
                "scope": "likely_usable",
                "candidate_path": str(self.root / "injected.mp4"),
            },
        )
        self.assertEqual(
            injected_organize.status_code, 400, injected_organize.get_json()
        )
        with patch.object(recovered_channels, "_launch"):
            started = self.client.post(
                organize_path,
                json={
                    "plan_token": plan["plan_token"],
                    "scope": "likely_usable",
                },
            )
        self.assertEqual(started.status_code, 202, started.get_json())
        job = started.get_json()["job"]
        self.assertEqual(job["scope"], "likely_usable")
        with db.get_db() as connection:
            stored = connection.execute(
                "SELECT payload_json FROM recovered_channel_jobs WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            event = connection.execute(
                "SELECT actor, details_json FROM recovered_channel_events "
                "WHERE case_id = ? AND event = 'organization_started' "
                "ORDER BY event_id DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        payload = json.loads(stored["payload_json"])
        details = json.loads(event["details_json"])
        self.assertEqual(payload["scope"], "likely_usable")
        self.assertEqual(payload["items"][0]["decision"], "pending")
        self.assertEqual(details["scope"], "likely_usable")
        self.assertEqual(event["actor"], "route-tester")

    def test_case_repair_route_accepts_no_client_path_and_records_actor(self) -> None:
        source, case, original_item, recovered_link = (
            RecoveredChannelIntegrationTests.publish_one(self)
        )
        before = self.source_signature(source)
        case_id = self.case_id(case)
        item_id = self.item_id(original_item)
        recovered_link.unlink()
        path = (
            f"/api/recovery/channel-cases/{case_id}/items/{item_id}/repair-link"
        )

        unauthenticated = self.client.post(path, json={})
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertFalse(os.path.lexists(recovered_link))

        self.login()
        injected = self.client.post(
            path,
            json={"candidate_path": str(self.root / "alternate.mp4")},
        )
        self.assertEqual(injected.status_code, 400, injected.get_json())
        self.assertFalse(os.path.lexists(recovered_link))

        repaired = self.client.post(path, json={})
        self.assertEqual(repaired.status_code, 200, repaired.get_json())
        body = repaired.get_json()
        self.assertTrue(body["link_repaired"])
        self.assertEqual(body["item"]["link_status"], "healthy")
        self.assertTrue(recovered_link.is_symlink())
        with db.get_db() as connection:
            event = connection.execute(
                "SELECT actor FROM recovered_channel_events "
                "WHERE case_id = ? AND item_id = ? "
                "AND event = 'recovered_link_repaired'",
                (case_id, item_id),
            ).fetchone()
        self.assertEqual(event["actor"], "route-tester")
        self.assertEqual(self.source_signature(source), before)

    def test_status_counts_recovered_video_verified_only_with_strict_link_health(self) -> None:
        source, _case, _item, recovered_link = (
            RecoveredChannelIntegrationTests.publish_one(self)
        )
        expected_target = os.readlink(recovered_link)
        self.login()

        healthy = self.client.get("/api/status")
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.get_json()["verified_links"], 1)
        self.assertEqual(healthy.get_json()["missing_count"], 0)

        recovered_link.unlink()
        absent = self.client.get("/api/status")
        self.assertEqual(absent.get_json()["verified_links"], 0)
        self.assertEqual(absent.get_json()["missing_count"], 1)

        recovered_link.symlink_to(self.root / "wrong-source.mp4")
        wrong = self.client.get("/api/status")
        self.assertEqual(wrong.get_json()["verified_links"], 0)
        self.assertEqual(wrong.get_json()["missing_count"], 1)

        recovered_link.unlink()
        recovered_link.symlink_to(expected_target)
        source.write_bytes(b"changed after recovered publication")
        source_invalid = self.client.get("/api/status")
        self.assertEqual(source_invalid.get_json()["verified_links"], 0)
        self.assertEqual(source_invalid.get_json()["missing_count"], 1)

    def test_recovery_start_and_force_never_stage_owned_source_media(self) -> None:
        self.login()
        source = self.make_media(payload=b"recovered immutable source")
        self.create_case()
        alias = self.equivalent_alias(source)
        before = self.source_signature(source)

        def fake_metadata_fetch(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            Path(f"{output}.info.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(recovery.subprocess, "run", side_effect=fake_metadata_fetch),
            patch.object(
                recovery,
                "_prepare_media_stage",
                side_effect=AssertionError("recovered source reached staging"),
            ) as stage,
        ):
            metadata_response = self.client.post(
                "/api/recovery/start",
                json={"filepath": str(alias)},
            )
        self.assertEqual(metadata_response.status_code, 409, metadata_response.get_json())
        self.assertIn(
            "immutable",
            (metadata_response.get_json().get("error") or "").lower(),
        )
        stage.assert_not_called()
        self.assertEqual(self.source_signature(source), before)
        self.assertEqual(list(self.import_dir.iterdir()), [])

        with patch.object(
            recovery,
            "_prepare_media_stage",
            side_effect=AssertionError("recovered source reached staging"),
        ) as stage:
            force_response = self.client.post(
                "/api/recovery/force",
                json={"filepath": str(alias)},
            )
        self.assertEqual(force_response.status_code, 409, force_response.get_json())
        self.assertIn(
            "immutable",
            (force_response.get_json().get("error") or "").lower(),
        )
        stage.assert_not_called()
        self.assertEqual(self.source_signature(source), before)
        self.assertEqual(list(self.import_dir.iterdir()), [])

    def test_destruct_mode_cannot_delete_owned_source_through_an_alias(self) -> None:
        self.login()
        source = self.make_media(payload=b"recovered immutable source")
        case = self.create_case()
        case_id = self.case_id(case)
        alias = self.equivalent_alias(source)
        before = self.source_signature(source)

        with patch.object(recovery_routes, "fetch_all_metadata", return_value={}):
            response = self.client.post(
                "/api/recovery/delete-batch",
                json={"filepaths": [str(alias)], "destruct_mode": True},
            )

        self.assertIn(response.status_code, {200, 409}, response.get_json())
        body = response.get_json()
        if response.status_code == 200:
            self.assertEqual(body["success_count"], 0)
            self.assertEqual(body["fail_count"], 1)
            error_text = " ".join(body.get("errors") or [])
        else:
            error_text = body.get("error") or ""
        self.assertTrue(
            "recovered" in error_text.lower() or "immutable" in error_text.lower(),
            body,
        )
        self.assertEqual(self.source_signature(source), before)
        self.assertEqual(len(self.items(case_id)), 1)

    def test_destruct_mode_cannot_delete_owned_link_or_source_through_an_alias(self) -> None:
        self.login()
        source = self.make_media(payload=b"recovered immutable source")
        case = self.create_case()
        case_id = self.case_id(case)
        item = self.items(case_id)[0]
        destination_dir = self.target_dir / "The Oversphere [Recovered]"
        destination_dir.mkdir()
        destination = destination_dir / f"{VIDEO_ID}.mp4"
        destination.symlink_to(source)
        with db.get_db() as connection:
            connection.execute(
                "UPDATE recovered_channel_items SET organized_path = ? WHERE item_id = ?",
                (str(destination), self.item_id(item)),
            )
            connection.execute(
                "INSERT INTO videos (video_id, title, channel, symlink, status) "
                "VALUES (?, ?, ?, ?, 'recovered')",
                (VIDEO_ID, VIDEO_ID, "The Oversphere [Recovered]", str(destination)),
            )
            connection.commit()
        alias = self.equivalent_alias(destination)
        before = self.source_signature(source)

        with patch.object(recovery_routes, "fetch_all_metadata", return_value={}):
            response = self.client.post(
                "/api/recovery/delete-batch",
                json={"filepaths": [str(alias)], "destruct_mode": True},
            )

        self.assertIn(response.status_code, {200, 409}, response.get_json())
        body = response.get_json()
        if response.status_code == 200:
            self.assertEqual(body["success_count"], 0)
            self.assertEqual(body["fail_count"], 1)
            error_text = " ".join(body.get("errors") or [])
        else:
            error_text = body.get("error") or ""
        self.assertTrue(
            "recovered" in error_text.lower() or "immutable" in error_text.lower(),
            body,
        )
        self.assertTrue(destination.is_symlink())
        self.assertEqual(self.source_signature(source), before)
        video, persisted = RecoveredChannelIntegrationTests.recovered_rows(
            self,
            self.item_id(item),
        )
        self.assertEqual(video["status"], "recovered")
        self.assertEqual(persisted["organized_path"], str(destination))

    def test_transcode_guard_recognizes_equivalent_owned_source_paths(self) -> None:
        self.login()
        source = self.make_media(payload=b"recovered immutable source")
        self.create_case()
        alias = self.equivalent_alias(source)
        before = self.source_signature(source)

        with (
            patch.object(
                transcode_routes,
                "detect_encoder",
                side_effect=AssertionError("recovered source reached encoder selection"),
            ) as detect,
            patch.object(
                transcode_routes,
                "start_transcode_job",
                side_effect=AssertionError("recovered source reached transcode"),
            ) as start,
        ):
            response = self.client.post(
                "/api/transcode/start",
                json={"filepath": str(alias)},
            )

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertIn("immutable", (response.get_json().get("error") or "").lower())
        detect.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.source_signature(source), before)


if __name__ == "__main__":
    unittest.main()
