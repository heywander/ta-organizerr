from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from ta_organizerr.library_lock import (
    LibraryMutationLockError,
    _LibraryMutationLock,
)


class LibraryMutationLockTests(unittest.TestCase):
    def test_independent_writers_share_the_same_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mutation.lock"
            first = _LibraryMutationLock()
            second = _LibraryMutationLock()

            with first.scoped(path):
                with self.assertRaises(LibraryMutationLockError) as raised:
                    with second.scoped(path, blocking=False):
                        self.fail("A second writer entered the shared lock")

            self.assertTrue(raised.exception.busy)

    def test_nested_library_guards_reuse_the_outer_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mutation.lock"
            lock = _LibraryMutationLock()

            with lock.scoped(path):
                with lock:
                    with lock:
                        pass

    def test_nonblocking_guard_does_not_wait_for_another_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mutation.lock"
            lock = _LibraryMutationLock()
            attempted = threading.Event()
            outcome: list[object] = []

            def contend() -> None:
                attempted.set()
                try:
                    with lock.scoped(path, blocking=False):
                        outcome.append("entered")
                except LibraryMutationLockError as exc:
                    outcome.append(exc.busy)

            with lock.scoped(path):
                thread = threading.Thread(target=contend)
                thread.start()
                self.assertTrue(attempted.wait(timeout=1))
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())

            self.assertEqual(outcome, [True])


if __name__ == "__main__":
    unittest.main()
