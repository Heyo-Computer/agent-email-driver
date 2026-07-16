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
                 journal=None, stop_check=None, memory=None):
        self.cfg = cfg
        self.git = git
        self.gh = gh
        self.printer = printer
        self.specgen = specgen
        self.linear = linear
        self.inbox = inbox
        self.notifier = notifier
        self.journal = journal
        self.memory = memory
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

    def _append_memory(self, spec_path: Path, mem_index: str) -> None:
        """Append the working-memory section to a freshly written spec."""
        try:
            with spec_path.open("a") as fh:
                fh.write(f"\n\n---\n\n{mem_index}")
        except OSError as e:  # noqa: BLE001
            log.error("could not append memory to spec: %s", e)

    def _append_conflict_instruction(self, spec_path: Path, branch: str) -> None:
        """Tell the agent to resolve the in-progress base merge before anything
        else. The worktree has an active merge (MERGE_HEAD + conflict markers);
        the agent has full git/file tooling to resolve it."""
        section = (
            "\n\n---\n\n## FIRST: resolve the in-progress merge conflict\n\n"
            f"This worktree has an in-progress merge of the latest "
            f"`{self.cfg.base_branch}` into `{branch}` that stopped on "
            "conflicts. Before doing any task work, resolve it:\n\n"
            "1. Run `git status` to see the conflicted files.\n"
            "2. Edit each one to resolve the `<<<<<<<`/`=======`/`>>>>>>>` "
            "markers, keeping both the branch's intent and the incoming "
            "changes from the base.\n"
            "3. `git add` the resolved files and `git commit --no-edit` to "
            "complete the merge.\n"
            "4. Confirm `git status` is clean, then proceed with the tasks "
            "below.\n\n"
            "If a conflict genuinely needs an owner decision you cannot make, "
            "emit the blocked sentinel with a clear question rather than "
            "guessing.\n"
        )
        try:
            with spec_path.open("a") as fh:
                fh.write(section)
        except OSError as e:  # noqa: BLE001
            log.error("could not append conflict instruction to spec: %s", e)

    # --- orchestration ---------------------------------------------------------

    def process(self, item: WorkItem, *, resume: bool = False,
                quiet: bool = False) -> None:
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

        # Keep factory's scratch (memory, exec state) out of every commit —
        # including printer's per-task `git add -A` — via the worktree's
        # private exclude, then stage the durable memory store into the tree
        # for the agent to recall from.
        mem_index = ""
        if self.memory:
            git.add_exclude([".factory/"])
            mem_index = self.memory.install(wt)

        # A detached exec may have kept running (or finished) while factory
        # was down. While one is active the tree belongs to the agent: skip
        # anything that would mutate it and go straight to re-attach/collect.
        exec_active = resume and printer.exec_active()
        merge_conflict = False
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
            # current base. A conflict is NOT fatal: it's left in the tree for
            # the agent to resolve as its first task. A non-conflict merge
            # error is non-fatal too — proceed on the existing base.
            merge_conflict = git.merge_base(branch) == "conflict"

        # 2. spec + commit + push. The spec is only generated once: on resume
        # the agent must keep working against the spec it started with (specgen
        # goes through claude, so regenerating wouldn't be deterministic).
        spec_path = wt / self.cfg.specs_dir / f"{stem}.md"
        if not spec_path.is_file():
            self.specgen.write_spec(spec_path, title=item.title, body=item.body)
            if mem_index:
                self._append_memory(spec_path, mem_index)
        if merge_conflict:
            self._append_conflict_instruction(spec_path, branch)
        rel_spec = f"{self.cfg.specs_dir}/{spec_path.name}"
        # With a merge in progress the tree has conflict markers — committing
        # now would bake them in. Skip the spec commit/push and let the agent
        # resolve the merge first; the appended instruction is read from the
        # working tree and committed with the merge resolution. (A conflict
        # only happens on a reused branch, whose spec + PR already exist.)
        if not merge_conflict:
            if not git.commit_paths([rel_spec],
                                    f"factory: spec for {item.title}"):
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
        self._claim(item, pr, resume=resume, quiet=quiet)

        # 5. execute (inside the worktree) — re-attaches to a live detached
        # exec, collects a finished one, or spawns fresh. Task completions
        # are surfaced to the requester as they happen so a long run doesn't
        # look dead.
        outcome = printer.exec_spec(
            spec_path,
            on_progress=self._progress_cb(item, pr, git, branch),
            stop_check=self.stop_check,
            on_nudge=self._nudge_cb(item, pr, git, branch),
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

        # Harvest anything the agent learned this run into the durable store
        # (idempotent + deduped). Skipped above for `interrupted` because the
        # detached exec is still live and may be mid-write to the inbox.
        if self.memory:
            try:
                n = self.memory.harvest(wt)
                if n:
                    log.info("memory: harvested %d learning(s) from '%s'",
                             n, item.title)
            except Exception as e:  # noqa: BLE001 - memory must not fail a run
                log.error("memory harvest failed: %s", e)

        # commit + push whatever the agent produced (visible on the draft PR)
        git.commit_all(f"factory: implement {item.title}")
        pushed = git.push(branch)

        if not outcome.success:
            if outcome.blocked and self.journal:
                # The agent needs an owner decision. Not a failure: keep the
                # work resumable and wait for an answer (email reply or PR
                # comment), then resume from the checkpoint.
                self._await_answer(item, pr, outcome.question or outcome.reason)
                return
            if (outcome.transient or outcome.stalled) and self.journal:
                # Not a real failure: either the provider said "not right now"
                # (credits/rate limit/overload), or the exec wedged and
                # auto-nudges couldn't clear it. Keep the journal entry and
                # worktree, schedule a retry, and say so — the exec resumes
                # from printer's checkpoint with all completed tasks intact.
                cause = ("a stall that automatic nudges could not clear"
                         if outcome.stalled
                         else "a temporary provider limit")
                self._pause(item, pr, outcome.reason, cause=cause)
                return
            self._fail(item, pr, outcome.reason)
            return

        # If the agent finished but left the base merge unresolved, the branch
        # still has conflict markers — never present that as ready. Ask for a
        # decision (resumable) rather than merging a broken tree.
        if not self.cfg.dry_run and git.has_conflicts():
            if self.journal:
                self._await_answer(
                    item, pr,
                    f"The merge of the latest {self.cfg.base_branch} into this "
                    f"branch has conflicts the agent could not resolve on its "
                    f"own. Resolve the conflicts on the branch (or advise how), "
                    f"then reply to resume.")
            else:
                self._fail(item, pr, "unresolved merge conflicts remain")
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

        # HARD GATE: never mark a PR ready unless GitHub verifiably has the
        # commits. A silently-failed push must not present as "ready for
        # review". Treat a push that didn't land as retryable (usually
        # transient/network; a persistent auth issue surfaces via deferral
        # notices) rather than a terminal failure.
        if not self.cfg.dry_run and (not pushed or git.unpushed(branch) != 0):
            reason = ("commits could not be pushed to GitHub (the work is "
                      "committed on the factory server but the push did not "
                      "land)")
            if self.journal:
                self._pause(item, pr, reason, cause="a GitHub push failure")
            else:
                self._fail(item, pr, reason)
            return

        # 6. ready for review
        self.gh.mark_ready(pr)

        # 7. completion update
        self._complete(item, pr)

    # --- notifications ----------------------------------------------------------

    def _push_verified(self, git, branch: str,
                       message: str = "factory: task progress") -> tuple[bool, str]:
        """Commit the worktree, push, and VERIFY the remote actually advanced.
        Returns (ok, human note). factory is the sole committer/pusher, so this
        is the one place that decides whether GitHub really has the work — the
        email never claims 'pushed' on faith."""
        committed = git.commit_all(message)
        pushed = git.push(branch)
        unpushed = git.unpushed(branch) if pushed else -1
        beyond = git.commits_beyond(f"origin/{self.cfg.base_branch}")
        ok = pushed and unpushed == 0
        if not ok:
            return False, (
                f"WARNING: commits are NOT on GitHub yet — the push "
                f"{'failed' if not pushed else 'did not fully land'} "
                f"(unpushed={unpushed}). The work is safe on the factory "
                f"server; check /var/log/factory/ for the git error."
            )
        if beyond <= 1:
            # Pushed cleanly, but nothing beyond the spec commit exists — the
            # agent marked tasks done without producing committed changes.
            return False, (
                "WARNING: tasks are being marked done but NO code has been "
                "committed to the branch yet — the run may not be writing "
                "files, or writing only ignored paths."
            )
        return True, f"{beyond - 1} implementation commit(s) are on the draft PR."

    def _progress_cb(self, item: WorkItem, pr: str, git, branch: str):
        """Progress reporter for the exec wait loop. On each task completion it
        commits, pushes, and verifies the remote advanced, then reports the
        VERIFIED state (never a bare 'pushed' claim). Best-effort; never raises
        into the wait loop."""
        def cb(titles: list[str], done: int, total: int) -> None:
            msg = f"factory: complete task(s): {'; '.join(titles)}"[:200]
            ok, note = self._push_verified(git, branch, message=msg)
            finished = "\n".join(f"  - {t}" for t in titles)
            body = (
                f"Progress: {done}/{total} tasks complete.\n\n"
                f"Just finished:\n{finished}\n\n{note}\n\nDraft PR:\n{pr}\n"
            )
            log.info("progress %d/%d (%s): %s", done, total,
                     "pushed" if ok else "NOT pushed", "; ".join(titles))
            if item.source == "email" and item.reply_to:
                self._reply(item, body, to=item.reply_to)
            elif item.source == "linear":
                flag = "" if ok else " ⚠️ not pushed"
                self.linear.comment(item.identifier or item.ref,
                                    f"⏳ {done}/{total} tasks done{flag} — "
                                    f"latest: {'; '.join(titles)}")
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

    def _claim(self, item: WorkItem, pr: str, *, resume: bool = False,
               quiet: bool = False) -> None:
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
        if quiet:
            # Scheduled retry of a paused item: the pause notice already told
            # everyone; re-announcing every backoff cycle is noise.
            return
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

    def _pause(self, item: WorkItem, pr: str, reason: str, *,
               cause: str = "a temporary provider limit") -> None:
        """Deferred-retry pause (transient provider failure, or an unclearable
        stall): defer the journaled item for retry and notify on the first
        pause only (a long outage must not send an email every retry cycle,
        but going quiet forever is how the agent looks dead — so every 20th
        deferral re-notifies)."""
        delay = self.cfg.retry_delay
        n = self.journal.defer(item, delay)
        log.warning("PAUSED %s '%s' (deferral %d, retry in %ds): %s",
                    item.source, item.title, n, delay, reason)
        if n == 1 or n % 20 == 0:
            body = (
                f"factory paused this item after hitting {cause}:\n\n"
                f"  {reason}\n\n"
                f"Completed work is committed and pushed to the draft PR:\n"
                f"{pr}\n\n"
                f"It will retry automatically about every "
                f"{delay // 60} minutes (restarting factory retries "
                f"immediately).\n"
            )
            if item.source == "email" and item.reply_to:
                self._reply(item, body, to=item.reply_to)
            self._notify_owner(item, f"factory paused: {item.title}", body)

    def _await_answer(self, item: WorkItem, pr: str, question: str) -> None:
        """Park a blocked item until the owner answers. Keeps the journal entry
        and worktree (resumable), surfaces the question on the PR and in-thread,
        and does NOT mark ready or auto-retry."""
        self.journal.block_on_answer(item, pr, question)
        headline = question.splitlines()[0] if question else question
        log.warning("BLOCKED %s '%s' awaiting owner answer: %s",
                    item.source, item.title, headline)
        # Render the (possibly multi-line) question as a markdown blockquote on
        # the PR, and as an indented block in the email.
        quoted_md = "\n".join(f"> {ln}" for ln in question.splitlines())
        quoted_txt = "\n".join(f"    {ln}" for ln in question.splitlines())
        self.gh.comment(
            pr,
            f"⏸️ **factory needs a decision to continue**\n\n{quoted_md}\n\n"
            f"Reply to the factory email thread, or comment here with your "
            f"answer, and factory will feed it back to the agent and resume "
            f"from where it stopped.")
        body = (
            f"The agent stopped and needs a decision from you before it can "
            f"continue. Here is what it's asking:\n\n"
            f"{quoted_txt}\n\n"
            f"Reply to this email with your answer (or comment on the PR) and "
            f"factory will resume from where it left off — completed work is "
            f"kept. Draft PR:\n{pr}\n"
        )
        if item.source == "email" and item.reply_to:
            self._reply(item, body, to=item.reply_to)
        elif item.source == "linear":
            self.linear.comment(item.identifier or item.ref,
                                f"⏸️ needs a decision to continue:\n\n{quoted_md}")
        self._notify_owner(item, f"factory needs a decision: {item.title}", body)

    def apply_answer(self, item: WorkItem, answer: str, question: str = "") -> bool:
        """Inject an owner's answer into the worktree spec and commit it, so a
        subsequent resume feeds it to the agent. Returns False if the worktree
        is gone (nothing to resume)."""
        wt = self._worktree(item)
        if not wt.exists():
            return False
        _branch, stem = self._names(item)
        spec_path = wt / self.cfg.specs_dir / f"{stem}.md"
        if not spec_path.is_file():
            return False
        section = (
            "\n\n---\n\n## Owner decision (resolves a blocker)\n\n"
            + (f"**Question:** {question}\n\n" if question else "")
            + f"**Answer:** {answer.strip()}\n\n"
            "Incorporate this decision and continue. Un-block any task that was "
            "waiting on it (`printer task start <id>`), then proceed.\n"
        )
        try:
            with spec_path.open("a") as fh:
                fh.write(section)
        except OSError as e:  # noqa: BLE001
            log.error("could not write answer to spec: %s", e)
            return False
        git = self.git.for_repo(wt)
        git.add_exclude([".factory/"])
        rel = f"{self.cfg.specs_dir}/{spec_path.name}"
        git.commit_paths([rel], f"factory: owner answer for {item.title}")
        git.push(_branch)
        log.info("applied owner answer to '%s'", item.title)
        return True

    def _nudge_cb(self, item: WorkItem, pr: str, git, branch: str):
        """Reporter fired when a wedged exec is nudged (killed + resumed from
        checkpoint). Pushes committed progress and tells the requester the run
        was stuck and has been restarted — so a stall reads as 'kicked', not
        'dead'. Best-effort; never raises into the wait loop."""
        def cb(reason: str, manual: bool) -> None:
            kind = "at your request" if manual else f"automatically ({reason})"
            git.push(branch)
            body = (
                f"factory found this run stuck and nudged it {kind}: the "
                f"agent was killed and resumed from its last checkpoint, so "
                f"completed tasks are kept and it picks up where it left "
                f"off.\n\nDraft PR:\n{pr}\n"
            )
            log.info("nudged '%s' (%s)", item.title, reason)
            if item.source == "email" and item.reply_to:
                self._reply(item, body, to=item.reply_to)
            elif item.source == "linear":
                self.linear.comment(item.identifier or item.ref,
                                    f"👋 nudged a stuck run ({reason}); resumed "
                                    f"from checkpoint")
            self._notify_owner(item, f"factory nudged: {item.title}", body)
        return cb

    def request_nudge(self, item: WorkItem) -> bool:
        """Signal a running exec for `item` to nudge itself. Returns False if
        the item has no active worktree/exec to nudge."""
        wt = self._worktree(item)
        if not wt.exists():
            return False
        return self.printer.for_repo(wt).request_nudge()

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
