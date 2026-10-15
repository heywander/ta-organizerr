"""Flask extension singletons.

Kept in their own module so route blueprints can import them without
creating a circular dependency on ``app.py``.
"""
from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


limiter = Limiter(key_func=get_remote_address, default_limits=[])
