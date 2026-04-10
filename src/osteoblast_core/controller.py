from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import hashlib
import json
import os
import re
from typing import Any

from .commands import CommandError, CommandRunner
from .models import Finding, Manifest, OsteoblastError
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

    def pick_scope(self, manifest: Manifest) -> Path:
        eligible = [
            path
            for path in manifest.allowed_scope_paths(self.repo_root)
            if not manifest.scope_is_excluded(self.repo_root, path)
        ]
        if not eligible:
            raise OsteoblastError("No eligible scopes were found in `.github/osteoblast.toml`.")
        ordering = sorted(
            eligible,
            key=lambda path: hashlib.sha256(
                f"{self.today.isoformat()}::{path.relative_to(self.repo_root).as_posix()}".encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        return ordering[0]

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

    def ensure_clean_worktree(self) -> None:
        result = self.runner.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_root,
        )
        if result.stdout.strip():
            raise OsteoblastError("Repository worktree must be clean before running Osteoblast automation.")

    def discover(self, *, scope: Path | None = None) -> Finding | None:
        manifest = self.load_manifest()
        chosen_scope = scope or self.pick_scope(manifest)
        relative_scope = chosen_scope.relative_to(self.repo_root).as_posix()
        self.ensure_clean_worktree()
        prompt = (
            "Discovery mode only. Analyze the repository scope "
            f"`{relative_scope}` and respond with JSON only. "
            "Use the osteoblast-finding-contract skill. "
            "Do not edit repository files or run mutating commands. "
            "Return exactly one finding object, or the documented no-finding object if nothing acceptable exists."
        )
        env = os.environ | {
            "OSTEOBLAST_READ_ONLY": "1",
            "OSTEOBLAST_SHOW_BANNER": "0",
        }
        result = self.runner.run(
            [
                "copilot",
                "--plugin-dir",
                str(self.core_root),
                "--agent",
                "osteoblast",
                "-p",
                prompt,
                "--allow-all-tools",
                "--no-ask-user",
                "-s",
            ],
            cwd=self.repo_root,
            env=env,
        )
        self.ensure_clean_worktree()
        payload = json.loads(result.stdout)
        if payload.get("status") == "no-finding":
            return None
        finding = Finding.from_dict(payload)
        if finding.category not in manifest.allowed_categories:
            raise OsteoblastError(
                f"Discovered category `{finding.category}` is not allowed by the manifest."
            )
        return finding.classify(manifest)

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
        env = os.environ | {
            "OSTEOBLAST_SHOW_BANNER": "0",
            "OSTEOBLAST_FINDING_SEVERITY": finding.severity or "routine",
        }
        result = self.runner.run(
            [
                "copilot",
                "--plugin-dir",
                str(self.core_root),
                "--agent",
                "osteoblast-worker",
                "-p",
                prompt,
                "--allow-all-tools",
                "--no-ask-user",
                "-s",
            ],
            cwd=self.repo_root,
            env=env,
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
