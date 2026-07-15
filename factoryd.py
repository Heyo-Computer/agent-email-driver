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
import re
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
from tools.memory import Memory
from tools.notify import Notifier, reply_subject
from tools.printer import Printer
from tools.specgen import SpecGen
from util import log, setup_logging

_stop = False

# Leading reply/forward prefixes (repeated, any case) on an email subject.
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)


def _strip_reply_prefixes(subject: str) -> str:
    """`Re: Fwd: factory: x` -> `factory: x`, so a reply maps to the same
    title (and thus stem/worktree) as the original trigger."""
    return _REPLY_PREFIX.sub("", subject or "").strip()


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
        self.memory = Memory(cfg)
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
            memory=self.memory,
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

    @staticmethod
    def _item_from_dict(item_dict: dict) -> WorkItem:
        """Rebuild a WorkItem from a journal entry, tolerating schema drift
        (unknown keys from an older/newer factory are dropped)."""
        from dataclasses import fields as dc_fields
        known = {f.name for f in dc_fields(WorkItem)}
        return WorkItem(**{k: v for k, v in item_dict.items() if k in known})

    def _submit(self, item: WorkItem, *, resume: bool = False,
                quiet: bool = False) -> bool:
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
                    self.pipeline.process(item, resume=resume, quiet=quiet)
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
                    # A reply threaded to an in-flight item that says "nudge"
                    # kicks that run rather than starting a new one.
                    if self._maybe_nudge(t):
                        continue
                    # A reply threaded to an item blocked on a decision is the
                    # owner's answer — feed it back and resume, don't start new.
                    if self._maybe_answer(t):
                        continue
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

    def _maybe_nudge(self, trigger) -> bool:
        """If `trigger` is a nudge for an existing run, kick that run and
        consume the email. Returns True when handled; False lets the trigger
        flow through as normal work.

        A trigger is a nudge when its OWN text (not quoted thread history)
        carries a nudge marker AND it maps to an existing run. Mapping is by
        the deterministic subject→stem→worktree the pipeline already uses
        (robust to mail threading), with a message-id fallback. A brand-new
        email on a new subject maps to no existing worktree, so it stays
        normal work even if it says "nudge"."""
        from tools.inbox import _has_directive
        # Check the marker against the reply's OWN body (fall back to the
        # folded body for older triggers), never the quoted thread history.
        own = getattr(trigger, "own_body", "") or trigger.body
        if not _has_directive(trigger.subject, own, self.cfg.imap_nudge_markers):
            return False
        target = self._reply_target(trigger)
        # A message whose own text is essentially just "nudge" is a nudge, full
        # stop — never a new spec. If there's nothing to nudge, say so instead
        # of surprising the user with a brand-new PR. A substantial body that
        # merely mentions "nudge" with no matching run is left as normal work.
        pure = len(own.strip()) <= 50
        if target is None and not pure:
            log.info("nudge marker in substantial reply '%s' with no matching "
                     "run; treating as normal work", trigger.subject)
            return False
        self.inbox.mark_seen(trigger.uid)
        if target is None:
            log.info("pure nudge '%s' but no active run to kick", trigger.subject)
            if trigger.sender:
                self.notifier.send(
                    reply_subject(trigger.subject),
                    "Got your nudge, but there's no run to kick — it looks "
                    "finished or was never started. Reply with a fresh request "
                    "to start new work.\n",
                    to=trigger.sender, in_reply_to=trigger.message_id or None,
                    references=trigger.references or None,
                )
            return True
        ok = self.pipeline.request_nudge(target)
        log.info("nudge for '%s': %s", target.title,
                 "signalled" if ok else "no active run")
        body = (
            "Got your nudge — the run was kicked and will resume from its last "
            "checkpoint.\n"
            if ok else
            "Got your nudge, but there's no active run to kick right now (it "
            "may be paused or already finished). A paused run retries on its "
            "own; reply with a fresh request to start over.\n"
        )
        if trigger.sender:
            self.notifier.send(
                reply_subject(trigger.subject), body, to=trigger.sender,
                in_reply_to=trigger.message_id or None,
                references=trigger.references or None,
            )
        return True

    def _reply_target(self, trigger) -> WorkItem | None:
        """The existing run a nudge reply refers to, or None. Primary match:
        the de-`Re:`'d subject yields the same stem/worktree the original run
        used and that worktree still exists. Fallback: the reply's message-id
        chain matches a journaled item."""
        from tools.inbox import _msgids
        subj = _strip_reply_prefixes(trigger.subject)
        target_kind, title = self._classify_target(subj)
        synth = WorkItem(
            source="email", title=title, body="", ref=trigger.uid,
            reply_to=trigger.sender, reply_msgid=trigger.message_id,
            reply_refs=trigger.references, reply_subject=subj,
            target=target_kind,
        )
        # Primary: subject-derived worktree exists (an active or paused run).
        if target_kind != "self" and self.pipeline._worktree(synth).exists():
            return synth
        # Fallback: message-id references vs journaled in-flight items.
        refs = set(_msgids(trigger.references))
        for item_dict, _meta in self.journal.pending():
            mid = item_dict.get("reply_msgid")
            if mid and mid in refs:
                return self._item_from_dict(item_dict)
        return None

    # --- unblocking: owner answers via email reply or PR comment ----------------

    def _awaiting_match(self, trigger):
        """The journaled item AWAITING an answer that this reply refers to,
        matched by subject→worktree or message-id, plus its meta. (None, None)
        if no awaiting item matches."""
        from tools.inbox import _msgids
        subj = _strip_reply_prefixes(trigger.subject)
        kind, title = self._classify_target(subj)
        synth = WorkItem(source="email", title=title, body="", target=kind, ref="x")
        want_wt = self.pipeline._worktree(synth)
        refs = set(_msgids(trigger.references))
        for item_dict, meta in self.journal.awaiting():
            it = self._item_from_dict(item_dict)
            if self.pipeline._worktree(it) == want_wt:
                return it, meta
            if it.reply_msgid and it.reply_msgid in refs:
                return it, meta
        return None, None

    def _maybe_answer(self, trigger) -> bool:
        """If this reply is the owner's answer to a blocked item, deliver it and
        resume. Returns True when handled."""
        target, meta = self._awaiting_match(trigger)
        if target is None:
            return False
        answer = (getattr(trigger, "own_body", "") or trigger.body).strip()
        if not answer:
            return False
        self.inbox.mark_seen(trigger.uid)
        self._deliver_answer(target, meta, answer, via="email reply",
                             trigger=trigger)
        return True

    def _poll_pr_answers(self) -> None:
        """Scan the PRs of items awaiting a decision for a new owner comment;
        the first non-factory comment is taken as the answer."""
        bot = ""
        try:
            bot = self.pipeline.gh.bot_login()
        except Exception:  # noqa: BLE001
            pass
        for item_dict, meta in self.journal.awaiting():
            pr = meta.get("pr")
            if not pr:
                continue
            item = self._item_from_dict(item_dict)
            try:
                comments = self.pipeline.gh.list_comments(pr)
            except Exception as e:  # noqa: BLE001
                log.debug("could not read PR comments for %s: %s", pr, e)
                continue
            seen = set(meta.get("seen_comment_ids", []))
            new_ids, answer = [], None
            for cid, author, cbody in comments:
                if cid in seen:
                    continue
                new_ids.append(cid)
                if author and author == bot:
                    continue  # factory's own question comment
                if cbody.strip():
                    answer = cbody.strip()
                    break
            if new_ids:
                self.journal.mark_comments_seen(item, new_ids)
            if answer:
                self._deliver_answer(item, meta, answer, via="PR comment")

    def _deliver_answer(self, item, meta, answer: str, *, via: str,
                        trigger=None) -> None:
        """Inject an answer into the run and resume it. Clears the awaiting
        flag first so a concurrent poll can't deliver twice."""
        self.journal.clear_awaiting(item)
        ok = self.pipeline.apply_answer(item, answer, meta.get("question", ""))
        log.info("answer for '%s' via %s: %s", item.title, via,
                 "applied, resuming" if ok else "no workspace to resume")
        if ok:
            self._submit(item, resume=True, quiet=True)
        if trigger is not None and trigger.sender:
            ack = ("Thanks — feeding your answer to the agent and resuming from "
                   "where it stopped.\n" if ok else
                   "Got your answer, but the run's workspace is no longer "
                   "available; reply with a fresh request to restart.\n")
            self.notifier.send(
                reply_subject(trigger.subject), ack, to=trigger.sender,
                in_reply_to=trigger.message_id or None,
                references=trigger.references or None,
            )

    def resume_pending(self) -> None:
        """Re-queue work interrupted by a crash/restart (journal leftovers).

        Runs once at startup, before the first poll, so an exec that died
        mid-flight is picked up before any new triggers. Each entry gets a
        bounded number of attempts; past that the item is abandoned with a
        notification instead of crash-looping the daemon.
        """
        for item_dict, meta in self.journal.pending():
            item = self._item_from_dict(item_dict)
            if meta.get("awaiting_answer"):
                # Blocked on an owner decision — waits for an answer, not a
                # restart. Leave it parked (the answer pollers pick it up).
                log.info("'%s' is awaiting an owner answer; not auto-resuming",
                         item.title)
                continue
            attempt = self.journal.bump(item)
            if attempt > self.cfg.resume_max_attempts:
                self.journal.clear(item)
                log.error("giving up on '%s' after %d interrupted attempts",
                          item.title, meta["attempts"])
                self.notifier.send(
                    f"factory gave up: {item.title}",
                    f"This item was interrupted {meta['attempts']} times "
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
        # Retry items paused on a transient provider failure (credits/rate
        # limit) whose backoff has elapsed. In-flight dedupe makes this safe
        # to call every poll.
        for item_dict, meta in self.journal.due():
            if _stop:
                break
            item = self._item_from_dict(item_dict)
            if item.target == "self":
                continue  # self-updates are never auto-retried
            if self._submit(item, resume=True, quiet=True):
                submitted += 1
                log.info("retrying paused item '%s' (deferral %d)",
                         item.title, meta["deferrals"])
        # Owner answers posted as PR comments (the email channel is handled in
        # collect() via the inbox triggers).
        if not _stop:
            try:
                self._poll_pr_answers()
            except Exception as e:  # noqa: BLE001
                log.exception("error polling PR answers: %s", e)
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
