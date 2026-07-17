"""Re-entrant process and cross-process exclusion for library mutations."""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import DATA_DIR


class LibraryMutationLockError(RuntimeError):
    """The shared writer lock could not be acquired safely."""

    def __init__(self, message: str, *, busy: bool = False):
        super().__init__(message)
        self.busy = busy


class _LibraryMutationLock:
    """A thread-reentrant lock backed by one advisory file lock.

    Every Target/Hidden writer already uses this object. Adding ``flock`` here
    extends that existing contract across Gunicorn workers and helper
    processes. Nested guards in one thread reuse the outer descriptor, which
    is important for media reservations inside designation transactions.
    """

    def __init__(self) -> None:
        self._thread_lock = threading.RLock()
        self._state = threading.local()

    @staticmethod
    def _fallback_path() -> Path:
        digest = hashlib.sha256(
            str(DATA_DIR.absolute()).encode("utf-8")
        ).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / (
            f"ta-organizerr-{os.getuid()}-{digest}.mutation.lock"
        )

    @classmethod
    def _default_path(cls) -> Path:
        directory = DATA_DIR / "channel-designation-journals"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = os.lstat(directory).st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise OSError("library lock directory is not a real directory")
            directory.chmod(0o700)
            return directory / ".mutation.lock"
        except OSError:
            # Unit tests and pre-bootstrap tools may not have /app/data yet.
            # A stable per-data-root lock in the system temporary directory
            # still excludes every process using that same configuration.
            return cls._fallback_path()

    def acquire(
        self,
        blocking: bool = True,
        *,
        lock_path: Path | None = None,
    ) -> bool:
        if not self._thread_lock.acquire(blocking=blocking):
            raise LibraryMutationLockError(
                "Another library mutation is already running", busy=True
            )
        depth = int(getattr(self._state, "depth", 0))
        if depth:
            self._state.depth = depth + 1
            return True

        path = Path(lock_path) if lock_path is not None else self._default_path()
        descriptor: int | None = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("library lock path is not a regular file")
            os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise LibraryMutationLockError(
                    "Another library mutation is already running", busy=True
                ) from exc
            self._state.depth = 1
            self._state.descriptor = descriptor
            self._state.path = path
            return True
        except LibraryMutationLockError:
            if descriptor is not None:
                os.close(descriptor)
            self._thread_lock.release()
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            self._thread_lock.release()
            raise LibraryMutationLockError(
                f"Could not acquire the shared library mutation lock: {exc}"
            ) from exc

    def release(self) -> None:
        depth = int(getattr(self._state, "depth", 0))
        if depth <= 0:
            raise RuntimeError("Cannot release an un-acquired library lock")
        if depth > 1:
            self._state.depth = depth - 1
            self._thread_lock.release()
            return
        descriptor = self._state.descriptor
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._state.depth = 0
            self._state.descriptor = None
            self._state.path = None
            self._thread_lock.release()

    def __enter__(self) -> "_LibraryMutationLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    @contextmanager
    def scoped(
        self,
        lock_path: Path,
        *,
        blocking: bool = True,
    ) -> Iterator[None]:
        self.acquire(blocking=blocking, lock_path=lock_path)
        try:
            yield
        finally:
            self.release()


LIBRARY_MUTATION_LOCK = _LibraryMutationLock()
