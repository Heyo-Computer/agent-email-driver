"""Build and restart tooling for factory's own process.

These are the two safety-critical primitives behind self-improvement:

* `build()` validates factory's own source after `printer` has edited it —
  byte-compiling every module and import-smoke-testing the entry points. It is
  the gate that must pass before we commit and before we restart onto new code.
* `restart()` asks the supervisor that runs factory to restart it, so freshly
  written source actually takes effect. The supervisorctl call is launched in a
  detached session so it survives the SIGTERM that the restart sends to us.

Both are intentionally dependency-free (stdlib + the shared `run` helper).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from util import Result, log, run

# Entry points that must import cleanly for factory to even start. If `printer`
# breaks one of these, the build gate fails and we roll back instead of
# restarting into a crash loop.
_SMOKE_IMPORTS = (
    "import config, util, pipeline, factoryd, selfimprove; "
    "import tools.gh, tools.gitops, tools.inbox, tools.linear_mcp, "
    "tools.notify, tools.printer, tools.specgen, tools.selfupdate"
)


class SelfUpdater:
    def __init__(self, cfg):
        self.cfg = cfg
        self.src = cfg.factory_self_path

    # --- build -----------------------------------------------------------------

    def build(self) -> Result:
        """Validate the on-disk source. Returns an ok Result only if factory can
        plausibly start: every module byte-compiles and the entry points import.

        Run this AFTER `printer` edits and BEFORE committing/restarting.
        """
        if self.cfg.dry_run:
            log.info("[dry-run] would build (compile + import smoke) %s", self.src)
            return Result(0, "dry-run build", "")

        log.info("self-build: byte-compiling %s", self.src)
        compile_res = run(
            [sys.executable, "-m", "compileall", "-q", str(self.src)],
            cwd=self.src,
            timeout=120,
        )
        if not compile_res.ok:
            return Result(
                compile_res.code,
                compile_res.out,
                "compile failed: "
                + (compile_res.err or compile_res.out).strip()[-500:],
            )

        log.info("self-build: import smoke test")
        # Fresh interpreter rooted at the source dir so the just-written files
        # (not stale .pyc / this live process) are what gets imported.
        smoke = run(
            [sys.executable, "-c", _SMOKE_IMPORTS],
            cwd=self.src,
            timeout=120,
        )
        if not smoke.ok:
            return Result(
                smoke.code,
                smoke.out,
                "import smoke test failed: "
                + (smoke.err or smoke.out).strip()[-500:],
            )
        log.info("self-build: OK")
        return Result(0, "build ok", "")

    # --- restart ---------------------------------------------------------------

    def _ctl(self, *args: str) -> list[str]:
        cmd = [self.cfg.supervisorctl_bin]
        if self.cfg.supervisor_conf:
            cmd += ["-c", self.cfg.supervisor_conf]
        cmd += list(args)
        return cmd

    def status(self) -> str:
        """Best-effort `supervisorctl status` for the factory program."""
        res = run(self._ctl("status", self.cfg.supervisor_program), timeout=30)
        return (res.out or res.err).strip()

    def restart(self) -> bool:
        """Tell supervisor to restart factory.

        Launched detached (`start_new_session=True`) and not awaited: the restart
        sends SIGTERM to *this* process, so we must not be supervisorctl's parent
        in the same session, or the stop→start sequence could die with us. Factory
        handles SIGTERM gracefully (finishes the current item, then exits), and
        supervisor brings a fresh process up on the new code.
        """
        prog = self.cfg.supervisor_program
        if self.cfg.dry_run:
            log.info("[dry-run] would restart supervisor program '%s'", prog)
            return True
        cmd = self._ctl("restart", prog)
        log.info("self-restart: %s (detached)", " ".join(cmd))
        try:
            subprocess.Popen(
                cmd,
                cwd=str(self.src),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            log.error("self-restart failed (supervisorctl not found): %s", e)
            return False
        except Exception as e:  # noqa: BLE001
            log.error("self-restart failed: %s", e)
            return False
        return True
