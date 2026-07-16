"""Process-local serialization for library filesystem mutations.

All code that changes the managed source/target/hidden inventory must take this
lock.  It is re-entrant because guarded actions finish by rebuilding the
inventory before releasing the mutation lock.
"""
from __future__ import annotations

import threading


LIBRARY_MUTATION_LOCK = threading.RLock()
