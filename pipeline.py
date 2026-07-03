r"""The shared work pipeline: trigger -> draft PR -> printer -> ready -> update.

`process_item` is called for both Linear tickets and trigger emails. The two
sources differ only in how work is *claimed* (Linear state vs IMAP \Seen) and
how *completion* is reported (Linear state/comment vs SMTP reply). Everything in
between is identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    target: str = "repo"   # "repo" (customer pipeline) | "self" (self-improvement)


class Pipeline:
    def __init__(self, cfg, *, git, gh, printer, specgen, linear, inbox, notifier):
        self.cfg = cfg
        self.git = git
        self.gh = gh
        self.printer = printer
        self.specgen = specgen
        self.linear = linear
        self.inbox = inbox
        self.notifier = notifier

    # --- naming (deterministic, so restarts re-derive the same branch/spec) ----

    def _names(self, item: WorkItem) -> tuple[str, str, Path]:
        if item.identifier:
            stem = f"{item.identifier.lower()}-{slugify(item.title)}"
        else:
            stem = f"email-{slugify(item.title)}"
        branch = f"{self.cfg.branch_prefix}/{stem}"
        spec_path = self.cfg.repo_path / self.cfg.specs_dir / f"{stem}.md"
        return branch, stem, spec_path

    # --- orchestration ---------------------------------------------------------

    def process(self, item: WorkItem) -> None:
        branch, stem, spec_path = self._names(item)
        log.info("=== processing %s [%s] '%s' -> %s",
                 item.source, item.identifier or item.ref, item.title, branch)

        # 1-2. branch + spec + commit + push
        if not self.git.prepare_branch(branch):
            self._fail(item, None, "could not prepare git branch")
            return
        self.specgen.write_spec(spec_path, title=item.title, body=item.body)
        rel_spec = f"{self.cfg.specs_dir}/{spec_path.name}"
        if not self.git.commit_paths([rel_spec], f"factory: spec for {item.title}"):
            self._fail(item, None, "could not commit spec")
            return
        if not self.git.push(branch):
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
        self._claim(item, pr)

        # 5. execute
        outcome = self.printer.exec_spec(spec_path)

        # commit + push whatever printer produced (visible on the draft PR)
        self.git.commit_all(f"factory: implement {item.title}")
        self.git.push(branch)

        if not outcome.success:
            self._fail(item, pr, outcome.reason)
            return

        # 6. ready for review
        self.gh.mark_ready(pr)

        # 7. completion update
        self._complete(item, pr)

    # --- per-source claim / completion / failure -------------------------------

    def _claim(self, item: WorkItem, pr: str) -> None:
        if item.source == "linear":
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_inprogress_state)
            self.linear.comment(item.identifier or item.ref,
                                 f"🏭 factory picked this up — draft PR: {pr}")
        elif item.source == "email":
            self.inbox.mark_seen(item.ref)
            if item.reply_to:
                # Acknowledge in-thread as soon as the draft PR exists, so the
                # requester knows their email was picked up (a second reply goes
                # out from _complete once it's ready for review).
                self.notifier.send(
                    f"Re: {item.title}",
                    f"factory picked up your request and opened a draft PR:\n\n"
                    f"{pr}\n\n"
                    f"It's being implemented now — you'll get another reply when "
                    f"it's ready for review.\n",
                    to=item.reply_to,
                    in_reply_to=item.reply_msgid or None,
                )
        self.notifier.send(
            f"factory started: {item.title}",
            f"Source: {item.source}\nDraft PR: {pr}\n",
        )

    def _complete(self, item: WorkItem, pr: str) -> None:
        msg = f"PR ready for review: {pr}"
        if item.source == "linear":
            self.linear.comment(item.identifier or item.ref, f"✅ {msg}")
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_review_state)
        elif item.source == "email" and item.reply_to:
            self.notifier.send(
                f"Re: {item.title}",
                f"Your request is done.\n\n{msg}\n",
                to=item.reply_to,
                in_reply_to=item.reply_msgid or None,
            )
        self.notifier.send(f"factory done: {item.title}", msg)
        log.info("=== done %s '%s' -> %s", item.source, item.title, pr)

    def _fail(self, item: WorkItem, pr: str | None, reason: str) -> None:
        log.warning("FAILED %s '%s': %s", item.source, item.title, reason)
        note = f"⚠️ factory could not finish this: {reason}"
        if pr:
            self.gh.comment(pr, note)
        if item.source == "linear" and self.cfg.linear_blocked_state:
            self.linear.set_state(item.identifier or item.ref,
                                  self.cfg.linear_blocked_state)
            self.linear.comment(item.identifier or item.ref, note)
        if item.source == "email" and item.reply_to:
            self.notifier.send(
                f"Re: {item.title}",
                f"factory could not finish this request.\n\nReason: {reason}\n"
                + (f"Draft PR (partial): {pr}\n" if pr else ""),
                to=item.reply_to,
                in_reply_to=item.reply_msgid or None,
            )
        self.notifier.send(
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
