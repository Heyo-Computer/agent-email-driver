"""IMAP inbox monitor — the second trigger source.

`fetch_triggers` returns unread messages that survive *triage* (see
`_triage_reject`); the subject becomes the work title and the plain-text body
becomes the spec. `mark_seen` flips a message to read, which is the dedup
mechanism so each email is processed exactly once.

Triage exists because the inbox is a shared, spammy surface: on boot every
unread message — newsletters, CI notifications, calendar invites — would
otherwise each spawn a PR. Two gates must both pass before an email is treated
as work:

  1. **sender allowlist** — the From address (or its domain) must be in
     `FACTORY_IMAP_ALLOWED_SENDERS`. Not everyone gets to trigger a PR.
  2. **explicit directive** — the message must actually ask the agent to do
     something, signalled by one of `FACTORY_IMAP_DIRECTIVE_MARKERS` appearing
     in the subject or body (e.g. `factory:`). This stops an allowlisted human's
     ordinary correspondence (or an auto-reply from their address) from being
     mistaken for a request.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from util import log

# Cap thread reconstruction so a long history can't produce a giant spec.
_MAX_THREAD_MSGS = 20
_MAX_THREAD_CHARS = 8000


@dataclass
class Trigger:
    uid: str
    sender: str           # bare address, e.g. sam@sarocu.com
    sender_full: str      # display form for replies
    subject: str
    body: str
    message_id: str       # for In-Reply-To threading on the SMTP reply
    references: str = ""  # full thread chain (References + own id), space-joined


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _plain_body(msg: email.message.Message) -> str:
    """Extract the best plain-text body, falling back to stripped HTML."""
    if msg.is_multipart():
        # Prefer text/plain.
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return _payload_text(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                return _strip_html(_payload_text(part))
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html(_payload_text(msg))
    return _payload_text(msg)


def _payload_text(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _msgids(*headers: str | None) -> list[str]:
    """Extract `<message-id>` tokens (with angle brackets) from header values,
    de-duplicated, preserving first-seen order."""
    seen: list[str] = []
    for h in headers:
        if not h:
            continue
        for mid in re.findall(r"<[^>]+>", h):
            if mid not in seen:
                seen.append(mid)
    return seen


def _sender_allowed(sender: str, allowed: list[str]) -> bool:
    """Is `sender` (a bare address) on the allowlist?

    An allowlist entry is either a full address (`sam@sarocu.com`) or a bare
    domain (`@example.com`, matching anyone at that domain). Empty allowlist
    means "nobody is allowlisted" — the gate rejects, since an unconfigured
    allowlist must not silently let the whole inbox trigger PRs.
    """
    if not sender:
        return False
    sender = sender.lower()
    domain = "@" + sender.split("@", 1)[1] if "@" in sender else ""
    for entry in allowed:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if domain and domain == entry:
                return True
        elif entry == sender:
            return True
    return False


def _has_directive(subject: str, body: str, markers: list[str]) -> bool:
    """Does the message explicitly ask the agent to act?

    True if any marker appears (case-insensitively) in the subject or body. An
    empty marker list disables this gate (any content counts as a request).
    """
    hay = f"{subject}\n{body}".lower()
    active = [m.strip().lower() for m in markers if m.strip()]
    if not active:
        return True
    return any(m in hay for m in active)


class Inbox:
    def __init__(self, cfg):
        self.cfg = cfg

    def _triage_reject(self, sender: str, subject: str, body: str) -> str | None:
        """Return a reason string if this message should be skipped, else None.

        Both gates are enforced here so the decision (and its rationale) lives
        in one place and is logged uniformly.
        """
        cfg = self.cfg
        if not _sender_allowed(sender, cfg.imap_allowed_senders):
            if not cfg.imap_allowed_senders:
                return ("no allowlist configured (set FACTORY_IMAP_ALLOWED_SENDERS "
                        "to enable email triggers)")
            return f"sender {sender!r} not in allowlist"
        if cfg.imap_require_directive and not _has_directive(
            subject, body, cfg.imap_directive_markers
        ):
            return (f"no directive marker "
                    f"({', '.join(cfg.imap_directive_markers)}) in subject/body")
        return None

    def _connect(self) -> imaplib.IMAP4:
        cfg = self.cfg
        if cfg.imap_port == 143:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
            conn.starttls()
        else:
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
        conn.login(cfg.imap_user, cfg.imap_pass)
        conn.select(cfg.imap_folder)
        return conn

    def _thread_context(self, conn: imaplib.IMAP4, msg) -> str:
        """Reconstruct earlier messages in this email's thread as a transcript.

        A reply usually only restates the latest ask; the request it refers to
        ("do what we discussed") lives in the messages before it. We look up the
        Message-IDs from References / In-Reply-To in the current folder and
        render them oldest-first so the spec carries the whole conversation.
        Messages not present in this folder (e.g. only in Sent) are skipped.
        """
        own = (msg.get("Message-ID") or "").strip()
        refs = [m for m in _msgids(msg.get("References"), msg.get("In-Reply-To"))
                if m != own]
        if not refs:
            return ""
        collected: list[tuple[float, str, str]] = []
        for mid in refs[-_MAX_THREAD_MSGS:]:
            try:
                typ, data = conn.uid("search", None, "HEADER", "Message-ID",
                                     f'"{mid}"')
                if typ != "OK" or not data or not data[0]:
                    continue
                muid = data[0].split()[0]
                typ, fetched = conn.uid("fetch", muid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                m = email.message_from_bytes(fetched[0][1])
            except Exception as e:  # noqa: BLE001 - context is best-effort
                log.debug("inbox: thread fetch failed for %s: %s", mid, e)
                continue
            try:
                dt = parsedate_to_datetime(m.get("Date")) if m.get("Date") else None
                # .timestamp() copes with both aware and naive datetimes, so the
                # sort below never trips over a tz mismatch.
                order = dt.timestamp() if dt else float("inf")
                stamp = dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown date"
            except Exception:  # noqa: BLE001
                order, stamp = float("inf"), "unknown date"
            frm = _decode(m.get("From")) or "unknown sender"
            collected.append((order, stamp, frm, _plain_body(m).strip()))
        if not collected:
            return ""
        collected.sort(key=lambda c: c[0])
        blocks = [f"**From {frm} — {stamp}**\n{body or '(no text)'}"
                  for _order, stamp, frm, body in collected]
        transcript = "\n\n".join(blocks).strip()
        if len(transcript) > _MAX_THREAD_CHARS:
            transcript = (transcript[:_MAX_THREAD_CHARS].rstrip()
                          + "\n\n…(earlier thread truncated)")
        return transcript

    def fetch_triggers(self) -> list[Trigger]:
        """Return UNSEEN messages that survive triage (sender + directive gates).

        Messages are NOT marked read here — call mark_seen after the work item
        has been claimed (a draft PR exists) so a crash before claiming leaves
        the email re-processable. Triaged-out messages are also left unread.
        """
        cfg = self.cfg
        if not cfg.imap_enabled:
            return []
        triggers: list[Trigger] = []
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            log.error("IMAP connect failed: %s", e)
            return []
        try:
            # PEEK so the search itself does not set \Seen.
            typ, data = conn.uid("search", None, "UNSEEN")
            if typ != "OK":
                log.error("IMAP search failed: %s", data)
                return []
            uids = data[0].split()
            for uid in uids:
                uid_s = uid.decode()
                typ, fetched = conn.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = email.message_from_bytes(fetched[0][1])
                sender_full = _decode(msg.get("From"))
                sender = parseaddr(sender_full)[1].lower()
                subject = _decode(msg.get("Subject")).strip() or "(no subject)"
                body = _plain_body(msg).strip()
                reason = self._triage_reject(sender, subject, body)
                if reason:
                    # Leave the message UNSEEN so a later allowlist/marker fix
                    # can still pick it up; just don't act on it now.
                    log.info("inbox: triaged out uid=%s from %s: %s",
                             uid_s, sender or "?", reason)
                    continue
                # If this is a reply, fold the earlier thread in as context so
                # the spec reflects the whole conversation, not just the reply.
                context = ""
                try:
                    context = self._thread_context(conn, msg)
                except Exception as e:  # noqa: BLE001 - never fail a trigger on this
                    log.debug("inbox: thread context failed for uid=%s: %s",
                              uid_s, e)
                if context:
                    body = (
                        f"{body}\n\n---\n\n"
                        f"## Earlier messages in this thread (context)\n\n"
                        f"{context}\n"
                    )
                    log.info("inbox: attached thread context to uid=%s", uid_s)
                # Full chain for the reply's References header: the trigger's
                # own ancestry plus its Message-ID, so our replies thread even
                # when the trigger is itself deep in a conversation.
                own_mid = (msg.get("Message-ID") or "").strip()
                chain = _msgids(msg.get("References"), msg.get("In-Reply-To"))
                if own_mid and own_mid not in chain:
                    chain.append(own_mid)
                triggers.append(
                    Trigger(
                        uid=uid_s,
                        sender=sender,
                        sender_full=sender_full,
                        subject=subject,
                        body=body,
                        message_id=own_mid,
                        references=" ".join(chain),
                    )
                )
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        if triggers:
            log.info("inbox: %d trigger email(s)", len(triggers))
        return triggers

    def mark_seen(self, uid: str) -> None:
        if self.cfg.dry_run:
            log.info("[dry-run] would mark email uid=%s as seen", uid)
            return
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            log.error("IMAP connect failed (mark_seen): %s", e)
            return
        try:
            conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
