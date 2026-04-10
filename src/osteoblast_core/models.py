from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable
import re
import tomllib


class OsteoblastError(RuntimeError):
    """Base error for osteoblast orchestration."""


class ManifestError(OsteoblastError):
    """Raised when a manifest is missing required configuration."""


class FindingError(OsteoblastError):
    """Raised when a finding payload is invalid."""


DEFAULT_SERIOUS_PATH_KEYWORDS = (
    "auth",
    "security",
    "secret",
    "permission",
    ".github/workflows",
    "migration",
    "migrations",
    "schema",
    "infra",
    "terraform",
)

DEFAULT_PUBLIC_API_GLOBS = (
    "public/**",
    "**/public/**",
    "api/**",
    "**/api/**",
    "**/*.proto",
    "**/openapi*.yml",
    "**/openapi*.yaml",
    "**/openapi*.json",
    "**/swagger*.json",
)

DEFAULT_FORBIDDEN_LOCAL_GLOBS = (
    ".github/workflows/**",
    "**/migrations/**",
    "**/migration/**",
    "**/schema/**",
    "**/*.sql",
)

DEFAULT_CATEGORY_ALIASES = {
    "bug": "bugs",
    "bugs": "bugs",
    "correctness": "bugs",
    "dead-code": "dead-code",
    "dead-code-removal": "dead-code",
    "deadcode": "dead-code",
    "dead-tissue": "dead-code",
    "doc": "docs",
    "docs": "docs",
    "documentation": "docs",
    "stale-docs": "docs",
    "hardening": "hardening",
    "consistency": "consistency",
    "readability": "readability",
    "performance": "performance",
}


def canonicalize_category(value: str) -> str:
    normalized = re.sub(r"[\s_]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return DEFAULT_CATEGORY_ALIASES.get(normalized, normalized)


def _as_list_of_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ManifestError(f"`{field_name}` must be an array of strings.")
    return tuple(value)


def _require_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"`{key}` must be a non-empty string.")
    return value


def _require_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or value < 0:
        raise ManifestError(f"`{key}` must be a non-negative integer.")
    return value


@dataclass(frozen=True)
class ChangeBudget:
    files: int
    lines: int


@dataclass(frozen=True)
class SeverityRules:
    confidence_threshold: float = 0.75
    serious_path_keywords: tuple[str, ...] = DEFAULT_SERIOUS_PATH_KEYWORDS
    public_api_globs: tuple[str, ...] = DEFAULT_PUBLIC_API_GLOBS
    forbidden_local_globs: tuple[str, ...] = DEFAULT_FORBIDDEN_LOCAL_GLOBS

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "SeverityRules":
        if mapping is None:
            return cls()
        if not isinstance(mapping, dict):
            raise ManifestError("`severity_rules` must be a table.")

        confidence = mapping.get("confidence_threshold", 0.75)
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ManifestError("`severity_rules.confidence_threshold` must be between 0 and 1.")

        serious_keywords = mapping.get("serious_path_keywords", list(DEFAULT_SERIOUS_PATH_KEYWORDS))
        public_api_globs = mapping.get("public_api_globs", list(DEFAULT_PUBLIC_API_GLOBS))
        forbidden_local_globs = mapping.get(
            "forbidden_local_globs",
            list(DEFAULT_FORBIDDEN_LOCAL_GLOBS),
        )

        return cls(
            confidence_threshold=float(confidence),
            serious_path_keywords=_as_list_of_strings(
                serious_keywords, "severity_rules.serious_path_keywords"
            ),
            public_api_globs=_as_list_of_strings(
                public_api_globs, "severity_rules.public_api_globs"
            ),
            forbidden_local_globs=_as_list_of_strings(
                forbidden_local_globs, "severity_rules.forbidden_local_globs"
            ),
        )

    def path_requires_serious_routing(self, path: str) -> bool:
        lowered = path.lower()
        if any(keyword in lowered for keyword in self.serious_path_keywords):
            return True
        return any(fnmatch(path, pattern) for pattern in self.public_api_globs)

    def path_forbidden_for_local_routine(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.forbidden_local_globs)


@dataclass(frozen=True)
class VerifyConfig:
    commands: tuple[str, ...]

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "VerifyConfig":
        if not isinstance(mapping, dict):
            raise ManifestError("`verify` must be a table containing `commands`.")
        commands = _as_list_of_strings(mapping.get("commands"), "verify.commands")
        return cls(commands=commands)


@dataclass(frozen=True)
class PullRequestConfig:
    labels: tuple[str, ...]
    reviewers: tuple[str, ...]

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "PullRequestConfig":
        if not isinstance(mapping, dict):
            raise ManifestError("`pr` must be a table.")
        labels = _as_list_of_strings(mapping.get("labels"), "pr.labels")
        reviewers = mapping.get("reviewers", [])
        reviewers_list = _as_list_of_strings(reviewers, "pr.reviewers")
        return cls(labels=labels, reviewers=reviewers_list)


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    cron: str | None = None

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "ScheduleConfig":
        if not isinstance(mapping, dict):
            raise ManifestError("`schedule` must be a table.")
        enabled = mapping.get("enabled")
        if not isinstance(enabled, bool):
            raise ManifestError("`schedule.enabled` must be a boolean.")
        cron = mapping.get("cron")
        if cron is not None and not isinstance(cron, str):
            raise ManifestError("`schedule.cron` must be a string when present.")
        return cls(enabled=enabled, cron=cron)


@dataclass(frozen=True)
class Manifest:
    version: str
    base_branch: str
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    severity_rules: SeverityRules
    max_files_changed: int
    max_changed_lines: int
    verify: VerifyConfig
    pr: PullRequestConfig
    schedule: ScheduleConfig

    @classmethod
    def from_path(cls, path: Path) -> "Manifest":
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "Manifest":
        if not isinstance(mapping, dict):
            raise ManifestError("Manifest contents must be a table.")

        include_paths = _as_list_of_strings(mapping.get("include_paths"), "include_paths")
        exclude_paths = mapping.get("exclude_paths", [])
        exclude_paths_list = _as_list_of_strings(exclude_paths, "exclude_paths")
        allowed_categories = _as_list_of_strings(
            mapping.get("allowed_categories"), "allowed_categories"
        )

        return cls(
            version=_require_string(mapping, "version"),
            base_branch=_require_string(mapping, "base_branch"),
            include_paths=include_paths,
            exclude_paths=exclude_paths_list,
            allowed_categories=tuple(canonicalize_category(category) for category in allowed_categories),
            severity_rules=SeverityRules.from_mapping(mapping.get("severity_rules")),
            max_files_changed=_require_int(mapping, "max_files_changed"),
            max_changed_lines=_require_int(mapping, "max_changed_lines"),
            verify=VerifyConfig.from_mapping(mapping.get("verify")),
            pr=PullRequestConfig.from_mapping(mapping.get("pr")),
            schedule=ScheduleConfig.from_mapping(mapping.get("schedule")),
        )

    def allowed_scope_paths(self, repo_root: Path) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for relative in self.include_paths:
            candidate = (repo_root / relative).resolve()
            if candidate.exists():
                resolved.append(candidate)
        return tuple(resolved)

    def scope_is_excluded(self, repo_root: Path, candidate: Path) -> bool:
        relative = candidate.resolve().relative_to(repo_root.resolve()).as_posix()
        return any(fnmatch(relative, pattern) for pattern in self.exclude_paths)


@dataclass(frozen=True)
class Finding:
    type: str
    category: str
    scope: str
    proof: tuple[str, ...]
    candidate_files: tuple[str, ...]
    why: str
    estimated_change_size: ChangeBudget
    confidence: float
    commit_title: str
    verification_hint: str
    severity: str | None = None

    @classmethod
    def from_dict(cls, mapping: dict[str, Any]) -> "Finding":
        if not isinstance(mapping, dict):
            raise FindingError("Finding payload must be a JSON object.")

        proof = mapping.get("proof")
        if isinstance(proof, str):
            proof_items = (proof,)
        elif isinstance(proof, list) and all(isinstance(item, str) for item in proof):
            proof_items = tuple(proof)
        else:
            raise FindingError("`proof` must be a string or array of strings.")

        candidate_files = _as_list_of_strings(mapping.get("candidate_files"), "candidate_files")

        estimated = mapping.get("estimated_change_size")
        if not isinstance(estimated, dict):
            raise FindingError("`estimated_change_size` must be an object.")
        files = estimated.get("files")
        lines = estimated.get("lines")
        if not isinstance(files, int) or files < 0 or not isinstance(lines, int) or lines < 0:
            raise FindingError("`estimated_change_size.files` and `.lines` must be non-negative integers.")

        confidence = mapping.get("confidence", 1.0)
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise FindingError("`confidence` must be between 0 and 1.")

        severity = mapping.get("severity")
        if severity is not None and severity not in {"routine", "serious"}:
            raise FindingError("`severity` must be `routine` or `serious` when present.")

        return cls(
            type=_require_string(mapping, "type"),
            category=canonicalize_category(_require_string(mapping, "category")),
            scope=_require_string(mapping, "scope"),
            proof=proof_items,
            candidate_files=candidate_files,
            why=_require_string(mapping, "why"),
            estimated_change_size=ChangeBudget(files=files, lines=lines),
            confidence=float(confidence),
            commit_title=_require_string(mapping, "commit_title"),
            verification_hint=_require_string(mapping, "verification_hint"),
            severity=severity,
        )

    def classify(self, manifest: Manifest) -> "Finding":
        severity = self.severity or "routine"
        if self.estimated_change_size.files > manifest.max_files_changed:
            severity = "serious"
        if self.estimated_change_size.lines > manifest.max_changed_lines:
            severity = "serious"
        if self.confidence < manifest.severity_rules.confidence_threshold:
            severity = "serious"
        if any(
            manifest.severity_rules.path_requires_serious_routing(path)
            for path in self.candidate_files
        ):
            severity = "serious"
        return replace(self, severity=severity)

    def violates_local_routine_policy(self, manifest: Manifest, changed_files: Iterable[str]) -> bool:
        if self.severity == "serious":
            return True
        return any(
            manifest.severity_rules.path_forbidden_for_local_routine(path)
            for path in changed_files
        )
