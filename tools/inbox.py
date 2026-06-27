"""IMAP inbox monitor — the second trigger source.

`fetch_triggers` returns unread messages (optionally restricted to an allowed
sender list); subject becomes the work title and the plain-text body becomes the
spec. `mark_seen` flips a message to read, which is the dedup mechanism so each
email is processed exactly once.
"""

from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr

from util import log


@dataclass
class Trigger:
    uid: str
    sender: str           # bare address, e.g. sam@sarocu.com
    sender_full: str      # display form for replies
    subject: str
    body: str
    message_id: str       # for In-Reply-To threading on the SMTP reply


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


class Inbox:
    def __init__(self, cfg):
        self.cfg = cfg

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

    def fetch_triggers(self) -> list[Trigger]:
        """Return UNSEEN messages that pass the sender allowlist.

        Messages are NOT marked read here — call mark_seen after the work item
        has been claimed (a draft PR exists) so a crash before claiming leaves
        the email re-processable.
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
            allowed = {a.lower() for a in cfg.imap_allowed_senders}
            for uid in uids:
                uid_s = uid.decode()
                typ, fetched = conn.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = email.message_from_bytes(fetched[0][1])
                sender_full = _decode(msg.get("From"))
                sender = parseaddr(sender_full)[1].lower()
                if allowed and sender not in allowed:
                    log.info("inbox: skipping non-allowlisted sender %s", sender)
                    continue
                triggers.append(
                    Trigger(
                        uid=uid_s,
                        sender=sender,
                        sender_full=sender_full,
                        subject=_decode(msg.get("Subject")).strip() or "(no subject)",
                        body=_plain_body(msg).strip(),
                        message_id=(msg.get("Message-ID") or "").strip(),
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
