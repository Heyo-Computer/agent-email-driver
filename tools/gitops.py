"""Git plumbing for the target repo: branch off base, commit the spec, push.

All operations use an explicit `git` argument list against `cfg.repo_path`.
Branch names are deterministic so a crash/restart re-derives the same branch
(supporting idempotent resume).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from util import Result, log, run


class Git:
    def __init__(self, cfg, repo: Path | None = None):
        # `repo` overrides the working tree git acts on. Defaults to the customer
        # repo; the self-improvement path passes `cfg.factory_self_path` so the
        # same helpers can operate on factory's own checkout.
        self.cfg = cfg
        self.repo = repo or cfg.repo_path

    def _git(self, args: list[str], timeout: int = 120) -> Result:
        return run(["git", *args], cwd=self.repo, timeout=timeout)

    def for_repo(self, repo: Path) -> "Git":
        """A Git bound to another working tree (same cfg) — used to operate
        inside a per-item worktree."""
        return type(self)(self.cfg, repo=repo)

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
            if not self._git(["switch", branch]).ok:
                return False
            return self._legacy_merge(branch)
        if self.remote_branch_exists(branch):
            if not self._git(["switch", "--track", f"origin/{branch}"]).ok:
                return False
            return self._legacy_merge(branch)
        base = self.cfg.base_branch
        start = f"origin/{base}"
        if not self._git(["rev-parse", "--verify", "--quiet", start]).ok:
            start = base  # fall back to local base ref
        res = self._git(["switch", "-c", branch, start])
        if not res.ok:
            log.error("git switch -c %s failed: %s", branch, res.err.strip())
            return False
        return True

    # --- worktree isolation: one checkout per work item -------------------------

    def worktree_add(self, path: Path, branch: str) -> bool:
        """Check out `branch` in a dedicated worktree at `path`, creating the
        branch off origin/<base> when it doesn't exist yet. Fetches first so
        new branches start from the current base. Reuses a leftover worktree
        already on `branch` (crash-resume keeps its in-flight work); a
        worktree in any other state is recreated."""
        if self.cfg.dry_run:
            log.info("[dry-run] would add worktree %s for %s", path, branch)
            return True
        if not self._git(["fetch", "origin"]).ok:
            log.error("git fetch failed")
            return False
        path = Path(path)
        if path.exists():
            # A valid, registered worktree already on `branch` is reused
            # (crash-resume keeps its in-flight work). Anything else — wrong
            # branch, or a stray directory git doesn't recognize as a worktree
            # (the "already exists / not a working tree" case) — is torn down
            # so the `worktree add` below can't collide with it.
            res = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
            if res.ok and res.out.strip() == branch and (path / ".git").exists():
                return True
            log.warning("worktree %s not a clean checkout of %r (%s); recreating",
                        path, branch, res.out.strip() or "unrecognized")
            if not self.worktree_remove(path):
                return False
        # Drop stale registrations (a deleted worktree dir still pins its
        # branch until pruned), and free the branch if the main checkout is
        # sitting on it from the pre-worktree flow.
        self._git(["worktree", "prune"])
        if self.current_branch() == branch:
            self._git(["switch", self.cfg.base_branch])
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.local_branch_exists(branch):
            res = self._git(["worktree", "add", str(path), branch])
        elif self.remote_branch_exists(branch):
            res = self._git(["worktree", "add", "--track", "-b", branch,
                             str(path), f"origin/{branch}"])
        else:
            start = f"origin/{self.cfg.base_branch}"
            if not self._git(["rev-parse", "--verify", "--quiet", start]).ok:
                start = self.cfg.base_branch
            res = self._git(["worktree", "add", "-b", branch, str(path), start])
        if not res.ok:
            log.error("git worktree add failed: %s", res.err.strip())
            return False
        return True

    def worktree_remove(self, path: Path) -> bool:
        """Remove a worktree. Forced — leftover uncommitted junk must not block
        cleanup (anything worth keeping was committed to the branch, which
        survives worktree removal). Falls back to a plain directory delete when
        git doesn't recognize the path as a worktree (a stray/half-created
        dir), so a later `worktree add` won't collide. Returns True once the
        path is gone."""
        if self.cfg.dry_run:
            log.info("[dry-run] would remove worktree %s", path)
            return True
        path = Path(path)
        if not path.exists():
            self._git(["worktree", "prune"])
            return True
        res = self._git(["worktree", "remove", "--force", str(path)])
        if not res.ok:
            # Not a registered worktree (or git refuses): remove the directory
            # directly, then prune the registration table. The branch and its
            # commits are unaffected.
            log.warning("git worktree remove failed (%s); removing dir directly",
                        res.err.strip())
            shutil.rmtree(path, ignore_errors=True)
            self._git(["worktree", "prune"])
        return not path.exists()

    def _legacy_merge(self, branch: str) -> bool:
        """merge_base for the non-worktree flow, which has no agent to hand a
        conflict to: a conflict is aborted and reported as failure."""
        status = self.merge_base(branch)
        if status == "conflict":
            self._git(["merge", "--abort"])
            log.error("merge conflict on %s (non-worktree flow); aborted", branch)
            return False
        return status != "error"

    def has_conflicts(self) -> bool:
        """True when the index has unmerged paths (an in-progress merge with
        unresolved conflicts)."""
        return bool(self._git(["ls-files", "--unmerged"]).out.strip())

    def merge_base(self, branch: str) -> str:
        """Fold the latest origin/<base> into a reused branch so resumed work
        starts from current base. Returns a status the caller acts on:

        - "ok":       merged cleanly, or nothing to merge.
        - "conflict": the merge conflicted and is LEFT IN PROGRESS in the
          worktree (markers + MERGE_HEAD) for the agent to resolve — factory
          no longer aborts+fails a whole item over a conflict.
        - "error":    the merge could not run; aborted to a clean state so work
          can still proceed on the existing base (a stale base is non-fatal —
          the PR diff handles it).
        """
        if self.cfg.dry_run:
            return "ok"
        base_ref = f"origin/{self.cfg.base_branch}"
        if not self._git(["rev-parse", "--verify", "--quiet", base_ref]).ok:
            return "ok"
        res = self._git(["merge", "--no-edit", base_ref])
        if res.ok:
            return "ok"
        if self.has_conflicts():
            log.warning("merge of %s into %s conflicted; leaving it for the "
                        "agent to resolve", base_ref, branch)
            return "conflict"
        self._git(["merge", "--abort"])
        log.error("could not merge %s into %s (non-conflict): %s; proceeding "
                  "on the existing base", base_ref, branch, res.err.strip())
        return "error"

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
        if self.has_conflicts():
            # `git add -A` would stage conflict markers as "resolved"; never
            # commit an unresolved merge. Leave it for the agent to finish.
            log.warning("unresolved merge conflicts present; not committing '%s'",
                        message)
            return False
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

    def unpushed(self, branch: str) -> int:
        """Commits on local HEAD not yet on `origin/<branch>`. 0 means the
        remote is up to date with HEAD (a push fully landed). -1 if it can't
        be determined (e.g. the remote-tracking ref doesn't exist yet). Call
        AFTER push: a successful `push -u` advances the local `origin/<branch>`
        ref, so 0 confirms GitHub actually has the commits."""
        if self.cfg.dry_run:
            return 0
        ref = f"origin/{branch}"
        if not self._git(["rev-parse", "--verify", "--quiet", ref]).ok:
            return -1
        res = self._git(["rev-list", "--count", f"{ref}..HEAD"])
        try:
            return int(res.out.strip()) if res.ok else -1
        except ValueError:
            return -1

    def commits_beyond(self, base_ref: str) -> int:
        """Number of commits on HEAD beyond `base_ref` (e.g. the spec commit
        plus each implementation commit). -1 if it can't be determined."""
        if not self._git(["rev-parse", "--verify", "--quiet", base_ref]).ok:
            return -1
        res = self._git(["rev-list", "--count", f"{base_ref}..HEAD"])
        try:
            return int(res.out.strip()) if res.ok else -1
        except ValueError:
            return -1

    def has_uncommitted(self) -> bool:
        return bool(self._git(["status", "--porcelain"]).out.strip())

    def add_exclude(self, patterns: list[str]) -> bool:
        """Add ignore patterns to THIS worktree's private exclude file
        (`$GIT_DIR/info/exclude`), so they never touch the tracked `.gitignore`
        and can't leak into a PR. Used to keep factory's `.factory/` scratch
        (memory, exec state) out of every commit — including printer's
        `git add -A` per-task commits."""
        if self.cfg.dry_run:
            return True
        res = self._git(["rev-parse", "--git-path", "info/exclude"])
        if not res.ok:
            log.error("could not resolve exclude path: %s", res.err.strip())
            return False
        exclude = Path(self.repo) / res.out.strip() if not Path(
            res.out.strip()).is_absolute() else Path(res.out.strip())
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text() if exclude.exists() else ""
            have = set(existing.splitlines())
            add = [p for p in patterns if p not in have]
            if add:
                with exclude.open("a") as fh:
                    if existing and not existing.endswith("\n"):
                        fh.write("\n")
                    fh.write("\n".join(add) + "\n")
            return True
        except OSError as e:  # noqa: BLE001
            log.error("could not write exclude file %s: %s", exclude, e)
            return False

    def has_changes_beyond(self, base_ref: str, exclude: list[str]) -> bool:
        """Does HEAD change anything vs the merge-base with `base_ref`,
        outside the `exclude`d paths? (The 'did the agent actually implement
        something beyond the spec commit' check.)"""
        if not self._git(["rev-parse", "--verify", "--quiet", base_ref]).ok:
            return True  # no base to compare against; assume changes
        res = self._git([
            "diff", "--quiet", f"{base_ref}...HEAD", "--", ".",
            *(f":(exclude){e}" for e in exclude),
        ])
        return not res.ok  # `diff --quiet` exits non-zero when there are diffs

    # --- self-update support: snapshot / rollback the running checkout ----------

    def current_branch(self) -> str:
        """Name of the currently checked-out branch (empty if detached)."""
        res = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        name = res.out.strip()
        return "" if name in ("", "HEAD") else name

    def head(self) -> str | None:
        """Current commit sha, for snapshot/rollback. None if not a repo."""
        res = self._git(["rev-parse", "HEAD"])
        return res.out.strip() if res.ok else None

    def reset_hard(self, ref: str) -> bool:
        """Discard tracked changes back to `ref` (used to roll back a bad self-update)."""
        if self.cfg.dry_run:
            log.info("[dry-run] would git reset --hard %s", ref)
            return True
        return self._git(["reset", "--hard", ref]).ok

    def clean_untracked(self) -> bool:
        """Remove untracked files/dirs (NOT git-ignored ones, so `.env` is safe).

        Pairs with `reset_hard` to fully revert a failed self-update — including
        any new files `printer` created — without touching ignored secrets/caches.
        """
        if self.cfg.dry_run:
            log.info("[dry-run] would git clean -fd (keeping ignored files)")
            return True
        return self._git(["clean", "-fd"]).ok
