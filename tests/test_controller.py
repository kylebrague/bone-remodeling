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
            self.assertTrue(
                (
                    target_root
                    / ".github"
                    / "skills"
                    / "osteoblast-manifest-setup"
                    / "SKILL.md"
                ).exists()
            )

    def test_branch_name_matches_plan(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        branch = controller.branch_name_for(make_finding())
        self.assertEqual(branch, "osteoblast/hardening/src-service/20260410")

    def test_ensure_clean_worktree_ignores_nested_core_checkout(self) -> None:
        def run_status(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["git", "status", "--porcelain"])
            self.assertIn(":(exclude).osteoblast-core", args)
            return CommandResult(
                args=tuple(args),
                stdout="",
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (
                    ("git", "status", "--porcelain"),
                    run_status,
                )
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT / ".osteoblast-core",
            runner=runner,
            today=date(2026, 4, 10),
        )
        controller.ensure_clean_worktree()

    def test_ensure_clean_worktree_rejects_non_core_changes(self) -> None:
        def run_status(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["git", "status", "--porcelain"])
            self.assertIn(":(exclude).osteoblast-core", args)
            return CommandResult(
                args=tuple(args),
                stdout="?? changed.txt\n",
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (
                    ("git", "status", "--porcelain"),
                    run_status,
                )
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT / ".osteoblast-core",
            runner=runner,
            today=date(2026, 4, 10),
        )
        with self.assertRaises(OsteoblastError):
            controller.ensure_clean_worktree()

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

    def test_pick_scope_error_mentions_configured_and_existing_paths(self) -> None:
        manifest = Manifest.from_mapping(
            {
                "version": "1",
                "base_branch": "main",
                "include_paths": ["src", "app", "lib", "docs"],
                "exclude_paths": [],
                "allowed_categories": ["bugs"],
                "severity_rules": {"confidence_threshold": 0.75},
                "max_files_changed": 5,
                "max_changed_lines": 150,
                "verify": {"commands": ["python -m unittest"]},
                "pr": {"labels": ["osteoblast"], "reviewers": []},
                "schedule": {"enabled": True},
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / "packages").mkdir()
            (repo_root / "documentation").mkdir()
            controller = OsteoblastController(
                repo_root=repo_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            with self.assertRaises(OsteoblastError) as context:
                controller.pick_scope(manifest)
            message = str(context.exception)
            self.assertIn("Configured include_paths: src, app, lib, docs", message)
            self.assertIn("documentation, packages", message)

    def test_parse_discovery_output_accepts_plain_json(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        payload = controller._parse_discovery_output(
            json.dumps(
                {
                    "type": "osteoblast",
                    "category": "hardening",
                    "scope": "packages/api",
                    "proof": ["Input is unvalidated."],
                    "candidate_files": ["packages/api/handler.ts"],
                    "why": "This should validate at the boundary.",
                    "estimated_change_size": {"files": 1, "lines": 12},
                    "confidence": 0.9,
                    "commit_title": "validate handler input",
                    "verification_hint": "npm test -- handler",
                }
            )
        )
        self.assertEqual(payload["scope"], "packages/api")

    def test_parse_discovery_output_accepts_fenced_json(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        payload = controller._parse_discovery_output(
            "Here is the finding:\n\n```json\n"
            + json.dumps(
                {
                    "type": "osteoblast",
                    "category": "docs",
                    "scope": "documentation",
                    "proof": ["README is stale."],
                    "candidate_files": ["documentation/README.md"],
                    "why": "Docs should match the code.",
                    "estimated_change_size": {"files": 1, "lines": 8},
                    "confidence": 0.95,
                    "commit_title": "fix stale readme",
                    "verification_hint": "N/A",
                }
            )
            + "\n```\n"
        )
        self.assertEqual(payload["category"], "docs")

    def test_parse_discovery_output_accepts_json_with_trailing_text(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        payload = controller._parse_discovery_output(
            json.dumps(
                {
                    "type": "osteoblast",
                    "category": "bugs",
                    "scope": "packages/functions",
                    "proof": ["A null check is missing."],
                    "candidate_files": ["packages/functions/index.ts"],
                    "why": "This can throw at runtime.",
                    "estimated_change_size": {"files": 1, "lines": 6},
                    "confidence": 0.88,
                    "commit_title": "add missing null check",
                    "verification_hint": "npm test -- functions",
                }
            )
            + "\n\nNotes: I stayed inside the requested scope."
        )
        self.assertEqual(payload["category"], "bugs")

    def test_discover_normalizes_category_alias_and_injects_allowed_categories(self) -> None:
        manifest = Manifest.from_mapping(
            {
                "version": "1",
                "base_branch": "main",
                "include_paths": ["src"],
                "exclude_paths": [],
                "allowed_categories": ["bugs", "dead-code", "docs"],
                "severity_rules": {"confidence_threshold": 0.75},
                "max_files_changed": 5,
                "max_changed_lines": 150,
                "verify": {"commands": ["python -m unittest"]},
                "pr": {"labels": ["osteoblast"], "reviewers": []},
                "schedule": {"enabled": True},
            }
        )

        def run_copilot(*, args, cwd=None, env=None, input_text=None, check=True):
            prompt = args[args.index("-p") + 1]
            self.assertIn("`dead-code`", prompt)
            self.assertIn("use `dead-code` instead of `dead tissue`", prompt.lower())
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps(
                    {
                        "type": "osteoclast",
                        "category": "dead tissue",
                        "scope": "src",
                        "proof": ["Unused fallback branch is unreachable."],
                        "candidate_files": ["src/legacy.py"],
                        "why": "This code is dead and increases maintenance burden.",
                        "estimated_change_size": {"files": 1, "lines": 9},
                        "confidence": 0.9,
                        "commit_title": "remove unused fallback branch",
                        "verification_hint": "python -m unittest",
                    }
                ),
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("git", "status", "--porcelain"), CommandResult(args=("git", "status", "--porcelain"), stdout="", stderr="", returncode=0)),
                (("copilot",), run_copilot),
            ]
        )
        controller = StubController(
            manifest=manifest,
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        finding = controller.discover(scope=ROOT / "src")
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.category, "dead-code")

    def test_prepare_copilot_environment_creates_session_state_root(self) -> None:
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            today=date(2026, 4, 10),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            copilot_home = Path(temp_dir) / "copilot-home"
            env = controller._prepare_copilot_environment(
                {"COPILOT_HOME": str(copilot_home), "OSTEOBLAST_SHOW_BANNER": "0"}
            )
            self.assertEqual(env["COPILOT_HOME"], str(copilot_home.resolve()))
            self.assertTrue((copilot_home / "session-state").exists())

    def test_run_copilot_wraps_command_error_with_context(self) -> None:
        runner = PrefixRunner(
            [
                (
                    ("copilot",),
                    CommandError(
                        ["copilot", "--agent", "osteoblast"],
                        1,
                        '{"status":"broken"}',
                        "stderr boom",
                    ),
                )
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(OsteoblastError) as context:
                controller._run_copilot(
                    agent="osteoblast",
                    prompt="test prompt",
                    extra_env={"COPILOT_HOME": str(Path(temp_dir) / "copilot-home")},
                )
        message = str(context.exception)
        self.assertIn("Copilot command failed for agent `osteoblast`", message)
        self.assertIn("stderr boom", message)
        self.assertIn('{"status":"broken"}', message)
        self.assertIn("COPILOT_HOME:", message)

    def test_run_copilot_recovers_from_session_persistence_failure(self) -> None:
        stderr = "\n".join(
            [
                "Failed to persist session events: Error: ENOENT",
                "Failed to persist session events: Error: ENOENT",
            ]
        )
        runner = PrefixRunner(
            [
                (
                    ("copilot",),
                    CommandError(
                        ["copilot", "--agent", "osteoblast"],
                        1,
                        '{"status":"no-finding"}',
                        stderr,
                    ),
                )
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        result = controller._run_copilot(agent="osteoblast", prompt="test prompt")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '{"status":"no-finding"}')

    def test_doctor_reports_missing_manifest_and_suggested_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / "packages").mkdir()
            (repo_root / "documentation").mkdir()
            controller = OsteoblastController(
                repo_root=repo_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            result = controller.doctor()
            self.assertEqual(result["status"], "error")
            checks = {check["name"]: check for check in result["checks"]}
            self.assertEqual(checks["manifest"]["status"], "error")
            self.assertTrue(any("packages" in suggestion for suggestion in result["suggestions"]))

    def test_doctor_reports_placeholder_verify_and_scope_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / ".git").mkdir()
            (repo_root / ".github" / "agents").mkdir(parents=True)
            (repo_root / ".github" / "hooks").mkdir(parents=True)
            (repo_root / ".github" / "skills" / "osteoblast-manifest-setup").mkdir(parents=True)
            (repo_root / ".github" / "workflows").mkdir(parents=True)
            (repo_root / "packages").mkdir()
            manifest_path = repo_root / ".github" / "osteoblast.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        'version = "1"',
                        'base_branch = "main"',
                        'include_paths = ["src", "docs"]',
                        'exclude_paths = []',
                        'allowed_categories = ["bugs"]',
                        'max_files_changed = 5',
                        'max_changed_lines = 150',
                        "",
                        "[severity_rules]",
                        "confidence_threshold = 0.75",
                        "",
                        "[verify]",
                        'commands = ["echo \\"Replace verify.commands in .github/osteoblast.toml with real repo checks\\" && exit 1"]',
                        "",
                        "[pr]",
                        'labels = ["osteoblast"]',
                        "reviewers = []",
                        "",
                        "[schedule]",
                        "enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            (repo_root / ".github" / "workflows" / "osteoblast.yml").write_text("name: test\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast.agent.md").write_text("---\nname: osteoblast\n---\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast-worker.agent.md").write_text("---\nname: osteoblast-worker\n---\n", encoding="utf-8")
            (repo_root / ".github" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            (
                repo_root / ".github" / "skills" / "osteoblast-manifest-setup" / "SKILL.md"
            ).write_text("---\nname: osteoblast-manifest-setup\n---\n", encoding="utf-8")

            controller = OsteoblastController(
                repo_root=repo_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            result = controller.doctor()
            self.assertEqual(result["status"], "error")
            checks = {check["name"]: check for check in result["checks"]}
            self.assertEqual(checks["manifest"]["status"], "ok")
            self.assertEqual(checks["manifest:include_paths"]["status"], "warn")
            self.assertEqual(checks["scope-selection"]["status"], "error")
            self.assertEqual(checks["manifest:verify"]["status"], "warn")

    def test_doctor_reports_healthy_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / ".git").mkdir()
            (repo_root / ".github" / "agents").mkdir(parents=True)
            (repo_root / ".github" / "hooks").mkdir(parents=True)
            (repo_root / ".github" / "skills" / "osteoblast-manifest-setup").mkdir(parents=True)
            (repo_root / ".github" / "workflows").mkdir(parents=True)
            (repo_root / "packages").mkdir()
            manifest_path = repo_root / ".github" / "osteoblast.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        'version = "1"',
                        'base_branch = "main"',
                        'include_paths = ["packages"]',
                        'exclude_paths = []',
                        'allowed_categories = ["bugs"]',
                        'max_files_changed = 5',
                        'max_changed_lines = 150',
                        "",
                        "[severity_rules]",
                        "confidence_threshold = 0.75",
                        "",
                        "[verify]",
                        'commands = ["python -m unittest"]',
                        "",
                        "[pr]",
                        'labels = ["osteoblast"]',
                        "reviewers = []",
                        "",
                        "[schedule]",
                        "enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            (repo_root / ".github" / "workflows" / "osteoblast.yml").write_text("name: test\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast.agent.md").write_text("---\nname: osteoblast\n---\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast-worker.agent.md").write_text("---\nname: osteoblast-worker\n---\n", encoding="utf-8")
            (repo_root / ".github" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            (
                repo_root / ".github" / "skills" / "osteoblast-manifest-setup" / "SKILL.md"
            ).write_text("---\nname: osteoblast-manifest-setup\n---\n", encoding="utf-8")

            controller = OsteoblastController(
                repo_root=repo_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            result = controller.doctor()
            self.assertEqual(result["status"], "ok")
            checks = {check["name"]: check for check in result["checks"]}
            self.assertEqual(checks["scope-selection"]["status"], "ok")
            self.assertEqual(checks["manifest:verify"]["status"], "ok")

    def test_doctor_fix_rewrites_include_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / ".git").mkdir()
            (repo_root / ".github" / "agents").mkdir(parents=True)
            (repo_root / ".github" / "hooks").mkdir(parents=True)
            (repo_root / ".github" / "skills" / "osteoblast-manifest-setup").mkdir(parents=True)
            (repo_root / ".github" / "workflows").mkdir(parents=True)
            (repo_root / "packages").mkdir()
            (repo_root / "documentation").mkdir()
            manifest_path = repo_root / ".github" / "osteoblast.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        'version = "1"',
                        'base_branch = "main"',
                        'include_paths = ["src", "docs"]',
                        'exclude_paths = []',
                        'allowed_categories = ["bugs"]',
                        'max_files_changed = 5',
                        'max_changed_lines = 150',
                        "",
                        "[severity_rules]",
                        "confidence_threshold = 0.75",
                        "",
                        "[verify]",
                        'commands = ["python -m unittest"]',
                        "",
                        "[pr]",
                        'labels = ["osteoblast"]',
                        "reviewers = []",
                        "",
                        "[schedule]",
                        "enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            (repo_root / ".github" / "workflows" / "osteoblast.yml").write_text("name: test\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast.agent.md").write_text("---\nname: osteoblast\n---\n", encoding="utf-8")
            (repo_root / ".github" / "agents" / "osteoblast-worker.agent.md").write_text("---\nname: osteoblast-worker\n---\n", encoding="utf-8")
            (repo_root / ".github" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
            (
                repo_root / ".github" / "skills" / "osteoblast-manifest-setup" / "SKILL.md"
            ).write_text("---\nname: osteoblast-manifest-setup\n---\n", encoding="utf-8")

            controller = OsteoblastController(
                repo_root=repo_root,
                core_root=ROOT,
                today=date(2026, 4, 10),
            )
            result = controller.doctor(fix=True)
            self.assertEqual(result["status"], "ok")
            self.assertIn("applied_fixes", result)
            self.assertEqual(result["applied_fixes"][0]["before"], ["src", "docs"])
            self.assertEqual(result["applied_fixes"][0]["after"], ["documentation", "packages"])
            rewritten = manifest_path.read_text(encoding="utf-8")
            self.assertIn('include_paths = ["documentation", "packages"]', rewritten)

    def test_create_tracking_issue_streams_body_via_stdin(self) -> None:
        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "label", "list"])
            label = args[args.index("--search") + 1]
            payload = [{"name": label}] if label == "osteoblast" else []
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps(payload),
                stderr="",
                returncode=0,
            )

        def run_label_create(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "label", "create"])
            self.assertEqual(args[3], "serious")
            return CommandResult(args=tuple(args), stdout="", stderr="", returncode=0)

        def run_issue_create(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "issue", "create"])
            self.assertIn("--title", args)
            self.assertIn("--body-file", args)
            self.assertIn("-", args)
            self.assertNotIn("--body", args)
            self.assertEqual(cwd, ROOT)
            assert input_text is not None
            self.assertIn("## Serious Osteoblast finding", input_text)
            self.assertIn('"severity": "serious"', input_text)
            self.assertIn("Input is used without validation.", input_text)
            return CommandResult(
                args=tuple(args),
                stdout="https://github.com/acme/repo/issues/42\n",
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("gh", "label", "list"), run_label_list),
                (("gh", "label", "create"), run_label_create),
                (("gh", "issue", "create"), run_issue_create),
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        issue_number = controller.create_tracking_issue(make_finding(severity="serious"))
        self.assertEqual(issue_number, 42)

    def test_open_pr_streams_body_via_stdin(self) -> None:
        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "label", "list"])
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps([{"name": "osteoblast"}]),
                stderr="",
                returncode=0,
            )

        def run_pr_create(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "pr", "create"])
            self.assertIn("--body-file", args)
            self.assertIn("-", args)
            self.assertNotIn("--body", args)
            self.assertEqual(cwd, ROOT)
            assert input_text is not None
            self.assertIn("## Summary", input_text)
            self.assertIn("## Verification", input_text)
            self.assertIn("python -m unittest", input_text)
            return CommandResult(
                args=tuple(args),
                stdout="https://github.com/acme/repo/pull/7\n",
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("git", "add", "--all"), CommandResult(args=("git", "add", "--all"), stdout="", stderr="", returncode=0)),
                (("git", "commit"), CommandResult(args=("git", "commit"), stdout="", stderr="", returncode=0)),
                (("git", "push"), CommandResult(args=("git", "push"), stdout="", stderr="", returncode=0)),
                (("gh", "label", "list"), run_label_list),
                (("gh", "pr", "create"), run_pr_create),
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        pr_url = controller.open_pr(
            finding=make_finding(),
            manifest=make_manifest(),
            branch_name="osteoblast/hardening/src-service/20260410",
            verification_commands=("python -m unittest",),
        )
        self.assertEqual(pr_url, "https://github.com/acme/repo/pull/7")

    def test_ensure_labels_exist_creates_missing_labels(self) -> None:
        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            self.assertEqual(args[:3], ["gh", "label", "list"])
            return CommandResult(
                args=tuple(args),
                stdout="[]",
                stderr="",
                returncode=0,
            )

        created: list[tuple[str, ...]] = []

        def run_label_create(*, args, cwd=None, env=None, input_text=None, check=True):
            created.append(tuple(args))
            return CommandResult(args=tuple(args), stdout="", stderr="", returncode=0)

        runner = PrefixRunner(
            [
                (("gh", "label", "list"), run_label_list),
                (("gh", "label", "create"), run_label_create),
            ]
        )
        controller = OsteoblastController(
            repo_root=ROOT,
            core_root=ROOT,
            runner=runner,
            today=date(2026, 4, 10),
        )
        controller.ensure_labels_exist(("osteoblast", "serious"))
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0][:4], ("gh", "label", "create", "osteoblast"))
        self.assertEqual(created[1][:4], ("gh", "label", "create", "serious"))

    def test_escalate_serious_finding_falls_back_to_copilot_assignee(self) -> None:
        issue_create = CommandResult(
            args=("gh", "issue", "create"),
            stdout="https://github.com/acme/repo/issues/42\n",
            stderr="",
            returncode=0,
        )
        issue_edit = CommandResult(args=("gh", "issue", "edit"), stdout="", stderr="", returncode=0)
        issue_comment = CommandResult(args=("gh", "issue", "comment"), stdout="", stderr="", returncode=0)

        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            label = args[args.index("--search") + 1]
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps([{"name": label}]),
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("gh", "label", "list"), run_label_list),
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

    def test_escalate_serious_finding_degrades_when_copilot_assignment_not_enabled(self) -> None:
        issue_create = CommandResult(
            args=("gh", "issue", "create"),
            stdout="https://github.com/acme/repo/issues/42\n",
            stderr="",
            returncode=0,
        )
        issue_comment = CommandResult(args=("gh", "issue", "comment"), stdout="", stderr="", returncode=0)

        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            label = args[args.index("--search") + 1]
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps([{"name": label}]),
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("gh", "label", "list"), run_label_list),
                (("gh", "issue", "create"), issue_create),
                (
                    ("gh", "agent-task", "create"),
                    CommandError(["gh", "agent-task", "create"], 1, "", "boom"),
                ),
                (
                    ("gh", "issue", "edit"),
                    CommandError(
                        ["gh", "issue", "edit", "42", "--add-assignee", "@copilot"],
                        1,
                        "",
                        "GraphQL: Copilot agent is not enabled in this repository. (replaceActorsForAssignable)",
                    ),
                ),
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
        self.assertEqual(result["mode"], "issue-only")
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

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

        def run_label_list(*, args, cwd=None, env=None, input_text=None, check=True):
            label = args[args.index("--search") + 1]
            return CommandResult(
                args=tuple(args),
                stdout=json.dumps([{"name": label}]),
                stderr="",
                returncode=0,
            )

        runner = PrefixRunner(
            [
                (("gh", "label", "list"), run_label_list),
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
