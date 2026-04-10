from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
from typing import Iterable, Mapping

from .models import OsteoblastError


def _preview_text(value: str, *, limit: int = 2000) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "\n...[truncated]"


def _preview_arg(value: str, *, limit: int = 120) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[:limit] + "..."
    return shlex.quote(collapsed or "<empty>")


def _format_command(command: Iterable[str]) -> str:
    return " ".join(_preview_arg(part) for part in command)


class CommandError(OsteoblastError):
    """Raised when an external command fails."""

    def __init__(self, command: Iterable[str], returncode: int, stdout: str, stderr: str) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        details = [f"Command failed with exit code {returncode}: {_format_command(self.command)}"]
        if stderr.strip():
            details.append("stderr:\n" + _preview_text(stderr))
        elif stdout.strip():
            details.append("stdout:\n" + _preview_text(stdout))
        super().__init__("\n\n".join(details))


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class CommandRunner:
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else None,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        result = CommandResult(
            args=tuple(args),
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if check and completed.returncode != 0:
            raise CommandError(args, completed.returncode, completed.stdout, completed.stderr)
        return result
