"""Configuration for the factory daemon.

Loads `FACTORY_*` settings from the process environment, optionally seeded from a
`.env` file sitting next to this module. Stdlib only — no python-dotenv.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file.

    Existing environment variables always win, so an exported value overrides
    the file. Lines that are blank or start with `#` are ignored. Surrounding
    single/double quotes on the value are stripped. A leading `export ` is
    tolerated.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _require(name: str) -> str:
    val = _env(name)
    if not val:
        raise SystemExit(
            f"factory: required environment variable {name} is not set "
            f"(see .env.example)"
        )
    return val


def _csv(name: str) -> list[str]:
    val = _env(name)
    if not val:
        return []
    return [p.strip() for p in val.split(",") if p.strip()]


@dataclass
class Config:
    # Target repo + git
    repo_path: Path
    base_branch: str
    branch_prefix: str
    specs_dir: str

    # Loop
    poll_interval: int
    dry_run: bool

    # Crash-resume journal (in-flight work items, re-queued on startup)
    state_dir: Path
    resume_max_attempts: int

    # Max work items processed concurrently (each in its own worktree).
    max_concurrent: int

    # Linear (via the Linear MCP server, reached through `claude -p`)
    linear_team: str
    linear_trigger_state: str
    linear_inprogress_state: str
    linear_review_state: str
    linear_blocked_state: str

    # IMAP (inbox monitor)
    imap_host: str | None
    imap_port: int
    imap_user: str | None
    imap_pass: str | None
    imap_folder: str
    imap_allowed_senders: list[str]
    imap_require_directive: bool
    imap_directive_markers: list[str]

    # SMTP (notifications)
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_pass: str | None
    smtp_from: str | None
    smtp_starttls: bool
    notify_to: str

    # Binaries / agent
    claude_bin: str
    printer_bin: str
    agent_model: str
    printer_max_turns: int

    # Self-improvement (factory editing its own source via printer)
    factory_self_path: Path        # factory's own checkout (defaults to this dir)
    self_marker: str               # request-title prefix that routes to self-update
    self_specs_dir: str            # spec dir under factory_self_path
    self_push: bool                # also push self changes to origin after build
    self_restart: bool             # restart via supervisor after a good self-update
    supervisorctl_bin: str         # supervisorctl binary
    supervisor_conf: str | None    # -c <conf> for supervisorctl (defaults to ./supervisord.conf)
    supervisor_program: str        # program name supervisord runs factory under

    @property
    def worktrees_dir(self) -> Path:
        """Per-item git worktrees (isolated checkouts of the target repo)."""
        return self.state_dir / "worktrees"

    @property
    def linear_enabled(self) -> bool:
        return bool(self.linear_team)

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_pass)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @classmethod
    def load(cls, dotenv: Path | None = None) -> "Config":
        _load_dotenv(dotenv or (HERE / ".env"))
        return cls(
            repo_path=Path(_require("FACTORY_REPO_PATH")).expanduser().resolve(),
            base_branch=_env("FACTORY_BASE_BRANCH", "main"),
            branch_prefix=_env("FACTORY_BRANCH_PREFIX", "factory"),
            specs_dir=_env("FACTORY_SPECS_DIR", "specs"),
            poll_interval=int(_env("FACTORY_POLL_INTERVAL", "180")),
            dry_run=_env("FACTORY_DRY_RUN", "") not in ("", "0", "false", "no"),
            state_dir=Path(
                _env("FACTORY_STATE_DIR", "~/.factory/state")
            ).expanduser().resolve(),
            resume_max_attempts=int(_env("FACTORY_RESUME_MAX_ATTEMPTS", "3")),
            max_concurrent=int(_env("FACTORY_MAX_CONCURRENT", "3")),
            linear_team=_env("FACTORY_LINEAR_TEAM", ""),
            linear_trigger_state=_env("FACTORY_LINEAR_TRIGGER_STATE", "Todo"),
            linear_inprogress_state=_env(
                "FACTORY_LINEAR_INPROGRESS_STATE", "In Progress"
            ),
            linear_review_state=_env("FACTORY_LINEAR_REVIEW_STATE", "In Review"),
            linear_blocked_state=_env("FACTORY_LINEAR_BLOCKED_STATE", ""),
            imap_host=_env("FACTORY_IMAP_HOST"),
            imap_port=int(_env("FACTORY_IMAP_PORT", "993")),
            imap_user=_env("FACTORY_IMAP_USER"),
            imap_pass=_env("FACTORY_IMAP_PASS"),
            imap_folder=_env("FACTORY_IMAP_FOLDER", "INBOX"),
            imap_allowed_senders=_csv("FACTORY_IMAP_ALLOWED_SENDERS"),
            imap_require_directive=_env("FACTORY_IMAP_REQUIRE_DIRECTIVE", "1")
            not in ("0", "false", "no"),
            imap_directive_markers=_csv("FACTORY_IMAP_DIRECTIVE_MARKERS")
            or ["factory:", "@factory", "hey factory"],
            smtp_host=_env("FACTORY_SMTP_HOST"),
            smtp_port=int(_env("FACTORY_SMTP_PORT", "587")),
            smtp_user=_env("FACTORY_SMTP_USER"),
            smtp_pass=_env("FACTORY_SMTP_PASS"),
            smtp_from=_env("FACTORY_SMTP_FROM"),
            smtp_starttls=_env("FACTORY_SMTP_STARTTLS", "1")
            not in ("0", "false", "no"),
            notify_to=_env("FACTORY_NOTIFY_TO", "sam@sarocu.com"),
            claude_bin=_env("FACTORY_CLAUDE_BIN", "claude"),
            printer_bin=_env("FACTORY_PRINTER_BIN", "printer"),
            agent_model=_env("FACTORY_AGENT_MODEL", "opus"),
            printer_max_turns=int(_env("FACTORY_PRINTER_MAX_TURNS", "40")),
            factory_self_path=Path(
                _env("FACTORY_SELF_PATH", str(HERE))
            ).expanduser().resolve(),
            self_marker=_env("FACTORY_SELF_MARKER", "[self]"),
            self_specs_dir=_env("FACTORY_SELF_SPECS_DIR", "specs"),
            self_push=_env("FACTORY_SELF_PUSH", "") not in ("", "0", "false", "no"),
            self_restart=_env("FACTORY_SELF_RESTART", "1")
            not in ("0", "false", "no"),
            supervisorctl_bin=_env("FACTORY_SUPERVISORCTL_BIN", "supervisorctl"),
            supervisor_conf=_env(
                "FACTORY_SUPERVISOR_CONF", str(HERE / "supervisord.conf")
            ),
            supervisor_program=_env("FACTORY_SUPERVISOR_PROGRAM", "factory"),
        )
