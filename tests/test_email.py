"""Email in (invoices arrive as attachments) and email out (the rules decide who is told)."""
from email.message import EmailMessage

import pytest

from app.domain import DraftMessage, Finding, Status
from app.files import FileStoreError, LocalFileStore
from app.intake import EmailIntake
from app.mail import is_deliverable, parse_email
from app.outbox import Outbox
from app.worker import RunQueue
from tests.conftest import FakeMailer

APP_ADDRESS = "ap@meridian-demo.com"
VENDOR = "accounts@kaveri-office.in"


def email(*, sender=f"Kaveri Accounts <{VENDOR}>", subject="Invoice attached", body="Please process.",
          attachments=(), headers=None) -> bytes:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, APP_ADDRESS, subject
    message["Message-ID"] = f"<{abs(hash((sender, subject, len(attachments))))}@mail.test>"
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    for filename, data, maintype, subtype in attachments:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def pdf(settings, name="05-kaveri-payment-reminder.pdf"):
    return (name, (settings.samples_dir / name).read_bytes(), "application", "pdf")


# --------------------------------------------------------------------------- reading an email


def test_an_email_is_read_into_sender_subject_preview_and_invoice_attachments(settings):
    logo = ("logo.png", b"\x89PNG" + b"0" * 2_000, "image", "png")  # a signature logo, not an invoice
    photo = ("IMG_2041.jpg", b"\xff\xd8\xff" + b"0" * 60_000, "image", "jpeg")

    parsed = parse_email(email(attachments=[pdf(settings), logo, photo], body="Hi,\n\n  please   process this."))

    assert parsed.from_address == VENDOR and parsed.from_name == "Kaveri Accounts"
    assert parsed.subject == "Invoice attached"
    assert parsed.body_preview == "Hi, please process this."
    assert [attachment.filename for attachment in parsed.attachments] == ["05-kaveri-payment-reminder.pdf", "IMG_2041.jpg"]
    assert not parsed.automated and not parsed.bounce and not parsed.in_thread


@pytest.mark.parametrize("sender, headers", [
    ("Billing <no-reply@rentco.in>", {}),
    (f"Kaveri <{VENDOR}>", {"Auto-Submitted": "auto-replied"}),
    (f"Kaveri <{VENDOR}>", {"Precedence": "bulk"}),
])
def test_automated_senders_are_recognised_so_they_are_never_answered(sender, headers):
    assert parse_email(email(sender=sender, headers=headers)).automated


def test_bounces_and_replies_are_recognised():
    assert parse_email(email(sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>")).bounce
    assert parse_email(email(headers={"In-Reply-To": "<earlier@mail.test>"})).in_thread


def test_placeholder_addresses_are_never_sent_to():
    assert not is_deliverable("meera.iyer@meridianretail.example")
    assert not is_deliverable("")
    assert is_deliverable("ap.team@gmail.com")


# --------------------------------------------------------------------------- taking an email in


@pytest.fixture
def intake(desk):
    pipeline_queue = RunQueue(lambda run_id: _run(desk, run_id), workers=0)
    return EmailIntake(desk.store, desk.files, pipeline_queue, desk.outbox, APP_ADDRESS, desk.settings.max_upload_bytes)


def _run(desk, run_id):
    from app.ai import TemplateExplainer
    from app.pipeline import InvoicePipeline

    InvoicePipeline(desk.store, desk.reader, TemplateExplainer("Meridian Retail"), desk.settings, desk.files,
                    desk.outbox).run(run_id)


def test_an_emailed_invoice_is_checked_and_the_vendor_gets_a_reply_in_the_same_thread(desk, intake, settings):
    raw = email(subject="Reminder: is this invoice still pending?", attachments=[pdf(settings)])
    parsed = parse_email(raw)

    [run_id] = intake.handle(parsed)

    run = desk.store.get_run(run_id)
    assert run["source"] == "email" and run["decision"]["primary_code"] == "DUPLICATE"
    [sent] = desk.mailer.sent
    assert sent["to_address"] == VENDOR
    assert sent["subject"] == "Re: Reminder: is this invoice still pending?"
    assert sent["in_reply_to"] == parsed.message_id
    assert desk.store.messages_for_run(run_id)[0]["status"] == "sent"
    notify = run["stages"][-1]
    assert notify["key"] == "notify" and notify["findings"][0]["code"] == "EMAIL_SENT"


def test_the_same_email_seen_twice_is_processed_once(desk, intake, settings):
    raw = email(attachments=[pdf(settings, "01-kaveri-chairs-and-desks.pdf")])

    assert len(intake.handle(parse_email(raw))) == 1
    assert intake.handle(parse_email(raw)) == []
    assert len(desk.store.list_runs()) == 1


def test_each_invoice_attached_to_one_email_becomes_its_own_run(desk, intake, settings):
    raw = email(attachments=[pdf(settings, "01-kaveri-chairs-and-desks.pdf"), pdf(settings, "05-kaveri-payment-reminder.pdf"),
                             ("terms.docx", b"PK\x03\x04 not an invoice", "application", "octet-stream")])

    run_ids = intake.handle(parse_email(raw))

    assert len(run_ids) == 2
    [received] = desk.store.recent_emails()
    assert received["status"] == "processed" and "terms.docx" in received["note"]
    assert [run["id"] for run in received["runs"]] == sorted(run_ids)


def test_an_email_without_an_invoice_gets_one_polite_request_for_it(desk, intake):
    assert intake.handle(parse_email(email(subject="Payment status?"))) == []

    [sent] = desk.mailer.sent
    assert sent["subject"] == "Re: Payment status?" and "couldn't find an invoice" in sent["body"]
    assert desk.store.recent_emails()[0]["status"] == "no_invoice"


@pytest.mark.parametrize("raw", [
    email(subject="Re: Payment status?", headers={"In-Reply-To": "<ours@meridian-demo.com>"}),  # "thanks!" in a thread
    email(sender="Billing <no-reply@rentco.in>"),  # a robot
])
def test_no_reply_is_sent_where_it_could_start_a_mail_loop(desk, intake, raw):
    intake.handle(parse_email(raw))

    assert desk.mailer.sent == []


def test_our_own_emails_and_bounces_are_ignored(desk, intake, settings):
    for sender in (f"Friday <{APP_ADDRESS}>", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"):
        assert intake.handle(parse_email(email(sender=sender, attachments=[pdf(settings)]))) == []

    assert desk.store.list_runs() == [] and desk.mailer.sent == []


def test_an_email_that_failed_to_go_in_is_retried_not_lost(desk, settings):
    class BrokenStorage(LocalFileStore):
        def save(self, key, data, media_type):
            raise FileStoreError("Storage is down.")

    raw = parse_email(email(attachments=[pdf(settings, "01-kaveri-chairs-and-desks.pdf")]))
    queue = RunQueue(lambda run_id: _run(desk, run_id), workers=0)
    broken = EmailIntake(desk.store, BrokenStorage(settings.upload_dir), queue, desk.outbox, APP_ADDRESS, 10**7)
    working = EmailIntake(desk.store, desk.files, queue, desk.outbox, APP_ADDRESS, 10**7)

    with pytest.raises(FileStoreError):
        broken.handle(raw)  # the watcher leaves the email unread when this raises
    assert desk.store.recent_emails()[0]["status"] == "failed"

    assert len(working.handle(raw)) == 1


# --------------------------------------------------------------------------- who is told


def _email_id(store, sender=VENDOR, can_reply=True):
    return store.claim_email("<inv@mail.test>", sender, "Vendor", "Invoice", "", 1, can_reply=can_reply)


def test_a_changed_bank_account_never_gets_a_reply_even_if_a_draft_exists(desk):
    outbox = desk.outbox
    draft = DraftMessage(to="vendor", subject="About your invoice", body="Hello")
    bank = Finding(code="BANK_CHANGED", status=Status.REVIEW, title="Bank details don't match", message="")

    [message] = outbox.notify(run_id=1, draft=draft, findings=[bank], vendor=None, po=None, email_id=_email_id(desk.store))

    assert message["status"] == "held" and "bank details changed" in message["reason"]
    assert desk.mailer.sent == []


def test_the_bank_change_case_emails_nobody_and_says_why(desk):
    run = desk.process("02-northwind-october-freight.pdf", email_id=_email_id(desk.store))

    assert desk.mailer.sent == []
    assert "phones the vendor" in run["stages"][-1]["summary"]


def test_po_owner_notes_go_to_the_procurement_inbox_addressed_to_the_owner(desk):
    desk.procurement_email = "procurement.demo@gmail.com"

    desk.process("03-brightpath-final-milestone.pdf")

    [sent] = desk.mailer.sent
    assert sent["to_address"] == "procurement.demo@gmail.com"
    assert sent["body"].startswith("For Arjun Mehta, IT, who raised PO-2026-0388.")


def test_a_note_to_a_placeholder_owner_address_is_held_with_the_reason(desk):
    run = desk.process("03-brightpath-final-milestone.pdf")

    [message] = desk.store.messages_for_run(run["id"])
    assert message["status"] == "held" and "placeholder" in message["reason"]
    assert desk.mailer.sent == []


def test_an_automated_sender_is_not_answered(desk):
    run = desk.process("05-kaveri-payment-reminder.pdf", email_id=_email_id(desk.store, can_reply=False))

    assert desk.store.messages_for_run(run["id"])[0]["status"] == "held"
    assert desk.mailer.sent == []


def test_manual_mode_waits_for_a_person_and_a_failed_send_can_be_retried(desk, store):
    outbox = Outbox(store, desk.mailer, send_mode="manual", company_name="Meridian Retail")
    draft = DraftMessage(to="vendor", subject="Duplicate", body="Already paid")
    [message] = outbox.notify(run_id=1, draft=draft, findings=[], vendor=None, po=None, email_id=_email_id(store))
    assert message["status"] == "draft" and desk.mailer.sent == []

    desk.mailer.fail_with = "Gmail is down."
    assert outbox.send(message["id"])["status"] == "failed"

    desk.mailer.fail_with = None
    assert outbox.send(message["id"])["status"] == "sent"
    with pytest.raises(ValueError):
        outbox.send(message["id"])  # never twice
