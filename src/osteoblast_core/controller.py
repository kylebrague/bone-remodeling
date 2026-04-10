from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import tempfile
from typing import Any, Mapping

from .commands import CommandError, CommandRunner, CommandResult
from .models import Finding, Manifest, ManifestError, OsteoblastError
from .templates import copy_paths, render_tree


SERIOUS_LABELS = ("osteoblast", "serious")
ROUTINE_LABEL = "osteoblast"


@dataclass(frozen=True)
class DiffStats:
    files: tuple[str, ...]
    file_count: int
    changed_lines: int


def _slugify(value: str) -> str:
    lowered = value.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "scope"


def _extract_issue_number(url: str) -> int:
    match = re.search(r"/issues/(\d+)$", url.strip())
    if not match:
        raise OsteoblastError(f"Could not extract issue number from URL: {url}")
    return int(match.group(1))


def _looks_like_discovery_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("status") == "no-finding":
        return True
    required = {
        "type",
        "category",
        "scope",
        "proof",
        "candidate_files",
        "why",
        "estimated_change_size",
        "commit_title",
        "verification_hint",
    }
    return required.issubset(value.keys())


def _is_session_persistence_failure(stderr: str) -> bool:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return False
    return all(line.startswith("Failed to persist session events:") for line in lines)


class OsteoblastController:
    def __init__(
        self,
        *,
        repo_root: Path,
        core_root: Path,
        runner: CommandRunner | None = None,
        today: date | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.core_root = core_root.resolve()
        self.runner = runner or CommandRunner()
        self.today = today or date.today()

    def bootstrap(
        self,
        *,
        target_root: Path | None = None,
        core_repository: str,
        core_ref: str,
        gh_token_secret: str = "OSTEOBLAST_GH_TOKEN",
        copilot_token_secret: str = "OSTEOBLAST_COPILOT_TOKEN",
        force: bool = False,
    ) -> list[Path]:
        target = (target_root or self.repo_root).resolve()
        template_root = self.core_root / "templates" / "target-repo"
        context = {
            "core_repository": core_repository,
            "core_ref": core_ref,
            "gh_token_expression": f"${{{{ secrets.{gh_token_secret} }}}}",
            "copilot_token_expression": f"${{{{ secrets.{copilot_token_secret} }}}}",
        }
        created = render_tree(template_root, target, context=context, force=force)
        hook_script_source = self.core_root / "hooks" / "scripts"
        hook_script_pairs = [
            (
                source,
                target / ".github" / "hooks" / "scripts" / source.name,
            )
            for source in sorted(hook_script_source.glob("*.py"))
        ]
        created.extend(copy_paths(hook_script_pairs, force=force))
        return created

    def load_manifest(self) -> Manifest:
        manifest_path = self.repo_root / ".github" / "osteoblast.toml"
        return Manifest.from_path(manifest_path)

    def doctor(self, *, fix: bool = False) -> dict[str, Any]:
        applied_fixes: list[dict[str, Any]] = []

        if fix:
            manifest_path = self.repo_root / ".github" / "osteoblast.toml"
            if manifest_path.exists():
                try:
                    manifest = self.load_manifest()
                except ManifestError:
                    manifest = None
                if manifest is not None:
                    configured = tuple(manifest.include_paths)
                    missing_include_paths = [
                        relative
                        for relative in manifest.include_paths
                        if not (self.repo_root / relative).exists()
                    ]
                    has_eligible_scopes = any(
                        not manifest.scope_is_excluded(self.repo_root, path)
                        for path in manifest.allowed_scope_paths(self.repo_root)
                    )
                    suggested_paths = self._suggest_fix_include_paths()
                    if suggested_paths and (missing_include_paths or not has_eligible_scopes):
                        if self._rewrite_manifest_include_paths(manifest_path, suggested_paths):
                            applied_fixes.append(
                                {
                                    "name": "manifest:include_paths",
                                    "detail": "Rewrote include_paths to detected repo directories.",
                                    "before": list(configured),
                                    "after": list(suggested_paths),
                                    "path": str(manifest_path),
                                }
                            )

        result = self._doctor_report()
        if applied_fixes:
            result["applied_fixes"] = applied_fixes
        return result

    def _doctor_report(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        suggestions: list[str] = []

        def add_check(name: str, status: str, detail: str, **extra: Any) -> None:
            payload: dict[str, Any] = {"name": name, "status": status, "detail": detail}
            payload.update(extra)
            checks.append(payload)

        add_check(
            "repo-root",
            "ok" if self.repo_root.exists() else "error",
            f"Using repo root `{self.repo_root}`.",
            path=str(self.repo_root),
        )

        git_dir = self.repo_root / ".git"
        add_check(
            "git-repo",
            "ok" if git_dir.exists() else "warn",
            "Git metadata found." if git_dir.exists() else "`.git` was not found at repo root.",
            path=str(git_dir),
        )

        core_plugin = self.core_root / "plugin.json"
        add_check(
            "core-root",
            "ok" if core_plugin.exists() else "error",
            "Core plugin manifest found."
            if core_plugin.exists()
            else "Core root does not look like the Osteoblast repo; `plugin.json` is missing.",
            path=str(self.core_root),
        )

        for command_name in ("git", "gh", "copilot", "python3"):
            location = shutil.which(command_name)
            add_check(
                f"command:{command_name}",
                "ok" if location else "error",
                f"`{command_name}` is available at `{location}`."
                if location
                else f"`{command_name}` is not on PATH.",
                path=location,
            )

        workflow_path = self.repo_root / ".github" / "workflows" / "osteoblast.yml"
        add_check(
            "workflow",
            "ok" if workflow_path.exists() else "warn",
            "Workflow file found."
            if workflow_path.exists()
            else "Missing `.github/workflows/osteoblast.yml`; run bootstrap or add the workflow manually.",
            path=str(workflow_path),
        )

        for relative, name in (
            (".github/agents/osteoblast.agent.md", "agent:osteoblast"),
            (".github/agents/osteoblast-worker.agent.md", "agent:osteoblast-worker"),
            (".github/skills/osteoblast-manifest-setup/SKILL.md", "skill:osteoblast-manifest-setup"),
            (".github/hooks/hooks.json", "hooks"),
        ):
            path = self.repo_root / relative
            add_check(
                name,
                "ok" if path.exists() else "warn",
                f"`{relative}` found."
                if path.exists()
                else f"`{relative}` is missing from the target repo overlay.",
                path=str(path),
            )

        manifest_path = self.repo_root / ".github" / "osteoblast.toml"
        if not manifest_path.exists():
            add_check(
                "manifest",
                "error",
                "Missing `.github/osteoblast.toml`; run bootstrap first.",
                path=str(manifest_path),
            )
            suggested = self._suggest_scope_paths()
            if suggested:
                suggestions.append(
                    "Suggested include_paths based on repo layout: " + ", ".join(suggested)
                )
            return self._doctor_result(checks, suggestions)

        try:
            manifest = self.load_manifest()
        except ManifestError as exc:
            add_check("manifest", "error", str(exc), path=str(manifest_path))
            suggested = self._suggest_scope_paths()
            if suggested:
                suggestions.append(
                    "Suggested include_paths based on repo layout: " + ", ".join(suggested)
                )
            return self._doctor_result(checks, suggestions)

        add_check("manifest", "ok", "Manifest parsed successfully.", path=str(manifest_path))

        missing_include_paths = [
            relative
            for relative in manifest.include_paths
            if not (self.repo_root / relative).exists()
        ]
        if missing_include_paths:
            add_check(
                "manifest:include_paths",
                "warn",
                "Some configured include_paths do not exist.",
                configured=list(manifest.include_paths),
                missing=missing_include_paths,
            )
            suggestions.append(
                "Update include_paths to existing directories such as: "
                + ", ".join(self._suggest_scope_paths() or ("<none>",))
            )
        else:
            add_check(
                "manifest:include_paths",
                "ok",
                "All configured include_paths exist.",
                configured=list(manifest.include_paths),
            )

        try:
            chosen_scope = self.pick_scope(manifest)
            add_check(
                "scope-selection",
                "ok",
                f"One eligible scope is `{chosen_scope.relative_to(self.repo_root).as_posix()}`.",
            )
        except OsteoblastError as exc:
            add_check("scope-selection", "error", str(exc))

        if any("Replace verify.commands" in command for command in manifest.verify.commands):
            add_check(
                "manifest:verify",
                "warn",
                "Manifest still contains the placeholder verify command. Replace it with real repo checks.",
                commands=list(manifest.verify.commands),
            )
            suggestions.append("Replace `verify.commands` with real lint/test/typecheck commands.")
        else:
            add_check(
                "manifest:verify",
                "ok",
                "verify.commands is configured.",
                commands=list(manifest.verify.commands),
            )

        return self._doctor_result(checks, suggestions)

    def pick_scope(self, manifest: Manifest) -> Path:
        eligible = [
            path
            for path in manifest.allowed_scope_paths(self.repo_root)
            if not manifest.scope_is_excluded(self.repo_root, path)
        ]
        if not eligible:
            configured = ", ".join(manifest.include_paths) or "<none>"
            suggestions = ", ".join(self._suggest_scope_paths()) or "<none>"
            raise OsteoblastError(
                "No eligible scopes were found in `.github/osteoblast.toml`. "
                f"Configured include_paths: {configured}. "
                f"Existing top-level candidates in this repo: {suggestions}. "
                "Update `.github/osteoblast.toml` include_paths or pass `--scope <path>`."
            )
        ordering = sorted(
            eligible,
            key=lambda path: hashlib.sha256(
                f"{self.today.isoformat()}::{path.relative_to(self.repo_root).as_posix()}".encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        return ordering[0]

    def _suggest_scope_paths(self) -> tuple[str, ...]:
        preferred = (
            "src",
            "app",
            "lib",
            "docs",
            "documentation",
            "packages",
            "services",
            "internal",
            "cmd",
            "specs",
            "infra",
        )
        ignored = {
            ".git",
            ".github",
            ".codex",
            ".claude",
            ".vscode",
            "node_modules",
            "dist",
            "build",
            "vendor",
        }
        children = {path.name: path for path in self.repo_root.iterdir()}
        ordered = [name for name in preferred if name in children and name not in ignored]
        extras = sorted(
            name
            for name, path in children.items()
            if path.is_dir() and name not in ignored and name not in ordered and not name.startswith(".")
        )
        return tuple((ordered + extras)[:8])

    def _suggest_fix_include_paths(self) -> tuple[str, ...]:
        safe_preferred = (
            "src",
            "app",
            "lib",
            "docs",
            "documentation",
            "packages",
            "services",
            "internal",
            "cmd",
            "specs",
        )
        children = {path.name: path for path in self.repo_root.iterdir()}
        suggested = tuple(
            name for name in safe_preferred if name in children and children[name].is_dir()
        )
        if suggested:
            return suggested
        fallback = tuple(
            name for name in self._suggest_scope_paths() if name not in {"infra", "ralph"}
        )
        return fallback or self._suggest_scope_paths()

    def _rewrite_manifest_include_paths(
        self,
        manifest_path: Path,
        include_paths: tuple[str, ...],
    ) -> bool:
        replacement = "include_paths = [" + ", ".join(json.dumps(path) for path in include_paths) + "]"
        original = manifest_path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^include_paths\s*=\s*\[[^\n]*\]\s*$",
            replacement,
            original,
            count=1,
        )
        if count == 0:
            base_branch_pattern = re.compile(r"(?m)^base_branch\s*=\s*[^\n]+$")
            match = base_branch_pattern.search(original)
            if not match:
                raise OsteoblastError(
                    "Could not rewrite include_paths because neither `include_paths` nor `base_branch` was found."
                )
            insertion = match.group(0) + "\n" + replacement
            updated = original[: match.start()] + insertion + original[match.end() :]
        if updated == original:
            return False
        manifest_path.write_text(updated, encoding="utf-8")
        return True

    @staticmethod
    def _doctor_result(
        checks: list[dict[str, Any]],
        suggestions: list[str],
    ) -> dict[str, Any]:
        statuses = [check["status"] for check in checks]
        overall = "ok"
        if any(status == "error" for status in statuses):
            overall = "error"
        elif any(status == "warn" for status in statuses):
            overall = "warn"
        return {
            "status": overall,
            "checks": checks,
            "suggestions": suggestions,
        }

    def has_open_routine_pr(self) -> bool:
        result = self.runner.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--label",
                ROUTINE_LABEL,
                "--json",
                "number,title,url",
            ],
            cwd=self.repo_root,
        )
        prs = json.loads(result.stdout or "[]")
        return bool(prs)

    def has_open_serious_issue(self) -> bool:
        result = self.runner.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                SERIOUS_LABELS[0],
                "--label",
                SERIOUS_LABELS[1],
                "--json",
                "number,title,url",
            ],
            cwd=self.repo_root,
        )
        issues = json.loads(result.stdout or "[]")
        return bool(issues)

    def _nested_core_checkout_pathspec(self) -> str | None:
        if self.core_root == self.repo_root:
            return None
        try:
            relative = self.core_root.relative_to(self.repo_root)
        except ValueError:
            return None
        return relative.as_posix()

    def ensure_clean_worktree(self) -> None:
        command = ["git", "status", "--porcelain"]
        nested_core_path = self._nested_core_checkout_pathspec()
        if nested_core_path:
            command.extend(["--", ".", f":(exclude){nested_core_path}"])
        result = self.runner.run(command, cwd=self.repo_root)
        if result.stdout.strip():
            raise OsteoblastError("Repository worktree must be clean before running Osteoblast automation.")

    def _prepare_copilot_environment(
        self,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)

        configured_home = env.get("COPILOT_HOME")
        copilot_home = (
            Path(configured_home).expanduser().resolve()
            if configured_home
            else (Path.home() / ".copilot").resolve()
        )
        session_state_root = copilot_home / "session-state"
        try:
            session_state_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback_home = (
                Path(tempfile.gettempdir())
                / f"osteoblast-copilot-{hashlib.sha256(str(self.repo_root).encode('utf-8')).hexdigest()[:12]}"
            )
            (fallback_home / "session-state").mkdir(parents=True, exist_ok=True)
            env["COPILOT_HOME"] = str(fallback_home)
        else:
            env["COPILOT_HOME"] = str(copilot_home)
        return env

    @staticmethod
    def _preview_output(text: str, *, limit: int = 2000) -> str:
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[:limit] + "\n...[truncated]"

    def _run_copilot(
        self,
        *,
        agent: str,
        prompt: str,
        extra_env: Mapping[str, str] | None = None,
    ):
        env = self._prepare_copilot_environment(extra_env)
        try:
            return self.runner.run(
                [
                    "copilot",
                    "--plugin-dir",
                    str(self.core_root),
                    "--agent",
                    agent,
                    "-p",
                    prompt,
                    "--allow-all-tools",
                    "--no-ask-user",
                    "-s",
                ],
                cwd=self.repo_root,
                env=env,
            )
        except CommandError as exc:
            if exc.stdout.strip() and _is_session_persistence_failure(exc.stderr):
                return CommandResult(
                    args=exc.command,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    returncode=0,
                )
            details: list[str] = [f"COPILOT_HOME: {env['COPILOT_HOME']}"]
            if exc.stderr.strip():
                details.append("stderr:\n" + self._preview_output(exc.stderr))
            if exc.stdout.strip():
                details.append("stdout:\n" + self._preview_output(exc.stdout))
            raise OsteoblastError(
                f"Copilot command failed for agent `{agent}` with exit code {exc.returncode}.\n\n"
                + "\n\n".join(details)
            ) from exc

    def discover(self, *, scope: Path | None = None) -> Finding | None:
        manifest = self.load_manifest()
        chosen_scope = scope or self.pick_scope(manifest)
        relative_scope = chosen_scope.relative_to(self.repo_root).as_posix()
        self.ensure_clean_worktree()
        allowed_categories = ", ".join(f"`{category}`" for category in manifest.allowed_categories)
        prompt = (
            "Discovery mode only. Analyze the repository scope "
            f"`{relative_scope}` and respond with JSON only. "
            "Use the osteoblast-finding-contract skill. "
            f"Set `category` to exactly one of: {allowed_categories}. "
            "Use canonical machine-safe slugs only, not prose or metaphors. "
            "For example, use `dead-code` instead of `dead tissue`. "
            "Do not edit repository files or run mutating commands. "
            "Return exactly one finding object, or the documented no-finding object if nothing acceptable exists."
        )
        env = {
            "OSTEOBLAST_READ_ONLY": "1",
            "OSTEOBLAST_SHOW_BANNER": "0",
        }
        result = self._run_copilot(agent="osteoblast", prompt=prompt, extra_env=env)
        self.ensure_clean_worktree()
        payload = self._parse_discovery_output(result.stdout)
        if payload.get("status") == "no-finding":
            return None
        finding = Finding.from_dict(payload)
        if finding.category not in manifest.allowed_categories:
            raise OsteoblastError(
                "Discovered category "
                f"`{finding.category}` is not allowed by the manifest. "
                f"Allowed categories: {', '.join(manifest.allowed_categories)}."
            )
        return finding.classify(manifest)

    def _parse_discovery_output(self, stdout: str) -> dict[str, Any]:
        text = stdout.strip()
        if not text:
            raise OsteoblastError("Copilot returned empty output for discovery.")

        direct = self._try_parse_json_document(text)
        if direct is not None:
            return direct

        fenced_match = re.search(r"```json\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced_match:
            fenced = self._try_parse_json_document(fenced_match.group(1).strip())
            if fenced is not None:
                return fenced

        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char not in "{[":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if _looks_like_discovery_payload(candidate):
                return candidate

        preview = text[:500]
        raise OsteoblastError(
            "Could not parse a valid discovery JSON payload from Copilot output. "
            f"Output preview: {preview}"
        )

    @staticmethod
    def _try_parse_json_document(text: str) -> dict[str, Any] | None:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return value if _looks_like_discovery_payload(value) else None

    def create_routine_branch(self, finding: Finding, manifest: Manifest) -> str:
        branch = self.branch_name_for(finding)
        self.runner.run(["git", "fetch", "origin", manifest.base_branch], cwd=self.repo_root)
        self.runner.run(
            ["git", "checkout", "-B", branch, f"origin/{manifest.base_branch}"],
            cwd=self.repo_root,
        )
        return branch

    def execute(self, finding: Finding) -> str:
        prompt = (
            "You are executing one approved Osteoblast finding. "
            "Use the osteoblast-worker agent instructions exactly. "
            "Finding payload follows as JSON:\n\n"
            f"{json.dumps(self._finding_payload(finding), indent=2)}\n\n"
            "Stay inside the approved scope and report using the worker response contract."
        )
        env = {
            "OSTEOBLAST_SHOW_BANNER": "0",
            "OSTEOBLAST_FINDING_SEVERITY": finding.severity or "routine",
        }
        result = self._run_copilot(
            agent="osteoblast-worker",
            prompt=prompt,
            extra_env=env,
        )
        return result.stdout.strip()

    def verify(self, manifest: Manifest) -> tuple[str, ...]:
        executed: list[str] = []
        for command in manifest.verify.commands:
            self.runner.run(["bash", "-lc", command], cwd=self.repo_root)
            executed.append(command)
        return tuple(executed)

    def diff_stats(self) -> DiffStats:
        result = self.runner.run(["git", "diff", "--numstat"], cwd=self.repo_root)
        changed_files: list[str] = []
        changed_lines = 0
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            changed_files.append(path)
            if added.isdigit():
                changed_lines += int(added)
            else:
                changed_lines += 999
            if deleted.isdigit():
                changed_lines += int(deleted)
            else:
                changed_lines += 999
        return DiffStats(
            files=tuple(changed_files),
            file_count=len(changed_files),
            changed_lines=changed_lines,
        )

    def validate_routine_diff(self, finding: Finding, manifest: Manifest) -> DiffStats:
        stats = self.diff_stats()
        if stats.file_count == 0:
            raise OsteoblastError("Routine execution produced no changes.")
        if stats.file_count > manifest.max_files_changed:
            raise OsteoblastError("Routine execution exceeded the allowed changed-file budget.")
        if stats.changed_lines > manifest.max_changed_lines:
            raise OsteoblastError("Routine execution exceeded the allowed changed-line budget.")
        if finding.violates_local_routine_policy(manifest, stats.files):
            raise OsteoblastError("Routine execution touched files reserved for serious escalation.")
        return stats

    def branch_name_for(self, finding: Finding) -> str:
        return "/".join(
            [
                "osteoblast",
                _slugify(finding.category),
                _slugify(finding.scope),
                self.today.strftime("%Y%m%d"),
            ]
        )

    def commit_message_for(self, finding: Finding) -> str:
        return f"chore(osteoblast): {finding.commit_title}"

    def pr_title_for(self, finding: Finding) -> str:
        return self.commit_message_for(finding)

    def pr_body_for(self, finding: Finding, verification_commands: tuple[str, ...]) -> str:
        body = [
            "## Summary",
            "",
            finding.why,
            "",
            "## Evidence",
            "",
        ]
        body.extend(f"- {item}" for item in finding.proof)
        body.extend(["", "## Verification", ""])
        body.extend(f"- `{command}`" for command in verification_commands)
        return "\n".join(body)

    def open_pr(
        self,
        *,
        finding: Finding,
        manifest: Manifest,
        branch_name: str,
        verification_commands: tuple[str, ...],
    ) -> str:
        self.runner.run(["git", "add", "--all"], cwd=self.repo_root)
        self.runner.run(
            ["git", "commit", "-m", self.commit_message_for(finding)],
            cwd=self.repo_root,
        )
        self.runner.run(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=self.repo_root,
        )
        command = [
            "gh",
            "pr",
            "create",
            "--base",
            manifest.base_branch,
            "--head",
            branch_name,
            "--title",
            self.pr_title_for(finding),
            "--body",
            self.pr_body_for(finding, verification_commands),
        ]
        for label in manifest.pr.labels:
            command.extend(["--label", label])
        for reviewer in manifest.pr.reviewers:
            command.extend(["--reviewer", reviewer])
        result = self.runner.run(command, cwd=self.repo_root)
        return result.stdout.strip()

    def create_tracking_issue(self, finding: Finding) -> int:
        title = f"[Osteoblast serious] {finding.commit_title}"
        body = "\n".join(
            [
                "## Serious Osteoblast finding",
                "",
                finding.why,
                "",
                "## Evidence",
                "",
                *[f"- {item}" for item in finding.proof],
                "",
                "## Finding payload",
                "",
                "```json",
                json.dumps(self._finding_payload(finding), indent=2),
                "```",
            ]
        )
        result = self.runner.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--label",
                SERIOUS_LABELS[0],
                "--label",
                SERIOUS_LABELS[1],
            ],
            cwd=self.repo_root,
        )
        return _extract_issue_number(result.stdout.strip())

    def start_cloud_agent(self, finding: Finding, issue_number: int, manifest: Manifest) -> dict[str, Any] | None:
        prompt = "\n".join(
            [
                f"A scheduled Osteoblast scan created tracking issue #{issue_number} for a serious repository-maintenance problem.",
                "Read the issue, verify the finding, fix only this problem, run the narrowest relevant verification, and open a PR.",
                "Do not broaden the scope beyond the issue and finding payload.",
                "",
                "Finding payload:",
                json.dumps(self._finding_payload(finding), indent=2),
            ]
        )
        self.runner.run(
            [
                "gh",
                "agent-task",
                "create",
                prompt,
                "--custom-agent",
                "osteoblast",
                "--base",
                manifest.base_branch,
            ],
            cwd=self.repo_root,
        )
        tasks = self.runner.run(
            [
                "gh",
                "agent-task",
                "list",
                "--limit",
                "5",
                "--json",
                "id,pullRequestNumber,pullRequestUrl,repository,state,createdAt",
            ],
            cwd=self.repo_root,
        )
        items = json.loads(tasks.stdout or "[]")
        return items[0] if items else None

    def comment_on_issue(self, issue_number: int, body: str) -> None:
        self.runner.run(
            ["gh", "issue", "comment", str(issue_number), "--body", body],
            cwd=self.repo_root,
        )

    def fallback_assign_copilot(self, issue_number: int) -> None:
        self.runner.run(
            ["gh", "issue", "edit", str(issue_number), "--add-assignee", "@copilot"],
            cwd=self.repo_root,
        )

    def escalate_serious_finding(self, finding: Finding, manifest: Manifest) -> dict[str, Any]:
        issue_number = self.create_tracking_issue(finding)
        summary: dict[str, Any] = {"issue_number": issue_number, "mode": "issue-only"}
        try:
            task = self.start_cloud_agent(finding, issue_number, manifest)
            if task:
                summary["mode"] = "agent-task"
                summary["task"] = task
                pr_url = task.get("pullRequestUrl")
                if pr_url:
                    self.comment_on_issue(
                        issue_number,
                        f"GitHub Copilot cloud agent started a remediation task and created {pr_url}.",
                    )
                else:
                    self.comment_on_issue(
                        issue_number,
                        "GitHub Copilot cloud agent started a remediation task for this issue.",
                    )
            else:
                self.comment_on_issue(
                    issue_number,
                    "GitHub Copilot cloud agent task started, but no task metadata was available immediately.",
                )
        except CommandError:
            self.fallback_assign_copilot(issue_number)
            summary["mode"] = "copilot-assignee"
            self.comment_on_issue(
                issue_number,
                "Fell back to assigning @copilot because `gh agent-task create` was unavailable or failed.",
            )
        return summary

    def run_scheduled(self) -> dict[str, Any]:
        manifest = self.load_manifest()
        if not manifest.schedule.enabled:
            return {"status": "skipped", "reason": "schedule-disabled"}
        if self.has_open_routine_pr():
            return {"status": "skipped", "reason": "open-osteoblast-pr"}
        if self.has_open_serious_issue():
            return {"status": "skipped", "reason": "active-serious-escalation"}

        finding = self.discover()
        if finding is None:
            return {"status": "no-finding"}

        if finding.severity == "serious":
            escalation = self.escalate_serious_finding(finding, manifest)
            return {"status": "serious", "finding": self._finding_payload(finding), **escalation}

        branch_name = self.create_routine_branch(finding, manifest)
        worker_report = self.execute(finding)
        verification_commands = self.verify(manifest)
        stats = self.validate_routine_diff(finding, manifest)
        pr_url = self.open_pr(
            finding=finding,
            manifest=manifest,
            branch_name=branch_name,
            verification_commands=verification_commands,
        )
        return {
            "status": "routine",
            "branch": branch_name,
            "pr_url": pr_url,
            "worker_report": worker_report,
            "changed_files": list(stats.files),
            "changed_lines": stats.changed_lines,
            "finding": self._finding_payload(finding),
        }

    @staticmethod
    def _finding_payload(finding: Finding) -> dict[str, Any]:
        return {
            "severity": finding.severity,
            "type": finding.type,
            "category": finding.category,
            "scope": finding.scope,
            "proof": list(finding.proof),
            "candidate_files": list(finding.candidate_files),
            "why": finding.why,
            "estimated_change_size": {
                "files": finding.estimated_change_size.files,
                "lines": finding.estimated_change_size.lines,
            },
            "confidence": finding.confidence,
            "commit_title": finding.commit_title,
            "verification_hint": finding.verification_hint,
        }
