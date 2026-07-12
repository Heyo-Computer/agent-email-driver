"""SMTP notification tool — the IO mechanism for sending status emails.

`send_notification` builds an RFC 5322 message and sends it via the configured
SMTP relay (STARTTLS by default, or implicit SSL on port 465). Used at each
pipeline milestone and to reply to email-triggered work.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from util import log


def reply_subject(subject: str) -> str:
    """Subject line for an in-thread reply: prefix `Re: ` exactly once."""
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    def send(
        self,
        subject: str,
        body: str,
        *,
        to: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> bool:
        """Send a plain-text email. Returns True on success.

        In dry-run mode (or when SMTP is unconfigured) it logs and returns
        without sending. `in_reply_to` threads a reply to an inbound message;
        `references` carries the full thread chain (falls back to
        `in_reply_to`) so replies deep in a conversation still thread.
        """
        cfg = self.cfg
        recipient = to or cfg.notify_to
        if cfg.dry_run:
            log.info("[dry-run] would email %s: %s", recipient, subject)
            return True
        if not cfg.smtp_enabled:
            log.warning("SMTP not configured; skipping notification: %s", subject)
            return False

        msg = EmailMessage()
        msg["From"] = cfg.smtp_from
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="factory")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body)

        try:
            if cfg.smtp_port == 465:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx) as s:
                    self._auth_send(s, msg)
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
                    if cfg.smtp_starttls:
                        s.starttls(context=ssl.create_default_context())
                    self._auth_send(s, msg)
        except Exception as e:  # noqa: BLE001 - notifications must never crash the loop
            log.error("failed to send notification to %s: %s", recipient, e)
            return False
        log.info("notified %s: %s", recipient, subject)
        return True

    def _auth_send(self, s: smtplib.SMTP, msg: EmailMessage) -> None:
        cfg = self.cfg
        if cfg.smtp_user and cfg.smtp_pass:
            s.login(cfg.smtp_user, cfg.smtp_pass)
        s.send_message(msg)
