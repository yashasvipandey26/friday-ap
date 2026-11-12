"""Turns an incoming email into runs: one per invoice attached, each checked by the same pipeline as an upload."""
from __future__ import annotations

import logging
from uuid import uuid4

from .files import FileStore, identify_file
from .mail import InboundEmail
from .outbox import Outbox
from .store import Store
from .worker import RunQueue

logger = logging.getLogger(__name__)


class EmailIntake:
    def __init__(self, store: Store, files: FileStore, queue: RunQueue, outbox: Outbox,
                 own_address: str, max_bytes: int) -> None:
        self._store = store
        self._files = files
        self._queue = queue
        self._outbox = outbox
        self._own_address = own_address.lower()
        self._max_bytes = max_bytes

    def handle(self, email: InboundEmail) -> list[int]:
        """Take one email in. Returns the ids of the runs it started (possibly none)."""
        email_id = self._store.claim_email(
            email.message_id, email.from_address, email.from_name, email.subject, email.body_preview,
            len(email.attachments), can_reply=not email.automated,
        )
        if email_id is None:
            return []  # already taken in: Gmail can show the same message again, and it must not be paid twice

        if email.bounce or email.from_address == self._own_address:
            self._store.update_email(email_id, status="ignored",
                                     note="A delivery report or our own email, so there is nothing to process.")
            return []

        try:
            return self._start_runs(email_id, email)
        except Exception as error:
            self._store.update_email(email_id, status="failed", note=f"Couldn't take this email in: {error}")
            raise  # the email stays unread and is tried again on the next check

    def _start_runs(self, email_id: int, email: InboundEmail) -> list[int]:
        run_ids, skipped = [], []
        for attachment in email.attachments:
            file_type = identify_file(attachment.data)
            if file_type is None:
                skipped.append(f"{attachment.filename} (not a PDF or image)")
                continue
            if len(attachment.data) > self._max_bytes:
                skipped.append(f"{attachment.filename} (over {self._max_bytes // (1024 * 1024)} MB)")
                continue
            media_type, extension = file_type
            key = f"runs/{uuid4().hex}{extension}"
            self._files.save(key, attachment.data, media_type)
            run_ids.append(self._store.create_run(attachment.filename, key, media_type, "email", email_id))

        skipped_note = f" Skipped: {', '.join(skipped)}." if skipped else ""
        if not run_ids:
            self._store.update_email(email_id, status="no_invoice", note="No invoice was attached." + skipped_note)
            # Ask for the invoice only on a new conversation from a person. A reply in an existing thread
            # ("thanks!") or an automated sender gets no answer, so two systems can't email each other forever.
            if not email.automated and not email.in_thread:
                self._outbox.reply_without_invoice(email_id)
            return []

        count = len(run_ids)
        self._store.update_email(email_id, status="processed",
                                 note=f"{count} attachment{'s' if count != 1 else ''} sent for checking." + skipped_note)
        for run_id in run_ids:
            self._queue.submit(run_id)
        logger.info("Email from %s started runs %s", email.from_address, run_ids)
        return run_ids
