"""Shared helpers: logging and safe subprocess execution (no shell)."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("factory")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int | None = None,
    input_text: str | None = None,
    env: dict | None = None,
) -> Result:
    """Run a command with an argument list (never a shell string).

    Returns a Result; does not raise on non-zero exit. Captures stdout/stderr
    as text. Logs the command at debug level.
    """
    log.debug("exec: %s (cwd=%s)", " ".join(args), cwd)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as e:
        return Result(127, "", f"binary not found: {e}")
    except subprocess.TimeoutExpired as e:
        return Result(124, e.stdout or "", f"timeout after {timeout}s")
    return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


def slugify(text: str, max_len: int = 48) -> str:
    """Lowercase, hyphenated, filesystem/branch-safe slug."""
    out = []
    prev_dash = False
    for ch in text.lower().strip():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return slug[:max_len].strip("-") or "untitled"
