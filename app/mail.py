"""Email in and out over plain IMAP and SMTP, so any mailbox works. Gmail with an app password is the default.

Incoming: a background thread checks the inbox every few seconds, hands each new email to the intake
(which turns invoice attachments into runs) and marks it read. Outgoing: one SMTP send per message.

Three rules keep the app from talking to itself or to robots:
- emails from our own address and delivery failures (mailer-daemon) are ignored;
- emails from automated senders (no-reply addresses, auto-responders) are processed, but never answered;
- every email we send says `Auto-Submitted`, which tells well-behaved systems not to auto-reply to it.
"""
from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, make_msgid, parseaddr
from typing import Callable

from .store import utc_now

logger = logging.getLogger(__name__)


class MailError(Exception):
    """Sending or receiving failed. The message is shown to the user as-is."""


@dataclass(frozen=True)
class MailSettings:
    address: str
    app_password: str
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    poll_seconds: int = 20
    lookback_days: int = 3  # how far back the inbox is searched, read or unread


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class InboundEmail:
    message_id: str
    from_address: str
    from_name: str
    subject: str
    body_preview: str
    attachments: list[Attachment] = field(default_factory=list)
    automated: bool = False  # a no-reply sender or an auto-responder: process it, never answer it
    bounce: bool = False  # a delivery failure report: ignore it
    in_thread: bool = False  # a reply to an earlier email rather than a new conversation


# --------------------------------------------------------------------------- reading an email

_BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
_NO_REPLY_SENDERS = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|bounces?)[.+@-]", re.I)
_MIN_IMAGE_BYTES = 30_000  # smaller pictures are signature logos and icons, not photographed invoices


def parse_email(raw: bytes) -> InboundEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    from_name, from_address = parseaddr(str(message.get("From", "")))
    message_id = str(message.get("Message-ID", "")).strip() or f"<{hashlib.sha256(raw).hexdigest()[:32]}@no-id>"

    auto_submitted = str(message.get("Auto-Submitted", "no")).strip().lower()
    precedence = str(message.get("Precedence", "")).strip().lower()
    automated = (
        auto_submitted != "no"
        or precedence in ("bulk", "junk", "list", "auto_reply")
        or any(message.get(header) for header in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress"))
        or bool(_NO_REPLY_SENDERS.match(from_address))
    )

    return InboundEmail(
        message_id=message_id,
        from_address=from_address.strip().lower(),
        from_name=from_name.strip(),
        subject=" ".join(str(message.get("Subject", "")).split()),
        body_preview=_preview(message),
        attachments=_attachments(message),
        automated=automated,
        bounce=bool(_BOUNCE_SENDERS.match(from_address)),
        in_thread=bool(message.get("In-Reply-To")),
    )


def _preview(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = body.get_content()
    except (LookupError, ValueError):  # an unknown charset should cost us the preview, not the invoice
        return ""
    if body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:280]


def _attachments(message: EmailMessage) -> list[Attachment]:
    found = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        is_image = content_type.startswith("image/")
        # Mail programs label PDFs all sorts of ways (application/x-pdf, octet-stream, force-download), so
        # anything attached is kept and its first bytes decide later. Only the message text itself is skipped.
        if content_type.startswith(("text/", "multipart/", "message/")):
            continue
        if part.get_content_disposition() is None and not part.get_filename():
            continue
        data = part.get_payload(decode=True) or b""
        if is_image and len(data) < _MIN_IMAGE_BYTES:
            continue
        found.append(Attachment(filename=part.get_filename() or "attachment", content_type=content_type, data=data))
    return found


# --------------------------------------------------------------------------- sending


class Mailer:
    def __init__(self, settings: MailSettings, sender_name: str) -> None:
        self._settings = settings
        self._sender_name = sender_name

    @property
    def address(self) -> str:
        return self._settings.address

    def send(self, *, to_address: str, to_name: str | None, subject: str, body: str,
             in_reply_to: str | None = None) -> str:
        """Send one plain-text email and return its Message-ID."""
        message = EmailMessage()
        message["From"] = formataddr((self._sender_name, self._settings.address))
        message["To"] = formataddr((to_name or "", to_address))
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self._settings.address.rsplit("@", 1)[-1])
        message["Auto-Submitted"] = "auto-replied" if in_reply_to else "auto-generated"
        if in_reply_to:  # keeps the reply in the vendor's thread
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)
        try:
            with smtplib.SMTP_SSL(self._settings.smtp_host, self._settings.smtp_port, timeout=30,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(self._settings.address, self._settings.app_password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as error:
            raise MailError("The mail server rejected the login. Check MATCHBOOK_MAIL_APP_PASSWORD.") from error
        except smtplib.SMTPRecipientsRefused as error:
            raise MailError(f"The mail server refused the address {to_address}.") from error
        except (smtplib.SMTPException, OSError) as error:
            raise MailError(f"Couldn't send the email: {error}") from error
        return message["Message-ID"]


_PLACEHOLDER_DOMAINS = ("example", "example.com", "example.org", "example.net", "test", "invalid", "localhost")


def is_deliverable(address: str | None) -> bool:
    """False for empty addresses and reserved demo domains, which would only bounce."""
    if not address or "@" not in address:
        return False
    domain = address.rsplit("@", 1)[-1].lower()
    return not any(domain == reserved or domain.endswith("." + reserved) for reserved in _PLACEHOLDER_DOMAINS)


# --------------------------------------------------------------------------- watching the inbox


class InboxWatcher:
    """Checks the inbox on a timer, in its own thread.

    It looks at every email from the last few days, read or unread, so opening an email in Gmail first
    doesn't hide it from the app. Each email is recognised by its Message-ID, which is checked against
    the database before the full email is downloaded, so nothing is processed twice.
    """

    def __init__(self, settings: MailSettings, handle: Callable[[InboundEmail], None],
                 already_taken_in: Callable[[list[str]], set[str]]) -> None:
        self._settings = settings
        self._handle = handle
        self._already_taken_in = already_taken_in
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="inbox-watcher", daemon=True)
        self.last_checked: str | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def check_soon(self) -> None:
        """Check now instead of waiting for the timer (the "Check now" button)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stopped.is_set():
            self.check_once()
            self._wake.wait(self._settings.poll_seconds)
            self._wake.clear()

    def check_once(self) -> None:
        try:
            with imaplib.IMAP4_SSL(self._settings.imap_host, timeout=30) as imap:
                imap.login(self._settings.address, self._settings.app_password)
                imap.select("INBOX")
                _, found = imap.uid("SEARCH", None, "SINCE", _imap_date(self._settings.lookback_days))
                for uid in self._new_uids(imap, found[0].split())[:15]:  # a burst is worked through 15 at a time
                    self._take(imap, uid)
            self.last_checked, self.last_error = utc_now(), None
        except imaplib.IMAP4.error as error:
            self.last_error = f"The mailbox refused the login or the request ({error}). Check the address and app password."
            logger.warning("Inbox check failed: %s", error)
        except OSError as error:
            self.last_error = f"Couldn't reach the mailbox: {error}"
            logger.warning("Inbox check failed: %s", error)

    def _new_uids(self, imap: imaplib.IMAP4_SSL, uids: list[bytes]) -> list[bytes]:
        """The emails not taken in yet, judged from their headers alone (cheap to fetch every 20 seconds)."""
        if not uids:
            return []
        _, parts = imap.uid("FETCH", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        message_ids: dict[bytes, str] = {}
        for part in parts:
            if not isinstance(part, tuple):
                continue
            uid = re.search(rb"UID (\d+)", part[0])
            found = re.search(rb"(?im)^message-id:\s*(\S+)", part[1] or b"")
            if uid and found:
                message_ids[uid.group(1)] = found.group(1).decode("ascii", "replace")
        known = self._already_taken_in(sorted(set(message_ids.values())))
        # An email without a Message-ID is downloaded anyway; the intake recognises it by its content.
        return [uid for uid in uids if message_ids.get(uid) not in known]

    def _take(self, imap: imaplib.IMAP4_SSL, uid: bytes) -> None:
        _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
        if raw is None:
            return
        try:
            self._handle(parse_email(raw))
        except Exception:
            logger.exception("Couldn't take in email %s; it is tried again on the next check", uid)
            return
        imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")  # a courtesy for whoever opens the mailbox


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(days_back: int) -> str:
    """IMAP wants 12-Nov-2026, always in English month names, whatever the server's locale."""
    day = date.today() - timedelta(days=days_back)
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"
