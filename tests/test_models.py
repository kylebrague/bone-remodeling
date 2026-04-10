from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from osteoblast_core.models import Finding, Manifest, canonicalize_category


def make_manifest() -> Manifest:
    return Manifest.from_mapping(
        {
            "version": "1",
            "base_branch": "main",
            "include_paths": ["src", "docs"],
            "exclude_paths": ["dist/**"],
            "allowed_categories": ["bugs", "hardening", "docs"],
            "severity_rules": {
                "confidence_threshold": 0.8,
                "serious_path_keywords": ["security", ".github/workflows"],
                "public_api_globs": ["api/**", "**/*.proto"],
                "forbidden_local_globs": [".github/workflows/**"],
            },
            "max_files_changed": 5,
            "max_changed_lines": 150,
            "verify": {"commands": ["python -m unittest"]},
            "pr": {"labels": ["osteoblast"], "reviewers": ["octocat"]},
            "schedule": {"enabled": True, "cron": "17 5 * * 1-5"},
        }
    )


class ManifestModelTests(unittest.TestCase):
    def test_manifest_parses_required_sections(self) -> None:
        manifest = make_manifest()
        self.assertEqual(manifest.base_branch, "main")
        self.assertEqual(manifest.verify.commands, ("python -m unittest",))
        self.assertEqual(manifest.pr.labels, ("osteoblast",))
        self.assertTrue(manifest.schedule.enabled)

    def test_finding_classifies_sensitive_paths_as_serious(self) -> None:
        finding = Finding.from_dict(
            {
                "type": "osteoblast",
                "category": "hardening",
                "scope": "src/security",
                "proof": ["Security-sensitive file lacks validation."],
                "candidate_files": ["src/security/auth.py"],
                "why": "Authentication logic needs hardening.",
                "estimated_change_size": {"files": 1, "lines": 20},
                "confidence": 0.95,
                "commit_title": "harden auth validation",
                "verification_hint": "pytest tests/test_auth.py",
            }
        )
        classified = finding.classify(make_manifest())
        self.assertEqual(classified.severity, "serious")

    def test_finding_classifies_low_risk_change_as_routine(self) -> None:
        finding = Finding.from_dict(
            {
                "type": "osteoclast",
                "category": "docs",
                "scope": "docs",
                "proof": ["README references a command that no longer exists."],
                "candidate_files": ["docs/README.md"],
                "why": "The docs are stale and should match the code.",
                "estimated_change_size": {"files": 1, "lines": 8},
                "confidence": 0.91,
                "commit_title": "remove stale docs command",
                "verification_hint": "N/A",
            }
        )
        classified = finding.classify(make_manifest())
        self.assertEqual(classified.severity, "routine")

    def test_canonicalize_category_maps_dead_tissue_to_dead_code(self) -> None:
        self.assertEqual(canonicalize_category("dead tissue"), "dead-code")

    def test_finding_normalizes_category_aliases(self) -> None:
        finding = Finding.from_dict(
            {
                "type": "osteoclast",
                "category": "dead tissue",
                "scope": "src",
                "proof": ["Unused code path remains reachable only by tests."],
                "candidate_files": ["src/legacy.py"],
                "why": "The dead code adds maintenance cost.",
                "estimated_change_size": {"files": 1, "lines": 10},
                "confidence": 0.92,
                "commit_title": "remove unused legacy branch",
                "verification_hint": "pytest tests/test_legacy.py",
            }
        )
        self.assertEqual(finding.category, "dead-code")
