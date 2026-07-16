from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from ta_organizerr import media_reservations


class MediaReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def tearDown(self) -> None:
        media_reservations._reset_reservations_for_tests()

    def test_inode_reservation_is_atomic_and_release_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            alias = Path(tmp) / "alias.mp4"
            source.write_bytes(b"media")
            os.link(source, alias)
            identity = media_reservations.media_identity(source.stat())
            first = media_reservations.reserve_media(
                (identity,), paths=(source,), owner="media salvage job one"
            )

            with self.assertRaises(media_reservations.MediaReservationConflict):
                media_reservations.reserve_media(
                    (media_reservations.media_identity(alias.stat()),),
                    paths=(alias,),
                    owner="transcode",
                )

            media_reservations.release_media(first)
            media_reservations.release_media(first)
            second = media_reservations.reserve_media(
                (identity,), paths=(alias,), owner="transcode"
            )
            media_reservations.release_media(second)

    def test_guard_detects_hardlink_identity_not_only_path_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            alias = Path(tmp) / "alias.mp4"
            source.write_bytes(b"media")
            os.link(source, alias)
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source,),
                owner="media salvage job",
            )
            try:
                with self.assertRaisesRegex(
                    media_reservations.MediaReservationConflict,
                    "currently in use",
                ):
                    with media_reservations.media_mutation_guard(
                        paths=(alias,), action="delete this file"
                    ):
                        alias.unlink()
            finally:
                media_reservations.release_media(reservation)

            self.assertTrue(alias.exists())

    def test_requested_symlink_entry_is_protected_separately_from_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            link = root / "video.mp4"
            source.write_bytes(b"media")
            link.symlink_to(source)
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source, link),
                owner="media salvage job",
            )
            try:
                with self.assertRaises(media_reservations.MediaReservationConflict):
                    with media_reservations.media_mutation_guard(
                        paths=(link,), action="remove this organized link"
                    ):
                        link.unlink()
            finally:
                media_reservations.release_media(reservation)

            self.assertTrue(link.is_symlink())

    def test_tree_move_refuses_reserved_requested_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = root / "Channel"
            channel.mkdir()
            source = root / "source.mp4"
            link = channel / "video.mp4"
            source.write_bytes(b"media")
            link.symlink_to(source)
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source, link),
                owner="media salvage job",
            )
            try:
                with self.assertRaises(media_reservations.MediaReservationConflict):
                    with media_reservations.media_mutation_guard(
                        trees=(channel,), action="move this channel"
                    ):
                        channel.rename(root / "Moved")
            finally:
                media_reservations.release_media(reservation)

            self.assertTrue(channel.is_dir())

    def test_owner_can_commit_its_own_reserved_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            source.write_bytes(b"media")
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source,),
                owner="transcode",
            )
            try:
                with media_reservations.media_mutation_guard(
                    paths=(source,),
                    except_reservation=reservation,
                    action="publish transcoded media",
                ):
                    source.write_bytes(b"replacement")
            finally:
                media_reservations.release_media(reservation)

            self.assertEqual(source.read_bytes(), b"replacement")

    def test_extending_lease_protects_generated_output_until_original_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            output = root / ".job.partial.mkv"
            source.write_bytes(b"source")
            output.write_bytes(b"partial")
            reservation = media_reservations.reserve_media(
                (media_reservations.media_identity(source.stat()),),
                paths=(source,),
                owner="media salvage job",
            )
            media_reservations.extend_media_reservation(
                reservation,
                (media_reservations.media_identity(output.stat()),),
                paths=(output,),
            )

            with self.assertRaises(media_reservations.MediaReservationConflict):
                media_reservations.reserve_media(
                    (media_reservations.media_identity(output.stat()),),
                    paths=(output,),
                    owner="second job",
                )

            # The caller may retain the original immutable handle; its token
            # releases the complete, extended lease stored in the registry.
            media_reservations.release_media(reservation)
            second = media_reservations.reserve_media(
                (media_reservations.media_identity(output.stat()),),
                paths=(output,),
                owner="second job",
            )
            media_reservations.release_media(second)

    def test_new_reservation_waits_until_guarded_mutation_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "video.mp4"
            source.write_bytes(b"media")
            identity = media_reservations.media_identity(source.stat())
            attempted = threading.Event()
            completed = threading.Event()
            result: list[media_reservations.MediaReservation] = []

            def reserve() -> None:
                attempted.set()
                result.append(media_reservations.reserve_media(
                    (identity,), paths=(source,), owner="media salvage job"
                ))
                completed.set()

            with media_reservations.media_mutation_guard(
                paths=(source,), action="replace this file"
            ):
                worker = threading.Thread(target=reserve)
                worker.start()
                self.assertTrue(attempted.wait(timeout=1))
                self.assertFalse(completed.wait(timeout=0.05))

            worker.join(timeout=1)
            self.assertTrue(completed.is_set())
            media_reservations.release_media(result[0])


if __name__ == "__main__":
    unittest.main()
