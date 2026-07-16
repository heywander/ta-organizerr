from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from ta_organizerr import config


class HostSourceConfigurationTests(unittest.TestCase):
    def test_configured_host_source_must_be_absolute(self) -> None:
        with patch.dict(os.environ, {"HOST_SOURCE_BASE": "relative/archive"}):
            with self.assertRaisesRegex(ValueError, "must be an absolute"):
                config._host_source_base()

    def test_blank_value_uses_absolute_legacy_fallback(self) -> None:
        with patch.dict(os.environ, {"HOST_SOURCE_BASE": "   "}):
            configured, value = config._host_source_base()

        self.assertFalse(configured)
        self.assertEqual(value, Path("/mnt/user/tubearchives/bp"))


if __name__ == "__main__":
    unittest.main()
