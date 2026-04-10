from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable, Mapping

from .models import OsteoblastError


class CommandError(OsteoblastError):
    """Raised when an external command fails."""

    def __init__(self, command: Iterable[str], returncode: int, stdout: str, stderr: str) -> None:
        rendered = " ".join(command)
        super().__init__(f"Command failed with exit code {returncode}: {rendered}")
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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
