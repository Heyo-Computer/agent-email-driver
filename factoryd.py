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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config import Config
from pipeline import Pipeline, WorkItem
from selfimprove import SelfImprover
from tools.gh import Gh
from tools.gitops import Git
from tools.inbox import Inbox
from tools.journal import Journal
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
        self.journal = Journal(cfg)
        self.pipeline = Pipeline(
            cfg,
            git=Git(cfg),
            gh=Gh(cfg),
            printer=Printer(cfg),
            specgen=SpecGen(cfg),
            linear=self.linear,
            inbox=self.inbox,
            notifier=self.notifier,
            journal=self.journal,
            # Lets the exec wait loop end promptly on SIGTERM/SIGINT instead
            # of blocking until the supervisor SIGKILLs us; the detached exec
            # keeps running and is re-attached on restart.
            stop_check=lambda: _stop,
        )
        # Same reporting channels, but edits factory's own source instead of the
        # customer repo. Used for items whose title carries the self marker.
        self.self_improver = SelfImprover(
            cfg, linear=self.linear, inbox=self.inbox, notifier=self.notifier
        )
        # Work items run concurrently, each in its own worktree, so one
        # multi-hour exec can't block polling or other requests. `_inflight`
        # (keyed by source:ref) prevents the same item being picked up twice
        # while a thread is already on it.
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, cfg.max_concurrent), thread_name_prefix="item"
        )
        self._inflight: dict[str, str] = {}
        self._inflight_lock = threading.Lock()
        # Self-updates mutate factory's own source and restart the daemon;
        # never run two at once.
        self._self_lock = threading.Lock()

    @staticmethod
    def _item_key(item: WorkItem) -> str:
        return f"{item.source}:{item.identifier or item.ref}"

    def _submit(self, item: WorkItem, *, resume: bool = False) -> bool:
        """Queue an item for processing. Returns False if it's already
        in flight (dedup across polls and against startup resume)."""
        key = self._item_key(item)
        with self._inflight_lock:
            if key in self._inflight:
                return False
            self._inflight[key] = item.title

        def worker() -> None:
            try:
                if item.target == "self":
                    with self._self_lock:
                        self.self_improver.process(item)
                else:
                    self.pipeline.process(item, resume=resume)
            except Exception as e:  # noqa: BLE001 - one bad item must not kill others
                log.exception("unhandled error on %s '%s': %s",
                              item.source, item.title, e)
                try:
                    self.notifier.send(
                        f"factory ERROR: {item.title}",
                        f"Unhandled exception: {e}",
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._inflight_lock:
                    self._inflight.pop(key, None)

        self.executor.submit(worker)
        return True

    def _classify_target(self, title: str) -> tuple[str, str]:
        """Route by the self marker. Returns (target, cleaned_title).

        A leading `cfg.self_marker` (default `[self]`, case-insensitive) on the
        title routes the item to self-improvement, with the marker stripped from
        the title that becomes the spec.
        """
        marker = self.cfg.self_marker
        if marker and title.strip().lower().startswith(marker.lower()):
            return "self", title.strip()[len(marker):].strip() or title.strip()
        return "repo", title

    def collect(self) -> list[WorkItem]:
        items: list[WorkItem] = []
        if self.cfg.linear_enabled:
            try:
                for iss in self.linear.list_trigger_issues():
                    target, title = self._classify_target(iss.title)
                    items.append(
                        WorkItem(
                            source="linear",
                            title=title,
                            body=iss.description,
                            ref=iss.id,
                            identifier=iss.identifier,
                            target=target,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("error polling Linear: %s", e)
        if self.cfg.imap_enabled:
            try:
                for t in self.inbox.fetch_triggers():
                    target, title = self._classify_target(t.subject)
                    items.append(
                        WorkItem(
                            source="email",
                            title=title,
                            body=t.body,
                            ref=t.uid,
                            reply_to=t.sender,
                            reply_msgid=t.message_id,
                            reply_refs=t.references,
                            reply_subject=t.subject,
                            target=target,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("error polling inbox: %s", e)
        return items

    def resume_pending(self) -> None:
        """Re-queue work interrupted by a crash/restart (journal leftovers).

        Runs once at startup, before the first poll, so an exec that died
        mid-flight is picked up before any new triggers. Each entry gets a
        bounded number of attempts; past that the item is abandoned with a
        notification instead of crash-looping the daemon.
        """
        from dataclasses import fields as dc_fields
        known = {f.name for f in dc_fields(WorkItem)}
        for item_dict, attempts in self.journal.pending():
            item = WorkItem(**{k: v for k, v in item_dict.items() if k in known})
            attempt = self.journal.bump(item)
            if attempt > self.cfg.resume_max_attempts:
                self.journal.clear(item)
                log.error("giving up on '%s' after %d interrupted attempts",
                          item.title, attempts)
                self.notifier.send(
                    f"factory gave up: {item.title}",
                    f"This item was interrupted {attempts} times "
                    f"(crash or restart mid-run) and will not be retried.\n"
                    f"Source: {item.source}\n",
                )
                continue
            if item.target == "self":
                # Self-updates are never auto-resumed: a broken one could
                # crash-loop the daemon it is modifying. Report instead.
                self.journal.clear(item)
                self.notifier.send(
                    f"factory self-update interrupted: {item.title}",
                    "A self-update was interrupted mid-run and was NOT "
                    "resumed automatically. Re-send the request to retry.\n",
                )
                continue
            log.info("resuming in-flight %s '%s' (attempt %d/%d)",
                     item.source, item.title, attempt,
                     self.cfg.resume_max_attempts)
            self._submit(item, resume=True)

    def tick(self) -> int:
        items = self.collect()
        submitted = 0
        for item in items:
            if _stop:
                break
            if self._submit(item):
                submitted += 1
            else:
                log.debug("skipping %s '%s': already in flight",
                          item.source, item.title)
        return submitted

    def run_forever(self) -> None:
        cfg = self.cfg
        log.info(
            "factory up — repo=%s base=%s interval=%ss linear=%s imap=%s smtp=%s%s",
            cfg.repo_path, cfg.base_branch, cfg.poll_interval,
            cfg.linear_team or "off", "on" if cfg.imap_enabled else "off",
            "on" if cfg.smtp_enabled else "off",
            "  [DRY-RUN]" if cfg.dry_run else "",
        )
        try:
            self.resume_pending()
        except Exception as e:  # noqa: BLE001
            log.exception("error during startup resume: %s", e)
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
        # Workers see the stop flag through the pipeline's stop_check and wind
        # down within one exec poll (~5s); detached execs keep running.
        log.info("factory stopping; waiting for in-flight items to wind down")
        self.executor.shutdown(wait=True)
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

        # IMAP: dump the exact credentials loaded from the environment/.env so
        # they can be eyeballed, then attempt a real login. Secrets are printed
        # in full here on purpose — --probe is an on-demand check, not the
        # always-on daemon log, so this does not persist to the service log.
        if cfg.imap_enabled:
            log.info(
                "IMAP config: host=%r port=%r user=%r pass=%r folder=%r",
                cfg.imap_host, cfg.imap_port, cfg.imap_user,
                cfg.imap_pass, cfg.imap_folder,
            )
            try:
                conn = factory.inbox._connect()
                conn.logout()
                log.info("IMAP login: OK")
            except Exception as e:  # noqa: BLE001
                log.error("IMAP login: FAILED: %s", e)
        else:
            log.info("IMAP config: off (host/user/pass not all set)")

        # SMTP too, for symmetry — same credentials story bites notifications.
        if cfg.smtp_enabled:
            log.info(
                "SMTP config: host=%r port=%r user=%r pass=%r from=%r starttls=%r",
                cfg.smtp_host, cfg.smtp_port, cfg.smtp_user,
                cfg.smtp_pass, cfg.smtp_from, cfg.smtp_starttls,
            )
        else:
            log.info("SMTP config: off (host/from not set)")

        return 0 if gh_ok else 1

    if args.once:
        factory.resume_pending()
        n = factory.tick()
        factory.executor.shutdown(wait=True)
        log.info("single cycle complete (%d item(s)).", n)
        return 0

    factory.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
