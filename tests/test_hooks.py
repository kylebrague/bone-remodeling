from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "scripts" / "pre_tool_policy.py"


class HookPolicyTests(unittest.TestCase):
    def run_hook(self, payload: dict[str, object], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=merged_env,
            check=False,
        )

    def test_denies_write_tool_in_read_only_mode(self) -> None:
        result = self.run_hook(
            {"toolName": "write", "toolArgs": "{}"},
            env={"OSTEOBLAST_READ_ONLY": "1"},
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["permissionDecision"], "deny")

    def test_denies_mutating_shell_command_in_serious_mode(self) -> None:
        result = self.run_hook(
            {
                "toolName": "bash",
                "toolArgs": json.dumps({"command": "git commit -m 'bad idea'"}),
            },
            env={"OSTEOBLAST_FINDING_SEVERITY": "serious"},
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["permissionDecision"], "deny")

    def test_allows_safe_shell_command(self) -> None:
        result = self.run_hook(
            {
                "toolName": "bash",
                "toolArgs": json.dumps({"command": "git status --short"}),
            },
            env={"OSTEOBLAST_READ_ONLY": "1"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
