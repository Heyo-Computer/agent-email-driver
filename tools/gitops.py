"""Git plumbing for the target repo: branch off base, commit the spec, push.

All operations use an explicit `git` argument list against `cfg.repo_path`.
Branch names are deterministic so a crash/restart re-derives the same branch
(supporting idempotent resume).
"""

from __future__ import annotations

from pathlib import Path

from util import Result, log, run


class Git:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg.repo_path

    def _git(self, args: list[str], timeout: int = 120) -> Result:
        return run(["git", *args], cwd=self.repo, timeout=timeout)

    def remote_branch_exists(self, branch: str) -> bool:
        res = self._git(["ls-remote", "--heads", "origin", branch])
        return res.ok and bool(res.out.strip())

    def local_branch_exists(self, branch: str) -> bool:
        return self._git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
        ).ok

    def prepare_branch(self, branch: str) -> bool:
        """Fetch, then check out `branch` (existing or new off origin/base).

        Idempotent: if the branch already exists locally or remotely we just
        switch to it rather than recreating it.
        """
        if self.cfg.dry_run:
            log.info("[dry-run] would prepare branch %s off %s", branch,
                     self.cfg.base_branch)
            return True
        if not self._git(["fetch", "origin"]).ok:
            log.error("git fetch failed")
            return False
        if self.local_branch_exists(branch):
            return self._git(["switch", branch]).ok
        if self.remote_branch_exists(branch):
            return self._git(["switch", "--track", f"origin/{branch}"]).ok
        base = self.cfg.base_branch
        start = f"origin/{base}"
        if not self._git(["rev-parse", "--verify", "--quiet", start]).ok:
            start = base  # fall back to local base ref
        res = self._git(["switch", "-c", branch, start])
        if not res.ok:
            log.error("git switch -c %s failed: %s", branch, res.err.strip())
            return False
        return True

    def commit_paths(self, paths: list[str], message: str) -> bool:
        """Stage given paths and commit. No-op (returns True) if nothing staged."""
        if self.cfg.dry_run:
            log.info("[dry-run] would commit %s: %s", paths, message)
            return True
        if not self._git(["add", "--", *paths]).ok:
            return False
        # Anything to commit?
        if self._git(["diff", "--cached", "--quiet"]).ok:
            log.info("nothing to commit for %s", message)
            return True
        res = self._git(["commit", "-m", message])
        if not res.ok:
            log.error("git commit failed: %s", res.err.strip())
            return False
        return True

    def commit_all(self, message: str) -> bool:
        """Commit the agent's work, excluding printer's own `.printer/` bookkeeping
        so it never leaks into the PR."""
        if self.cfg.dry_run:
            log.info("[dry-run] would commit all: %s", message)
            return True
        self._git(["add", "-A", "--", ".", ":(exclude).printer", ":(exclude).printer/**"])
        if self._git(["diff", "--cached", "--quiet"]).ok:
            return True
        return self._git(["commit", "-m", message]).ok

    def push(self, branch: str) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] would push %s", branch)
            return True
        res = self._git(["push", "-u", "origin", branch])
        if not res.ok:
            log.error("git push failed: %s", res.err.strip())
            return False
        return True

    def has_uncommitted(self) -> bool:
        return bool(self._git(["status", "--porcelain"]).out.strip())
