"""GitHub operations via the `gh` CLI.

Every GitHub action goes through `gh` with an explicit argument list (no shell),
mirroring the safety of /home/sarocu/Projects/backoffice-slack/gh.js but enabling
the write subcommands factory needs (pr create/ready/comment).
"""

from __future__ import annotations

from pathlib import Path

from util import Result, log, run


class Gh:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg.repo_path

    def _gh(self, args: list[str], timeout: int = 120) -> Result:
        return run(["gh", *args], cwd=self.repo, timeout=timeout)

    def create_draft_pr(
        self, *, title: str, body: str, head: str, base: str
    ) -> str | None:
        """Open a draft PR for `head` against `base`. Returns the PR URL."""
        if self.cfg.dry_run:
            log.info("[dry-run] would create draft PR %s -> %s: %s", head, base, title)
            return f"https://github.com/DRY-RUN/pull/0  (head={head})"
        res = self._gh(
            [
                "pr", "create",
                "--draft",
                "--base", base,
                "--head", head,
                "--title", title,
                "--body", body,
            ]
        )
        if not res.ok:
            # If a PR already exists for this branch, recover its URL.
            existing = self.pr_url_for_branch(head)
            if existing:
                log.info("draft PR already exists for %s: %s", head, existing)
                return existing
            log.error("gh pr create failed: %s", res.err.strip() or res.out.strip())
            return None
        url = res.out.strip().splitlines()[-1] if res.out.strip() else None
        log.info("opened draft PR: %s", url)
        return url

    def pr_url_for_branch(self, head: str) -> str | None:
        res = self._gh(
            ["pr", "list", "--head", head, "--state", "open", "--json", "url",
             "--jq", ".[0].url"]
        )
        url = res.out.strip()
        return url or None

    def mark_ready(self, pr: str) -> bool:
        """Mark a draft PR ready for review. `pr` is a URL or number."""
        if self.cfg.dry_run:
            log.info("[dry-run] would mark PR ready: %s", pr)
            return True
        res = self._gh(["pr", "ready", pr])
        if not res.ok:
            log.error("gh pr ready failed: %s", res.err.strip() or res.out.strip())
            return False
        log.info("marked PR ready for review: %s", pr)
        return True

    def comment(self, pr: str, body: str) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] would comment on PR %s: %s", pr, body[:80])
            return True
        res = self._gh(["pr", "comment", pr, "--body", body])
        if not res.ok:
            log.error("gh pr comment failed: %s", res.err.strip())
            return False
        return True

    def auth_ok(self) -> bool:
        return run(["gh", "auth", "status"]).ok
