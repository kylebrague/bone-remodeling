#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from osteoblast_core.controller import OsteoblastController  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Osteoblast repository automation controller.")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to operate on. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--core-root",
        default=str(REPO_ROOT),
        help="Path to the Osteoblast core repository.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap", help="Render target-repo overlays from templates.")
    bootstrap.add_argument("--target-root", default=".", help="Target repository root.")
    bootstrap.add_argument("--core-repository", required=True, help="GitHub OWNER/REPO for this core repository.")
    bootstrap.add_argument("--core-ref", default="main", help="Pinned ref for the core checkout in workflows.")
    bootstrap.add_argument("--gh-token-secret", default="OSTEOBLAST_GH_TOKEN")
    bootstrap.add_argument("--copilot-token-secret", default="OSTEOBLAST_COPILOT_TOKEN")
    bootstrap.add_argument("--force", action="store_true", help="Overwrite existing overlay files.")

    discover = subparsers.add_parser("discover", help="Run discovery for one scope.")
    discover.add_argument("--scope", help="Optional relative scope path to analyze.")

    execute = subparsers.add_parser("execute", help="Execute one finding payload from JSON.")
    execute.add_argument("--finding-file", required=True, help="Path to a JSON file containing the finding payload.")

    subparsers.add_parser("verify", help="Run repository verification commands from the manifest.")

    open_pr = subparsers.add_parser("open-pr", help="Commit and open a PR for a finding.")
    open_pr.add_argument("--finding-file", required=True, help="Path to a JSON file containing the finding payload.")
    open_pr.add_argument("--branch-name", required=True, help="Existing branch name to open the PR from.")
    open_pr.add_argument(
        "--verification-command",
        action="append",
        default=[],
        help="Verification command to include in the PR body. Can be passed multiple times.",
    )

    subparsers.add_parser("run-scheduled", help="Run the full scheduled discovery/remediation flow.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    core_root = Path(args.core_root).resolve()
    controller = OsteoblastController(repo_root=repo_root, core_root=core_root)

    if args.command == "bootstrap":
        created = controller.bootstrap(
            target_root=Path(args.target_root).resolve(),
            core_repository=args.core_repository,
            core_ref=args.core_ref,
            gh_token_secret=args.gh_token_secret,
            copilot_token_secret=args.copilot_token_secret,
            force=args.force,
        )
        print(json.dumps({"status": "ok", "files": [str(path) for path in created]}, indent=2))
        return 0

    if args.command == "discover":
        scope = (repo_root / args.scope).resolve() if args.scope else None
        finding = controller.discover(scope=scope)
        print(json.dumps(controller._finding_payload(finding), indent=2) if finding else json.dumps({"status": "no-finding"}))
        return 0

    if args.command == "execute":
        from osteoblast_core.models import Finding  # noqa: E402

        payload = json.loads(Path(args.finding_file).read_text(encoding="utf-8"))
        report = controller.execute(Finding.from_dict(payload))
        print(report)
        return 0

    if args.command == "verify":
        manifest = controller.load_manifest()
        commands = controller.verify(manifest)
        print(json.dumps({"status": "ok", "commands": list(commands)}, indent=2))
        return 0

    if args.command == "open-pr":
        from osteoblast_core.models import Finding  # noqa: E402

        payload = json.loads(Path(args.finding_file).read_text(encoding="utf-8"))
        finding = Finding.from_dict(payload)
        manifest = controller.load_manifest()
        pr_url = controller.open_pr(
            finding=finding,
            manifest=manifest,
            branch_name=args.branch_name,
            verification_commands=tuple(args.verification_command),
        )
        print(json.dumps({"status": "ok", "pr_url": pr_url}, indent=2))
        return 0

    if args.command == "run-scheduled":
        print(json.dumps(controller.run_scheduled(), indent=2))
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
