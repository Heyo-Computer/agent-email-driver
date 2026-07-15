"""Drive the `printer` CLI to execute a spec inside the target repo.

`printer exec <spec> --cwd <repo>` runs the plan/implement/review loop. Exit 0
means success (all tasks done + review passed). Non-zero means blocked, stalled,
max-turns, or error — we classify by peeking at the `.printer/exec/<key>.json`
checkpoint and the tail of the exec log. Resumable via `printer exec --continue`.

The exec runs DETACHED (its own session, output to a log file, pid + exit code
recorded under `.printer/factory-exec/` in the target tree). Factory merely
waits on it, so a factory crash/restart — including the self-update restart,
whose supervisor group-kill would otherwise take the exec down with it — leaves
the agent running; on resume factory re-attaches to the live process (or picks
up the recorded exit code if it finished while factory was away).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from util import Result, log

# Returned by `_wait` when an exec stalled and auto-nudges couldn't clear it.
_STALLED = object()

# Printer's non-TTY heartbeat line. It's emitted every ~10s from printer's own
# process even while the child agent is wedged, so it is NOT evidence of
# progress — real activity is any other `[agent]`/`[printer]` line, or a task
# transition.
_HEARTBEAT = "still working"

# Scaffold lines like `--- stdout ---` that carry no diagnostic content.
_SECTION_HEADER = re.compile(r"^-{2,}[^-]*-{2,}$")

# Failure signatures that are TRANSIENT — the request is fine, the provider
# said "not right now" (credits exhausted, rate limited, overloaded). These
# runs must be paused and retried, never reported as terminal failures.
_TRANSIENT = re.compile(
    r"credit balance|out of credits|insufficient credits|usage limit|"
    r"rate.?limit|overloaded|quota|billing|too many requests|"
    r"\b(?:429|529)\b",
    re.IGNORECASE,
)


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
    # Factory was asked to shut down while the detached exec was still
    # running. The exec continues; the item must stay journaled for resume.
    interrupted: bool = False
    # Failure looks provider-transient (credits/rate limit/overload): the
    # item should be paused and retried, not reported as terminally failed.
    transient: bool = False
    # The exec wedged and automatic nudges could not clear it: pause and
    # retry fresh later, like a transient failure.
    stalled: bool = False


class Printer:
    def __init__(self, cfg, repo: Path | None = None):
        # `repo` overrides the target working directory. It defaults to the
        # customer repo (`cfg.repo_path`); the self-improvement path points it
        # at factory's own source (`cfg.factory_self_path`).
        self.cfg = cfg
        self.repo = repo or cfg.repo_path

    def for_repo(self, repo: Path) -> "Printer":
        """A Printer bound to another working tree (same cfg) — used to run
        the exec inside a per-item worktree."""
        return type(self)(self.cfg, repo=repo)

    # Poll cadence while waiting on the detached exec.
    POLL_SECS = 5

    def exec_spec(self, spec_path: Path, *, resume: bool = False,
                  on_progress=None, stop_check=None, on_nudge=None) -> ExecOutcome:
        """Run `printer exec` for a spec, detached. Blocks until it finishes
        (no wall-clock timeout), but the exec itself survives factory dying:
        on a later call for the same tree we re-attach to the live process or
        collect the exit code it left behind.

        `on_progress(titles, done, total)` is called (best-effort) whenever
        tasks newly transition to done while we wait, so the pipeline can
        surface progress to the requester. `stop_check()` returning True ends
        the wait promptly with an `interrupted` outcome — the detached exec
        keeps running and a later call re-attaches. `on_nudge(reason, manual)`
        fires when a wedged exec is nudged (killed + resumed from checkpoint),
        either automatically after a stall or on a manual request."""
        cfg = self.cfg
        rel = self._rel(spec_path)
        if cfg.dry_run:
            log.info("[dry-run] would run: printer exec %s --cwd %s", rel, self.repo)
            return ExecOutcome(True, "dry-run", "Done")

        # Baseline of already-done tasks — completions before this call
        # (earlier attempts, or announced pre-crash) aren't re-announced.
        reported, _total, _titles = self._task_progress()

        if self._exit_file().is_file():
            # Finished while factory was away; collect the recorded outcome.
            log.info("printer exec already finished for %s; collecting result", rel)
        elif (pid := self._live_pid()) is not None:
            log.info("re-attaching to running printer exec for %s (pid %d)",
                     rel, pid)
        else:
            self._spawn(spec_path, resume=resume)

        code = self._wait(spec_path, on_progress=on_progress, reported=reported,
                          stop_check=stop_check, on_nudge=on_nudge)
        phase = self._phase(spec_path)
        if code is None:
            log.info("shutdown requested; leaving detached exec running for %s",
                     rel)
            return ExecOutcome(False,
                               "factory shutdown requested; detached exec "
                               "continues and will be re-attached on restart",
                               phase, interrupted=True)
        if code is _STALLED:
            log.warning("printer exec stalled past nudge limit for %s", rel)
            return ExecOutcome(False,
                               "exec stalled (no progress); automatic nudges "
                               "did not recover it — will retry fresh later",
                               phase, stalled=True)
        self._cleanup_state()
        if code == 0:
            log.info("printer exec succeeded for %s", rel)
            return ExecOutcome(True, "all tasks done; review passed", phase or "Done")
        tail = self._log_tail()
        res = Result(code, "", tail)
        reason = self._classify(res, phase)
        transient = bool(_TRANSIENT.search(tail))
        log.warning("printer exec failed for %s%s: %s", rel,
                    " (transient)" if transient else "", reason)
        return ExecOutcome(False, reason, phase, transient=transient)

    # --- detached process management --------------------------------------------

    def _state_dir(self) -> Path:
        return Path(self.repo) / ".printer" / "factory-exec"

    def _pid_file(self) -> Path:
        return self._state_dir() / "exec.pid"

    def _exit_file(self) -> Path:
        return self._state_dir() / "exec.exit"

    def _log_file(self) -> Path:
        return self._state_dir() / "exec.log"

    def exec_active(self) -> bool:
        """True when a detached exec for this tree is still running, or has
        finished but its result hasn't been collected yet. While active, the
        working tree belongs to the agent — callers must not mutate it (no
        banking commits, base merges, or spec rewrites)."""
        return self._exit_file().is_file() or self._live_pid() is not None

    def request_nudge(self) -> bool:
        """Ask a running detached exec in this tree to nudge itself (kill +
        resume from checkpoint). Cross-thread safe: the flag is a file the
        wait loop polls. Returns False when no exec is active to nudge."""
        if self._live_pid() is None:
            return False
        try:
            self._state_dir().mkdir(parents=True, exist_ok=True)
            self._nudge_file().write_text("1")
            log.info("manual nudge requested for %s", self.repo)
            return True
        except OSError as e:  # noqa: BLE001
            log.error("could not write nudge request: %s", e)
            return False

    def _spawn(self, spec_path: Path, *, resume: bool) -> None:
        cfg = self.cfg
        args = [cfg.printer_bin, "exec"]
        if resume:
            args.append("--continue")
        args.append(str(spec_path))
        args += [
            "--cwd", str(self.repo),
            "--model", cfg.agent_model,
            "--max-turns", str(cfg.printer_max_turns),
            "--verbose",
            # One commit per completed spec task, pushed immediately, so the
            # PR shows how the implementation was built up and progress is
            # retained on GitHub even if the exec dies.
            "--commit-each-task",
            "--push-each-task",
        ]
        state = self._state_dir()
        state.mkdir(parents=True, exist_ok=True)
        self._exit_file().unlink(missing_ok=True)
        # Wrap in `sh` so the exit code is captured on disk even when factory
        # isn't around to observe it. The temp-then-mv makes its appearance
        # atomic (a reader never sees an empty exit file).
        cmd = " ".join(shlex.quote(a) for a in args)
        exit_tmp = self._exit_file().with_suffix(".tmp")
        wrapper = (
            f"{cmd} >> {shlex.quote(str(self._log_file()))} 2>&1; "
            f"echo $? > {shlex.quote(str(exit_tmp))} && "
            f"mv {shlex.quote(str(exit_tmp))} {shlex.quote(str(self._exit_file()))}"
        )
        proc = subprocess.Popen(  # noqa: S602 - argv is shlex-quoted above
            ["sh", "-c", wrapper],
            cwd=str(self.repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own session: survives factory's death
        )
        self._pid_file().write_text(str(proc.pid))
        log.info("printer exec started detached (pid %d), log %s",
                 proc.pid, self._log_file())

    def _live_pid(self) -> int | None:
        """Pid of a still-running detached exec for this tree, else None."""
        try:
            pid = int(self._pid_file().read_text().strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # exists but owned elsewhere — treat as alive
        return pid

    def _wait(self, spec_path: Path, on_progress=None,
              reported: set[str] | None = None, stop_check=None,
              on_nudge=None):
        """Block until the detached exec finishes. Returns its exit code, or
        None when `stop_check()` asks us to stop (exec keeps running), or the
        `_STALLED` sentinel when the exec wedged and auto-nudges couldn't clear
        it. Fires `on_progress` for done-transitions and `on_nudge` when a
        wedged exec is kicked."""
        reported = set(reported or ())
        stall_timeout = getattr(self.cfg, "stall_timeout", 0)
        max_nudges = getattr(self.cfg, "max_nudges", 0)
        auto_nudges = 0
        # Measure activity only from now on: a re-attached exec's log history
        # is not evidence of current liveness.
        self._log_offset = self._log_size()
        last_activity = time.monotonic()
        while True:
            if self._exit_file().is_file():
                try:
                    return int(self._exit_file().read_text().strip())
                except ValueError:
                    return 1
            if stop_check and stop_check():
                return None
            if self._live_pid() is None:
                if self._exit_file().is_file():
                    continue
                log.warning("detached printer exec disappeared without an "
                            "exit code")
                return 1

            active = False
            done_ids, total, titles = self._task_progress()
            new = done_ids - reported
            if new:
                active = True
                reported |= new
                if on_progress:
                    try:
                        on_progress(sorted(titles.get(i, i) for i in new),
                                    len(done_ids), total)
                    except Exception as e:  # noqa: BLE001 - best-effort
                        log.error("progress callback failed: %s", e)
            if self._log_advanced():
                active = True
            if active:
                last_activity = time.monotonic()

            # Manual nudge takes priority and doesn't consume the auto budget.
            manual = self._nudge_file().is_file()
            if manual:
                self._nudge_file().unlink(missing_ok=True)
            stalled = (stall_timeout and not manual
                       and time.monotonic() - last_activity > stall_timeout)

            if manual or stalled:
                if stalled and auto_nudges >= max_nudges:
                    # Give up: kill the wedged exec and clear its state so the
                    # later retry spawns fresh (--continue from checkpoint)
                    # instead of re-attaching to the same dead process.
                    pid = self._live_pid()
                    if pid is not None:
                        self._kill_group(pid)
                    self._cleanup_state()
                    return _STALLED
                secs = int(time.monotonic() - last_activity)
                reason = ("manual request" if manual
                          else f"no progress for {secs}s")
                log.warning("nudging wedged exec (%s): killing and resuming "
                            "from checkpoint", reason)
                self._nudge(spec_path)
                if stalled:
                    auto_nudges += 1
                last_activity = time.monotonic()
                self._log_offset = self._log_size()
                if on_nudge:
                    try:
                        on_nudge(reason, manual)
                    except Exception as e:  # noqa: BLE001 - best-effort
                        log.error("nudge callback failed: %s", e)
            time.sleep(self.POLL_SECS)

    # --- stall detection + nudge ------------------------------------------------

    def _nudge_file(self) -> Path:
        """Presence requests a manual nudge; the wait loop consumes it."""
        return self._state_dir() / "nudge.request"

    def _log_size(self) -> int:
        try:
            return self._log_file().stat().st_size
        except OSError:
            return 0

    def _log_advanced(self) -> bool:
        """True if the log gained any NON-heartbeat line since the last check
        (advancing the read offset). Heartbeat lines are excluded — printer
        emits them even while the child agent is wedged, so they are not
        progress. Only new bytes are read, so this stays cheap on long runs."""
        path = self._log_file()
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size < self._log_offset:   # truncated/rotated
            self._log_offset = 0
        if size <= self._log_offset:
            return False
        try:
            with path.open("r", errors="replace") as fh:
                fh.seek(self._log_offset)
                chunk = fh.read()
                self._log_offset = fh.tell()
        except OSError:
            return False
        return any(line.strip() and _HEARTBEAT not in line
                   for line in chunk.splitlines())

    def _nudge(self, spec_path: Path) -> None:
        """Kill the wedged detached exec (whole process group) and re-spawn it
        with --continue. Printer resumes from its checkpoint; per-task commits
        already pushed are preserved."""
        pid = self._live_pid()
        if pid is not None:
            self._kill_group(pid)
        self._cleanup_state()
        self._spawn(spec_path, resume=True)

    def _kill_group(self, pid: int) -> None:
        """SIGTERM then SIGKILL the process group led by the detached wrapper
        (spawned with start_new_session, so pid is the group leader — this
        reaps the sh wrapper, printer, and the agent subtree)."""
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(30):  # up to ~3s for graceful exit
            time.sleep(0.1)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _task_progress(self) -> tuple[set[str], int, dict[str, str]]:
        """Read printer's task store: (done ids, total tasks, id -> title).

        Task files are TOML frontmatter between `+++` fences; we only need
        id/title/status, parsed line-wise to avoid a TOML dependency."""
        tasks_dir = Path(self.repo) / ".printer" / "tasks"
        done: set[str] = set()
        titles: dict[str, str] = {}
        total = 0
        if not tasks_dir.is_dir():
            return done, total, titles
        for f in sorted(tasks_dir.glob("*.md")):
            tid = title = status = None
            fences = 0
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line.strip() == "+++":
                        fences += 1
                        if fences == 2:
                            break  # end of frontmatter; ignore the body
                        continue
                    m = re.match(r'^(id|title|status)\s*=\s*"(.*)"\s*$', line)
                    if m:
                        if m.group(1) == "id":
                            tid = m.group(2)
                        elif m.group(1) == "title":
                            title = m.group(2)
                        else:
                            status = m.group(2)
            except OSError:
                continue
            if tid is None:
                continue
            total += 1
            titles[tid] = title or tid
            if status == "done":
                done.add(tid)
        return done, total, titles

    def _cleanup_state(self) -> None:
        """Drop pid/exit markers so a later fresh exec in this tree doesn't
        read stale state. The log is kept for debugging (it lives under
        `.printer/`, which is excluded from commits and removed with the
        worktree)."""
        self._pid_file().unlink(missing_ok=True)
        self._exit_file().unlink(missing_ok=True)

    def _log_tail(self, lines: int = 60) -> str:
        try:
            content = self._log_file().read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

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
