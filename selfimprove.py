#!/usr/bin/env python3
"""factory self-improvement: let the agent edit its OWN source via `printer`.

Where the normal pipeline runs `printer exec` against the customer repo, this
runs it against factory's own checkout (`FACTORY_SELF_PATH`, default: this dir),
then **builds** (compile + import smoke test), commits, optionally pushes, and
**restarts** factory via the supervisor so the new code takes effect.

Because a restart redeploys the *working tree*, self-updates happen on the
running branch (not a throwaway PR branch). The build gate is the safety net: if
`printer` or the build fails, the working tree is rolled back and factory is NOT
restarted.

Two ways in:
  * Triggered: a Linear ticket / email whose title starts with the self marker
    (default ``[self]``) — routed here by `factoryd.py`.
  * Manual CLI (below), e.g.::

      ./selfimprove.py "add a --status flag to factoryd"
      ./selfimprove.py "rework retries" --body-file note.md --no-restart
      echo "make logging JSON" | ./selfimprove.py "json logs" --body -
      ./selfimprove.py --build-only        # just validate the current source
      ./selfimprove.py --restart-only      # just restart via supervisor
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import Config
from tools.gitops import Git
from tools.inbox import Inbox
from tools.linear_mcp import Linear
from tools.notify import Notifier
from tools.printer import Printer
from tools.selfupdate import SelfUpdater
from tools.specgen import SpecGen
from util import log, setup_logging, slugify


class SelfImprover:
    """Run one self-improvement end-to-end against factory's own source."""

    def __init__(self, cfg: Config, *, linear=None, inbox=None, notifier=None):
        self.cfg = cfg
        self.src = cfg.factory_self_path
        self.git = Git(cfg, repo=self.src)
        self.printer = Printer(cfg, repo=self.src)
        self.specgen = SpecGen(cfg)
        self.updater = SelfUpdater(cfg)
        # Reporting channels are shared with the daemon when available so a
        # self-update reports back the same way ordinary work does.
        self.linear = linear or Linear(cfg)
        self.inbox = inbox or Inbox(cfg)
        self.notifier = notifier or Notifier(cfg)

    # --- the work item form (used by the daemon) -------------------------------

    def process(self, item) -> None:
        """Run a self-improvement for a WorkItem routed here by the daemon."""
        self.run(
            title=item.title,
            body=item.body,
            source=item.source,
            identifier=getattr(item, "identifier", ""),
            ref=getattr(item, "ref", ""),
            reply_to=getattr(item, "reply_to", ""),
            reply_msgid=getattr(item, "reply_msgid", ""),
        )

    # --- the core flow ---------------------------------------------------------

    def run(
        self,
        *,
        title: str,
        body: str,
        source: str = "manual",
        identifier: str = "",
        ref: str = "",
        reply_to: str = "",
        reply_msgid: str = "",
    ) -> bool:
        """Apply one improvement to factory's own source. Returns success.

        On success this may not return at all when `self_restart` is on — the
        restart SIGTERMs us mid-call — so all reporting happens *before* restart.
        """
        title = title.strip()
        stem = f"self-{slugify(title)}"
        spec_path = self.src / self.cfg.self_specs_dir / f"{stem}.md"
        log.info("=== self-improve [%s] '%s' -> %s", source, identifier or ref, title)

        snapshot = self.git.head()
        if snapshot is None and not self.cfg.dry_run:
            self._report_fail(source, title, identifier, ref, reply_to,
                              reply_msgid, "factory source is not a git checkout")
            return False
        branch = self.git.current_branch() or self.cfg.base_branch

        # 1. claim + "started" notice (so a crashed/looping run is visible)
        self._claim(source, title, identifier, ref)

        # 2. spec + 3. printer exec against factory's own source
        self.specgen.write_spec(spec_path, title=title, body=body)
        outcome = self.printer.exec_spec(spec_path)
        if not outcome.success:
            self._rollback(snapshot)
            self._report_fail(source, title, identifier, ref, reply_to,
                              reply_msgid, f"printer: {outcome.reason}")
            return False

        # 4. build gate — must pass before we keep or deploy the change
        build = self.updater.build()
        if not build.ok:
            self._rollback(snapshot)
            self._report_fail(source, title, identifier, ref, reply_to,
                              reply_msgid, build.err or "build failed")
            return False

        # 5. commit (and optionally push) the validated change
        if not self.git.commit_all(f"factory(self): {title}"):
            log.warning("self-improve: nothing committed (no changes?)")
        if self.cfg.self_push and not self.cfg.dry_run:
            self.git.push(branch)

        # 6. report success BEFORE restarting (restart may kill us)
        self._report_done(source, title, identifier, ref, reply_to, reply_msgid,
                          restarting=self.cfg.self_restart)

        # 7. restart so the new code runs
        if self.cfg.self_restart:
            self.updater.restart()
        log.info("=== self-improve done '%s'", title)
        return True

    # --- rollback --------------------------------------------------------------

    def _rollback(self, snapshot: str | None) -> None:
        if snapshot is None:
            return
        log.warning("self-improve: rolling back working tree to %s", snapshot[:8])
        self.git.reset_hard(snapshot)
        self.git.clean_untracked()

    # --- reporting (mirrors pipeline.py, minus the PR) -------------------------

    def _claim(self, source, title, identifier, ref) -> None:
        if source == "linear" and (identifier or ref):
            self.linear.set_state(identifier or ref, self.cfg.linear_inprogress_state)
            self.linear.comment(identifier or ref,
                                "🛠️ factory picked this up as a self-update.")
        elif source == "email" and ref:
            self.inbox.mark_seen(ref)
        self.notifier.send(f"factory self-update started: {title}",
                           f"Source: {source}\nTarget: factory itself ({self.src})\n")

    def _report_done(self, source, title, identifier, ref, reply_to, reply_msgid,
                     *, restarting: bool) -> None:
        tail = " factory is restarting to apply it." if restarting else ""
        msg = f"Self-update applied to factory's source.{tail}"
        if source == "linear" and (identifier or ref):
            self.linear.comment(identifier or ref, f"✅ {msg}")
            self.linear.set_state(identifier or ref, self.cfg.linear_review_state)
        elif source == "email" and reply_to:
            self.notifier.send(f"Re: {title}", f"{msg}\n", to=reply_to,
                               in_reply_to=reply_msgid or None)
        self.notifier.send(f"factory self-update done: {title}", msg)

    def _report_fail(self, source, title, identifier, ref, reply_to, reply_msgid,
                     reason) -> None:
        log.warning("self-improve FAILED '%s': %s", title, reason)
        note = (f"⚠️ factory could not apply this self-update: {reason}\n"
                "The working tree was rolled back; factory was not restarted.")
        if source == "linear" and (identifier or ref):
            if self.cfg.linear_blocked_state:
                self.linear.set_state(identifier or ref, self.cfg.linear_blocked_state)
            self.linear.comment(identifier or ref, note)
        elif source == "email" and reply_to:
            self.notifier.send(f"Re: {title}", note + "\n", to=reply_to,
                               in_reply_to=reply_msgid or None)
        self.notifier.send(f"factory self-update FAILED: {title}",
                           f"Source: {source}\nReason: {reason}\n")


def _read_body(arg: str | None) -> str:
    if arg is None:
        return ""
    if arg == "-":
        return sys.stdin.read()
    return arg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="selfimprove",
        description="Have factory improve its own source via printer, then "
                    "build and restart itself.",
    )
    ap.add_argument("title", nargs="?", help="what to change (becomes the spec title)")
    ap.add_argument("--body", help="request body / details ('-' reads stdin)")
    ap.add_argument("--body-file", help="read the request body from this file")
    ap.add_argument("--no-restart", action="store_true",
                    help="apply + build + commit but do not restart")
    ap.add_argument("--push", action="store_true",
                    help="also push the committed self-update to origin")
    ap.add_argument("--build-only", action="store_true",
                    help="just run the build gate against the current source")
    ap.add_argument("--restart-only", action="store_true",
                    help="just restart factory via the supervisor")
    ap.add_argument("--status", action="store_true",
                    help="print the supervisor status for factory and exit")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    cfg = Config.load()
    if args.push:
        cfg.self_push = True
    if args.no_restart:
        cfg.self_restart = False

    updater = SelfUpdater(cfg)
    if args.status:
        print(updater.status())
        return 0
    if args.build_only:
        res = updater.build()
        print(res.out.strip() or res.err.strip() or ("ok" if res.ok else "failed"))
        return 0 if res.ok else 1
    if args.restart_only:
        return 0 if updater.restart() else 1

    if not args.title:
        ap.error("a title is required (or use --build-only/--restart-only/--status)")
    body = _read_body(args.body)
    if args.body_file:
        body = (body + "\n" + Path(args.body_file).read_text()).strip()

    improver = SelfImprover(cfg)
    ok = improver.run(title=args.title, body=body, source="manual")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
