"""Who gets told, and how: turns the rules' "notify" into an email that is sent, drafted or held.

The rules already decide WHETHER someone must be told (`Decision.notify`) and the writer drafts the
words. This module only works out the address and applies the sending rules:

- a vendor message answers the email the invoice came in, in the same thread;
- a PO-owner message goes to the owner of the matched PO (or the procurement inbox, if one is set);
- nothing is ever sent towards a vendor when the bank details changed, because if the invoice is
  fraudulent, a reply reaches the fraudster;
- nothing is sent to an automated sender or a placeholder address, and nothing is sent at all when
  email isn't set up or sending is manual: the message waits as a draft.
"""
from __future__ import annotations

import logging
from typing import Any

from .domain import DraftMessage, Finding, PurchaseOrder, Vendor
from .mail import Mailer, MailError, is_deliverable
from .store import Store, utc_now

logger = logging.getLogger(__name__)

BANK_CHANGED_HOLD = ("Not sent: the bank details changed, so nothing goes back towards the sender. "
                     "The reviewer phones the vendor on the number already on file.")


class Outbox:
    def __init__(self, store: Store, mailer: Mailer | None, *, send_mode: str = "auto",
                 procurement_email: str | None = None, company_name: str = "") -> None:
        self._store = store
        self._mailer = mailer
        self._send_mode = send_mode  # "auto": send as soon as a run decides; "manual": a person presses Send
        self._procurement_email = procurement_email
        self._company_name = company_name

    # ----------------------------------------------------------------- after a run

    def notify(self, *, run_id: int, draft: DraftMessage | None, findings: list[Finding],
               vendor: Vendor | None, po: PurchaseOrder | None, email_id: int | None) -> list[dict[str, Any]]:
        """Create (and in auto mode send) the message a decided run calls for. Returns the outbox rows."""
        if draft is None:
            return []
        email = self._store.get_email(email_id) if email_id else None
        to_address, to_name, in_reply_to, subject, body, hold = self._address(draft, vendor, po, email)
        if hold is None and draft.to == "vendor" and any(finding.code == "BANK_CHANGED" for finding in findings):
            hold = BANK_CHANGED_HOLD

        message_id = self._store.add_message(
            kind=draft.to, run_id=run_id, email_id=email_id, to_address=to_address, to_name=to_name,
            subject=subject, body=body, in_reply_to=in_reply_to,
            status="held" if hold else "draft", reason=hold or self._draft_reason(),
        )
        if hold is None and self._send_mode == "auto" and self._mailer:
            self.send(message_id)
        return [self._store.get_message(message_id)]

    def _address(self, draft: DraftMessage, vendor: Vendor | None, po: PurchaseOrder | None,
                 email: dict[str, Any] | None) -> tuple[str | None, str | None, str | None, str, str, str | None]:
        subject, body = draft.subject, draft.body
        if draft.to == "vendor":
            if email:
                if not email["can_reply"]:
                    return (email["from_address"], email["from_name"], None, subject, body,
                            "Not sent: the invoice came from an automated address that doesn't read replies.")
                original = email["subject"] or "your invoice"
                reply_subject = original if original.lower().startswith("re:") else f"Re: {original}"
                return email["from_address"], email["from_name"], email["message_id"], reply_subject, body, None
            if vendor:
                return vendor.email, vendor.name, None, subject, body, self._undeliverable(vendor.email)
            return None, None, None, subject, body, "Not sent: there is no known address for this vendor."

        owner = po.owner_name if po else ""
        address = self._procurement_email or (po.owner_email if po else None)
        if not address:
            return (None, None, None, subject, body,
                    "Not sent: there is no PO owner to send this to. Set FRIDAY_PROCUREMENT_EMAIL.")
        if self._procurement_email and po and owner:
            body = f"For {owner}, who raised {po.po_number}.\n\n{body}"
        return address, owner or "Procurement", None, subject, body, self._undeliverable(address)

    @staticmethod
    def _undeliverable(address: str | None) -> str | None:
        if is_deliverable(address):
            return None
        return (f"Not sent: {address} is a placeholder address from the sample data. "
                "Set FRIDAY_PROCUREMENT_EMAIL to receive PO-owner notes.")

    def _draft_reason(self) -> str | None:
        if self._mailer is None:
            return "Email isn't set up, so this is a draft to copy and send yourself."
        if self._send_mode != "auto":
            return "Waiting for a person to press Send."
        return None

    # ----------------------------------------------------------------- sending

    def send(self, message_id: int) -> dict[str, Any]:
        """Send a draft or retry a failed message. Held messages are never sent."""
        message = self._store.get_message(message_id)
        if message is None:
            raise LookupError(f"Message {message_id} does not exist.")
        if message["status"] not in ("draft", "failed"):
            raise ValueError("This message can't be sent: it was already sent, or it is held on purpose.")
        if self._mailer is None:
            raise ValueError("Email isn't set up. Add FRIDAY_MAIL_ADDRESS and FRIDAY_MAIL_APP_PASSWORD.")
        try:
            sent_id = self._mailer.send(to_address=message["to_address"], to_name=message["to_name"],
                                        subject=message["subject"], body=message["body"],
                                        in_reply_to=message["in_reply_to"])
        except MailError as error:
            logger.warning("Message %s not sent: %s", message_id, error)
            self._store.update_message(message_id, status="failed", reason=str(error))
        else:
            self._store.update_message(message_id, status="sent", reason=None, sent_at=utc_now(),
                                       sent_message_id=sent_id)
        return self._store.get_message(message_id)

    # ----------------------------------------------------------------- emails without an invoice

    def reply_without_invoice(self, email_id: int) -> dict[str, Any] | None:
        """Ask the sender for the invoice, when an email arrived with nothing we could read attached."""
        email = self._store.get_email(email_id)
        body = ("Hello,\n\nThanks for your email. We couldn't find an invoice attached to it, so nothing has been "
                "processed. Please reply with the invoice attached as a PDF, or as a clear photo (PNG or JPEG).\n\n"
                f"Accounts Payable, {self._company_name}")
        original = email["subject"] or "your email"
        message_id = self._store.add_message(
            kind="vendor", email_id=email_id, to_address=email["from_address"], to_name=email["from_name"],
            subject=original if original.lower().startswith("re:") else f"Re: {original}", body=body,
            in_reply_to=email["message_id"], status="draft", reason=self._draft_reason(),
        )
        if self._send_mode == "auto" and self._mailer:
            return self.send(message_id)
        return self._store.get_message(message_id)
