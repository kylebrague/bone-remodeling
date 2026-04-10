from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from osteoblast_core.commands import CommandError


class CommandTests(unittest.TestCase):
    def test_command_error_sanitizes_multiline_args_and_includes_stderr(self) -> None:
        error = CommandError(
            [
                "gh",
                "issue",
                "create",
                "--title",
                "[Osteoblast serious] remove unused hierarchy emitter",
                "--body",
                "## Serious Osteoblast finding\n\nThis module is unused.",
            ],
            1,
            "",
            "flag needs an argument: --body",
        )

        message = str(error)
        self.assertIn("Command failed with exit code 1:", message)
        self.assertIn("flag needs an argument: --body", message)
        self.assertIn("'## Serious Osteoblast finding This module is unused.'", message)
        self.assertNotIn("## Serious Osteoblast finding\n\nThis module is unused.", message)


if __name__ == "__main__":
    unittest.main()
