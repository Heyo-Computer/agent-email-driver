"""Drive the `printer` CLI to execute a spec inside the target repo.

`printer exec <spec> --cwd <repo>` runs the plan/implement/review loop. Exit 0
means success (all tasks done + review passed). Non-zero means blocked, stalled,
max-turns, or error — we classify by peeking at the `.printer/exec/<key>.json`
checkpoint and the tail of stderr. Resumable via `printer exec --continue`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from util import Result, log, run

# Scaffold lines like `--- stdout ---` that carry no diagnostic content.
_SECTION_HEADER = re.compile(r"^-{2,}[^-]*-{2,}$")


def _meaningful_tail(lines: list[str], limit: int = 400) -> str:
    """The most informative line of process output: the last explicit
    error-ish line if any, else the last line that isn't a section header."""
    info = [ln for ln in lines if ln and not _SECTION_HEADER.match(ln)]
    generic = None
    for ln in reversed(info):
        low = ln.lower()
        if "exited with status" in low:
            generic = generic or ln
        elif "error" in low or "panic" in low:
            return ln[:limit]
    if generic:
        # The wrapper alone says nothing; attach the child's last real line
        # (its final stderr output, which is where the actual reason lives).
        for ln in reversed(info):
            if ln is not generic and "exited with status" not in ln.lower():
                return f"{generic} — {ln}"[:limit]
        return generic[:limit]
    return info[-1][:limit] if info else ""


@dataclass
class ExecOutcome:
    success: bool
    reason: str          # human-readable summary (blocked reason / error tail)
    phase: str | None    # last checkpoint phase, if discoverable


class Printer:
    def __init__(self, cfg, repo: Path | None = None):
        # `repo` overrides the target working directory. It defaults to the
        # customer repo (`cfg.repo_path`); the self-improvement path points it
        # at factory's own source (`cfg.factory_self_path`).
        self.cfg = cfg
        self.repo = repo or cfg.repo_path

    def exec_spec(self, spec_path: Path, *, resume: bool = False) -> ExecOutcome:
        """Run `printer exec` for a spec. Long-running; no wall-clock timeout."""
        cfg = self.cfg
        rel = self._rel(spec_path)
        if cfg.dry_run:
            log.info("[dry-run] would run: printer exec %s --cwd %s", rel, self.repo)
            return ExecOutcome(True, "dry-run", "Done")

        args = [cfg.printer_bin, "exec"]
        if resume:
            args.append("--continue")
        args.append(str(spec_path))
        args += [
            "--cwd", str(self.repo),
            "--model", cfg.agent_model,
            "--max-turns", str(cfg.printer_max_turns),
            "--verbose",
            # One commit per completed spec task, so the PR shows how the
            # implementation was built up instead of a single opaque commit.
            "--commit-each-task",
        ]
        log.info("printer exec starting for %s", rel)
        res = run(args, cwd=self.repo)  # inherits no timeout (can run for a while)
        phase = self._phase(spec_path)
        if res.ok:
            log.info("printer exec succeeded for %s", rel)
            return ExecOutcome(True, "all tasks done; review passed", phase or "Done")
        reason = self._classify(res, phase)
        log.warning("printer exec failed for %s: %s", rel, reason)
        return ExecOutcome(False, reason, phase)

    # --- helpers ---------------------------------------------------------------

    def _rel(self, spec_path: Path) -> str:
        try:
            return str(spec_path.relative_to(self.repo))
        except ValueError:
            return str(spec_path)

    def _classify(self, res: Result, phase: str | None) -> str:
        combined = ((res.err or "") + "\n" + (res.out or "")).strip()
        lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
        # The reason string stays short; put the full tail in the factory log
        # so failures are diagnosable without rerunning.
        if lines:
            log.warning("printer exec output tail:\n%s", "\n".join(lines[-40:]))
        if "<<BLOCKED" in combined:
            # printer surfaces the blocked reason in its error text.
            for line in reversed(lines):
                if "BLOCK" in line.upper():
                    return f"blocked: {line}"
        if res.code == 124:
            return "timed out"
        last = _meaningful_tail(lines)
        if phase:
            return f"failed at phase {phase}: {last}" if last else f"failed at phase {phase}"
        return f"exit {res.code}: {last}" if last else f"exit {res.code}"

    def _phase(self, spec_path: Path) -> str | None:
        """Best-effort read of the current exec checkpoint phase."""
        exec_dir = self.repo / ".printer" / "exec"
        if not exec_dir.is_dir():
            return None
        best = None
        for jf in exec_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text())
            except Exception:  # noqa: BLE001
                continue
            spec = str(data.get("spec", ""))
            if spec and Path(spec).name == spec_path.name:
                return data.get("phase") or data.get("status")
            best = data.get("phase") or best
        return best
