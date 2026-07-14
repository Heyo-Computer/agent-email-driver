r"""The shared work pipeline: trigger -> draft PR -> printer -> ready -> update.

`process_item` is called for both Linear tickets and trigger emails. The two
sources differ only in how work is *claimed* (Linear state vs IMAP \Seen) and
how *completion* is reported (Linear state/comment vs SMTP reply). Everything in
between is identical.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from tools.notify import reply_subject
from util import log, slugify


@dataclass
class WorkItem:
    source: str            # "linear" | "email"
    title: str
    body: str              # becomes the spec input
    ref: str               # linear issue id, or email uid
    identifier: str = ""   # linear ENG-123 (for branch/state ops); "" for email
    reply_to: str = ""     # email sender address (email source only)
    reply_msgid: str = ""  # email Message-ID for threading (email source only)
    reply_refs: str = ""   # full References chain incl. reply_msgid (email only)
    reply_subject: str = ""  # original email subject, for in-thread replies
    target: str = "repo"   # "repo" (customer pipeline) | "self" (self-improvement)


class Pipeline:
    def __init__(self, cfg, *, git, gh, printer, specgen, linear, inbox, notifier,
                 journal=None, stop_check=None):
        self.cfg = cfg
        self.git = git
        self.gh = gh
        self.printer = printer
        self.specgen = specgen
        self.linear = linear
        self.inbox = inbox
        self.notifier = notifier
        self.journal = journal
        # Callable -> True when the daemon wants to shut down; lets the exec
        # wait loop end promptly (the detached exec itself keeps running).
        self.stop_check = stop_check
        # Worktree add/remove touch the shared main checkout (fetch, prune,
        # possibly a branch switch); serialize those across item threads.
        self._main_git_lock = threading.Lock()

    # --- naming (deterministic, so restarts re-derive the same branch/spec) ----

    def _names(self, item: WorkItem) -> tuple[str, str]:
        if item.identifier:
            stem = f"{item.identifier.lower()}-{slugify(item.title)}"
        else:
            stem = f"email-{slugify(item.title)}"
        branch = f"{self.cfg.branch_prefix}/{stem}"
        return branch, stem

    def _worktree(self, item: WorkItem) -> Path:
        _branch, stem = self._names(item)
        return self.cfg.worktrees_dir / stem

    # --- orchestration ---------------------------------------------------------

    def process(self, item: WorkItem, *, resume: bool = False) -> None:
        branch, stem = self._names(item)
        wt = self._worktree(item)
        log.info("=== processing%s %s [%s] '%s' -> %s (worktree %s)",
                 " (resume)" if resume else "",
                 item.source, item.identifier or item.ref, item.title, branch, wt)

        # Journal first: from here on a crash leaves a record to resume from.
        if self.journal:
            self.journal.record(item)

        # 1. isolated worktree with the item's branch checked out. Each item
        # gets its own checkout (and its own `.printer/` state) so work items
        # can never pollute each other or the main checkout.
        with self._main_git_lock:
            wt_ok = self.git.worktree_add(wt, branch)
        if not wt_ok:
            self._fail(item, None, "could not prepare git worktree")
            return
        git = self.git.for_repo(wt)
        printer = self.printer.for_repo(wt)

        # A detached exec may have kept running (or finished) while factory
        # was down. While one is active the tree belongs to the agent: skip
        # anything that would mutate it and go straight to re-attach/collect.
        exec_active = resume and printer.exec_active()
        if exec_active:
            log.info("live/finished detached exec found for '%s'; re-attaching",
                     item.title)
        else:
            # A crash mid-exec can leave uncommitted agent work in the reused
            # worktree; bank it before the base merge (which refuses a dirty
            # tree).
            if resume and git.has_uncommitted():
                git.commit_all(
                    f"factory: recover in-progress work for {item.title}")

            # Reused branches start from wherever they were cut; fold in
            # current base. (No-op for branches just created off origin/<base>.)
            if not git.merge_base(branch):
                self._fail(item, None,
                           f"could not merge latest {self.cfg.base_branch} "
                           f"into {branch}")
                return

        # 2. spec + commit + push. The spec is only generated once: on resume
        # the agent must keep working against the spec it started with (specgen
        # goes through claude, so regenerating wouldn't be deterministic).
        spec_path = wt / self.cfg.specs_dir / f"{stem}.md"
        if not spec_path.is_file():
            self.specgen.write_spec(spec_path, title=item.title, body=item.body)
        rel_spec = f"{self.cfg.specs_dir}/{spec_path.name}"
        if not git.commit_paths([rel_spec], f"factory: spec for {item.title}"):
            self._fail(item, None, "could not commit spec")
            return
        if not git.push(branch):
            self._fail(item, None, "could not push branch")
            return

        # 3. draft PR
        pr_body = self._pr_body(item, rel_spec)
        pr = self.gh.create_draft_pr(
            title=item.title, body=pr_body, head=branch, base=self.cfg.base_branch
        )
        if not pr:
            self._fail(item, None, "could not open draft PR")
            return

        # 4. claim (dedup commit point) + "started" notice
        self._claim(item, pr, resume=resume)

        # 5. execute (inside the worktree) — re-attaches to a live detached
        # exec, collects a finished one, or spawns fresh. Task completions
        # are surfaced to the requester as they happen so a long run doesn't
        # look dead.
        outcome = printer.exec_spec(
            spec_path,
            on_progress=self._progress_cb(item, pr, git, branch),
            stop_check=self.stop_check,
        )

        if outcome.interrupted:
            # Shutdown requested while the exec is still running. Push what's
            # committed so GitHub reflects the progress, tell the owner, and
            # leave the journal + worktree intact — the restarted daemon
            # re-attaches to the live exec.
            git.push(branch)
            self._notify_owner(
                item,
                f"factory stopping: {item.title}",
                f"factory is shutting down, but the implementation run "
                f"continues in the background. Committed progress has been "
                f"pushed to the draft PR:\n{pr}\n\n"
                f"factory will re-attach when it restarts.\n",
            )
            return

        # commit + push whatever printer produced (visible on the draft PR)
        git.commit_all(f"factory: implement {item.title}")
        git.push(branch)

        if not outcome.success:
            self._fail(item, pr, outcome.reason)
            return

        # A "successful" exec whose branch changes nothing beyond the spec
        # means the agent never actually did the work — keep the PR a draft
        # and flag it. (Diff-based so it stays correct across resumes and
        # re-attached execs, where commits can predate this call.)
        if not self.cfg.dry_run and not git.has_changes_beyond(
                f"origin/{self.cfg.base_branch}", [self.cfg.specs_dir]):
            self._fail(item, pr,
                       "printer exec exited 0 but produced no changes "
                       "beyond the spec")
            return

        # 6. ready for review
        self.gh.mark_ready(pr)

        # 7. completion update
        self._complete(item, pr)

    # --- notifications ----------------------------------------------------------

    def _progress_cb(self, item: WorkItem, pr: str, git, branch: str):
        """Progress reporter for the exec wait loop: pushes the branch (so
        the PR on GitHub always reflects committed progress, independent of
        printer's own per-task push), then emails the requester in-thread
        (and posts to Linear). Failures are logged, never raised — progress
        reporting must not take down the run."""
        def cb(titles: list[str], done: int, total: int) -> None:
            # Belt-and-suspenders push: the per-task commits exist locally
            # even if printer-side pushing is unavailable or failed. A push
            # failure goes INTO the email — silent staleness on the PR is
            # exactly how a healthy run looks dead.
            pushed = git.push(branch)
            finished = "\n".join(f"  - {t}" for t in titles)
            push_note = (
                f"Each completed task is committed and pushed — the draft PR "
                f"has the work so far:\n{pr}\n"
                if pushed else
                f"WARNING: pushing to GitHub FAILED — the commits exist only "
                f"on the factory server. Check /var/log/factory/ for the git "
                f"error. Draft PR (stale): {pr}\n"
            )
            body = (
                f"Progress: {done}/{total} tasks complete.\n\n"
                f"Just finished:\n{finished}\n\n{push_note}"
            )
            log.info("progress %d/%d: %s", done, total, "; ".join(titles))
            if item.source == "email" and item.reply_to:
                self._reply(item, body, to=item.reply_to)
            elif item.source == "linear":
                self.linear.comment(item.identifier or item.ref,
                                    f"⏳ {done}/{total} tasks done — latest: "
                                    f"{'; '.join(titles)}")
            self._notify_owner(item, f"factory progress: {item.title}", body)
        return cb

    def _reply(self, item: WorkItem, body: str, *, to: str) -> None:
        """Email a status update inside the original trigger's thread."""
        self.notifier.send(
            reply_subject(item.reply_subject or item.title),
            body,
            to=to,
            in_reply_to=item.reply_msgid or None,
            references=item.reply_refs or None,
        )

    def _notify_owner(self, item: WorkItem, subject: str, body: str) -> None:
        """Status notice to the owner (cfg.notify_to). For email-triggered work
        it threads into the trigger's conversation instead of starting a new
        one — and is skipped when the owner is the requester, who already got
        the in-thread reply."""
        if item.source == "email" and item.reply_msgid:
            if item.reply_to and item.reply_to.lower() == self.cfg.notify_to.lower():
                return
            self._reply(item, body, to=self.cfg.notify_to)
            return
        self.notifier.send(subject, body)

    # --- per-source claim / completion / failure -------------------------------

    def _claim(self, item: WorkItem, pr: str, *, resume: bool = False) -> None:
        if item.source == "linear":
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_inprogress_state)
            if not resume:
                self.linear.comment(item.identifier or item.ref,
                                    f"🏭 factory picked this up — draft PR: {pr}")
        elif item.source == "email":
            self.inbox.mark_seen(item.ref)
            if item.reply_to and not resume:
                # Acknowledge in-thread as soon as the draft PR exists, so the
                # requester knows their email was picked up (a second reply goes
                # out from _complete once it's ready for review). Skipped on
                # resume — they were already acknowledged before the crash.
                self._reply(
                    item,
                    f"factory picked up your request and opened a draft PR:\n\n"
                    f"{pr}\n\n"
                    f"It's being implemented now — you'll get another reply when "
                    f"it's ready for review.\n",
                    to=item.reply_to,
                )
        verb = "resumed" if resume else "started"
        self._notify_owner(
            item,
            f"factory {verb}: {item.title}",
            f"factory {verb} this item.\nSource: {item.source}\nDraft PR: {pr}\n",
        )

    def _complete(self, item: WorkItem, pr: str) -> None:
        if self.journal:
            self.journal.clear(item)
        with self._main_git_lock:
            self.git.worktree_remove(self._worktree(item))
        msg = f"PR ready for review: {pr}"
        if item.source == "linear":
            self.linear.comment(item.identifier or item.ref, f"✅ {msg}")
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_review_state)
        elif item.source == "email" and item.reply_to:
            self._reply(item, f"Your request is done.\n\n{msg}\n",
                        to=item.reply_to)
        self._notify_owner(item, f"factory done: {item.title}", msg)
        log.info("=== done %s '%s' -> %s", item.source, item.title, pr)

    def _fail(self, item: WorkItem, pr: str | None, reason: str) -> None:
        # Reported failures are terminal: the requester is told, so a restart
        # must not silently retry. Only an unreported interruption (crash)
        # leaves the journal entry (and its worktree) behind for resume.
        if self.journal:
            self.journal.clear(item)
        with self._main_git_lock:
            self.git.worktree_remove(self._worktree(item))
        log.warning("FAILED %s '%s': %s", item.source, item.title, reason)
        note = f"⚠️ factory could not finish this: {reason}"
        if pr:
            self.gh.comment(pr, note)
        if item.source == "linear" and self.cfg.linear_blocked_state:
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_blocked_state)
            self.linear.comment(item.identifier or item.ref, note)
        if item.source == "email" and item.reply_to:
            self._reply(
                item,
                f"factory could not finish this request.\n\nReason: {reason}\n"
                + (f"Draft PR (partial): {pr}\n" if pr else ""),
                to=item.reply_to,
            )
        self._notify_owner(
            item,
            f"factory FAILED: {item.title}",
            f"Source: {item.source}\nReason: {reason}\n"
            + (f"Draft PR: {pr}\n" if pr else ""),
        )

    def _pr_body(self, item: WorkItem, rel_spec: str) -> str:
        origin = (
            f"Linear issue {item.identifier}" if item.source == "linear"
            else f"email from {item.reply_to or 'inbox'}"
        )
        return (
            f"Automated PR opened by **factory** from {origin}.\n\n"
            f"**Request:** {item.title}\n\n"
            f"Spec: `{rel_spec}` (executed by `printer`). This PR starts as a "
            f"draft and is marked ready for review once `printer exec` succeeds.\n"
        )
