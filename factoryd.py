#!/usr/bin/env python3
"""factory — autonomous ticket→PR agent.

Polls a Linear board (via the Linear MCP server) and an email inbox (IMAP) every
few minutes. Each new ticket or trigger email becomes: a draft PR on the target
repo, a spec, a `printer exec` run, a push, "ready for review", and a status
update back to Linear / by email.

Run: `python3 factoryd.py`  (config from environment / `.env`; see .env.example)
Flags: `--once` (single poll then exit), `--probe` (connectivity check + exit).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from config import Config
from pipeline import Pipeline, WorkItem
from tools.gh import Gh
from tools.gitops import Git
from tools.inbox import Inbox
from tools.linear_mcp import Linear
from tools.notify import Notifier
from tools.printer import Printer
from tools.specgen import SpecGen
from util import log, setup_logging

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("received signal %s; will stop after the current item", signum)
    _stop = True


class Factory:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.linear = Linear(cfg)
        self.inbox = Inbox(cfg)
        self.notifier = Notifier(cfg)
        self.pipeline = Pipeline(
            cfg,
            git=Git(cfg),
            gh=Gh(cfg),
            printer=Printer(cfg),
            specgen=SpecGen(cfg),
            linear=self.linear,
            inbox=self.inbox,
            notifier=self.notifier,
        )

    def collect(self) -> list[WorkItem]:
        items: list[WorkItem] = []
        if self.cfg.linear_enabled:
            try:
                for iss in self.linear.list_trigger_issues():
                    items.append(
                        WorkItem(
                            source="linear",
                            title=iss.title,
                            body=iss.description,
                            ref=iss.id,
                            identifier=iss.identifier,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("error polling Linear: %s", e)
        if self.cfg.imap_enabled:
            try:
                for t in self.inbox.fetch_triggers():
                    items.append(
                        WorkItem(
                            source="email",
                            title=t.subject,
                            body=t.body,
                            ref=t.uid,
                            reply_to=t.sender,
                            reply_msgid=t.message_id,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("error polling inbox: %s", e)
        return items

    def tick(self) -> int:
        items = self.collect()
        for item in items:
            if _stop:
                break
            try:
                self.pipeline.process(item)
            except Exception as e:  # noqa: BLE001 - one bad item must not kill the loop
                log.exception("unhandled error on %s '%s': %s",
                              item.source, item.title, e)
                try:
                    self.notifier.send(
                        f"factory ERROR: {item.title}",
                        f"Unhandled exception: {e}",
                    )
                except Exception:  # noqa: BLE001
                    pass
        return len(items)

    def run_forever(self) -> None:
        cfg = self.cfg
        log.info(
            "factory up — repo=%s base=%s interval=%ss linear=%s imap=%s smtp=%s%s",
            cfg.repo_path, cfg.base_branch, cfg.poll_interval,
            cfg.linear_team or "off", "on" if cfg.imap_enabled else "off",
            "on" if cfg.smtp_enabled else "off",
            "  [DRY-RUN]" if cfg.dry_run else "",
        )
        while not _stop:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.exception("error during poll cycle: %s", e)
            # Sleep in small slices so signals are honored promptly.
            slept = 0
            while slept < cfg.poll_interval and not _stop:
                time.sleep(min(2, cfg.poll_interval - slept))
                slept += 2
        log.info("factory stopped.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factoryd", description="factory daemon")
    ap.add_argument("--once", action="store_true",
                    help="run a single poll cycle and exit")
    ap.add_argument("--probe", action="store_true",
                    help="check Linear MCP / gh connectivity and exit")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    cfg = Config.load()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    factory = Factory(cfg)

    if args.probe:
        gh_ok = Gh(cfg).auth_ok()
        log.info("gh auth: %s", "OK" if gh_ok else "NOT authenticated")
        lin_ok = factory.linear.probe() if cfg.linear_enabled else None
        log.info("linear MCP reachable: %s",
                 "OK" if lin_ok else ("off" if lin_ok is None else "FAILED"))
        return 0 if gh_ok else 1

    if args.once:
        n = factory.tick()
        log.info("single cycle complete (%d item(s)).", n)
        return 0

    factory.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
