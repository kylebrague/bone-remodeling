from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from osteoblast_core.commands import CommandError, CommandResult
from osteoblast_core.controller import DiffStats, OsteoblastController
from osteoblast_core.models import Finding, Manifest, OsteoblastError


def make_manifest() -> Manifest:
    return Manifest.from_mapping(
        {
            "version": "1",
            "base_branch": "main",
            "include_paths": ["src", "docs"],
            "exclude_paths": [],
            "allowed_categories": ["bugs", "hardening", "docs"],
            "severity_rules": {
                "confidence_threshold": 0.75,
                "serious_path_keywords": ["security", ".github/workflows"],
                "public_api_globs": ["api/**"],
                "forbidden_local_globs": [".github/workflows/**"],
            },
            "max_files_changed": 5,
            "max_changed_lines": 150,
            "verify": {"commands": ["python -m unittest"]},
            "pr": {"labels": ["osteoblast"], "reviewers": []},
            "schedule": {"enabled": True, "cron": "17 5 * * 1-5"},
        }
    )


def make_finding(*, severity: str = "routine") -> Finding:
    return Finding.from_dict(
        {
            "severity": severity,
            "type": "osteoblast",
            "category": "hardening",
            "scope": "src/service",
            "proof": ["Input is used without validation."],
            "candidate_files": ["src/service.py"],
            "why": "The service should validate input before use.",
            "estimated_change_size": {"files": 1, "lines": 12},
            "confidence": 0.93,
            "commit_title": "validate service input",
            "verification_hint": "pytest tests/test_service.py",
        }
    )


class PrefixRunner:
    def __init__(self, handlers: list[tuple[tuple[str, ...], object]]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, cwd=None, env=None, input_text=None, check=True):
        key = tuple(args)
        self.calls.append(key)
        for prefix, response in self.handlers:
            if key[: len(prefix)] == prefix:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(args=args, cwd=cwd, env=env, input_text=input_text, check=check)
                return response
        raise AssertionError(f"Unhandled command: {key}")


class StubController(OsteoblastController):
    def __init__(self, *, manifest: Manifest, **kwargs) -> None:
        super().__init__(**kwargs)
        self._manifest = manifest

    def load_manifest(self) -> Manifest:
        return self._manifest


class ControllerTests(unittest.TestCase):
    def test_bootstrap_renders_overlay_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_root = Path(temp_dir)
            controller = OsteoblastController(
                repo_root=target_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            created = controller.bootstrap(
                target_root=target_root,
                core_repository="acme/osteoblast-core",
                core_ref="v1.2.3",
                force=True,
            )
            workflow = target_root / ".github" / "workflows" / "osteoblast.yml"
            self.assertIn(workflow, created)
            contents = workflow.read_text(encoding="utf-8")
            self.assertIn("acme/osteoblast-core", contents)
            self.assertIn("v1.2.3", contents)
            self.assertTrue((target_root / ".github" / "hooks" / "scripts" / "pre_tool_policy.py").exists())

    def test_branch_name_matches_plan(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        branch = controller.branch_name_for(make_finding())
        self.assertEqual(branch, "osteoblast/hardening/src-service/20260410")

    def test_run_scheduled_skips_when_open_pr_exists(self) -> None:
        class SkipController(StubController):
            def has_open_routine_pr(self) -> bool:
                return True

        controller = SkipController(
            manifest=make_manifest(),
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        self.assertEqual(
            controller.run_scheduled(),
            {"status": "skipped", "reason": "open-osteoblast-pr"},
        )

    def test_validate_routine_diff_rejects_forbidden_path(self) -> None:
        controller = StubController(
            manifest=make_manifest(),
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        controller.diff_stats = lambda: DiffStats(
            files=(".github/workflows/ci.yml",),
            file_count=1,
            changed_lines=10,
        )
        with self.assertRaises(OsteoblastError):
            controller.validate_routine_diff(make_finding(), make_manifest())

    def test_escalate_serious_finding_falls_back_to_copilot_assignee(self) -> None:
        issue_create = CommandResult(
            args=("gh", "issue", "create"),
            stdout="https://github.com/acme/repo/issues/42\n",
            stderr="",
            returncode=0,
        )
        issue_edit = CommandResult(args=("gh", "issue", "edit"), stdout="", stderr="", returncode=0)
        issue_comment = CommandResult(args=("gh", "issue", "comment"), stdout="", stderr="", returncode=0)
        runner = PrefixRunner(
            [
                (("gh", "issue", "create"), issue_create),
                (
                    ("gh", "agent-task", "create"),
                    CommandError(["gh", "agent-task", "create"], 1, "", "boom"),
                ),
                (("gh", "issue", "edit"), issue_edit),
                (("gh", "issue", "comment"), issue_comment),
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        result = controller.escalate_serious_finding(make_finding(severity="serious"), make_manifest())
        self.assertEqual(result["mode"], "copilot-assignee")
        self.assertTrue(any(call[:3] == ("gh", "issue", "edit") for call in runner.calls))

    def test_escalate_serious_finding_uses_agent_task_when_available(self) -> None:
        issue_create = CommandResult(
            args=("gh", "issue", "create"),
            stdout="https://github.com/acme/repo/issues/42\n",
            stderr="",
            returncode=0,
        )
        agent_task_create = CommandResult(args=("gh", "agent-task", "create"), stdout="", stderr="", returncode=0)
        agent_task_list = CommandResult(
            args=("gh", "agent-task", "list"),
            stdout=json.dumps(
                [
                    {
                        "id": "task-123",
                        "pullRequestUrl": "https://github.com/acme/repo/pull/99",
                        "state": "queued",
                    }
                ]
            ),
            stderr="",
            returncode=0,
        )
        issue_comment = CommandResult(args=("gh", "issue", "comment"), stdout="", stderr="", returncode=0)
        runner = PrefixRunner(
            [
                (("gh", "issue", "create"), issue_create),
                (("gh", "agent-task", "create"), agent_task_create),
                (("gh", "agent-task", "list"), agent_task_list),
                (("gh", "issue", "comment"), issue_comment),
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        result = controller.escalate_serious_finding(make_finding(severity="serious"), make_manifest())
        self.assertEqual(result["mode"], "agent-task")
        self.assertEqual(result["task"]["id"], "task-123")
