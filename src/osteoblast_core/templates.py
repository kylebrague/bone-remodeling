from __future__ import annotations

from pathlib import Path
from typing import Iterable
import re
import shutil


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-z_]+)\s*}}")


def render_text(text: str, context: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, match.group(0))

    return PLACEHOLDER_PATTERN.sub(replace, text)


def render_tree(
    template_root: Path,
    target_root: Path,
    *,
    context: dict[str, str],
    force: bool,
) -> list[Path]:
    created: list[Path] = []
    for source in sorted(path for path in template_root.rglob("*") if path.is_file()):
        relative = source.relative_to(template_root)
        destination = target_root / relative
        if destination.exists() and not force:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        rendered = render_text(source.read_text(encoding="utf-8"), context)
        destination.write_text(rendered, encoding="utf-8")
        created.append(destination)
    return created


def copy_paths(
    paths: Iterable[tuple[Path, Path]],
    *,
    force: bool,
) -> list[Path]:
    copied: list[Path] = []
    for source, destination in paths:
        if destination.exists() and not force:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied
