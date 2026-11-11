"""The happy path and the four edge cases, end to end through the real pipeline."""
import pytest

from app.pipeline import ResolutionError, resolve_review
from tests.readings import READINGS, StubReader

HAPPY = "01-kaveri-chairs-and-desks.pdf"
BANK_CHANGE = "02-northwind-october-freight.pdf"
SPLIT_PO = "03-brightpath-final-milestone.pdf"
SCAN_NO_PO = "04-sahyadri-brochures-scan.pdf"
RESENT = "05-kaveri-payment-reminder.pdf"


def codes(run) -> set[str]:
    return {finding["code"] for stage in run["stages"] for finding in stage["findings"]}


def test_clean_invoice_is_approved_and_recorded_against_its_po(desk):
    run = desk.process(HAPPY)

    assert run["status"] == "completed"
    assert run["outcome"] == "approved"
    assert run["po_number"] == "PO-2026-0412"
    assert all(stage["state"] == "done" for stage in run["stages"])
    balance = desk.store.find_purchase_order("PO-2026-0412")
    assert balance.billed == 240000 and balance.remaining == 0


def test_changed_bank_account_holds_payment_even_though_everything_else_matches(desk):
    run = desk.process(BANK_CHANGE)

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "BANK_CHANGED"
    assert {"PO_FOUND", "WITHIN_PO", "TAX_OK", "NOT_DUPLICATE"} <= codes(run)  # nothing else is wrong
    assert "+91 124 5550 0178" in run["decision"]["next_step"]  # the number on file, not the one on the invoice
    assert run["explanation"]["message"] is None  # never write back to the address the invoice came from
    assert desk.store.find_purchase_order("PO-2026-0431").billed == 0  # nothing recorded while on hold


def test_third_partial_invoice_is_caught_by_the_running_balance_not_the_po_total(desk):
    run = desk.process(SPLIT_PO)

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "OVER_PO"
    evidence = run["decision"]["issues"][0]["evidence"]
    assert evidence == {"overage": 60000, "remaining": 240000, "billed": 660000}
    assert run["explanation"]["message"]["to"] == "procurement"


def test_scan_with_no_po_number_gets_a_suggested_po_but_never_an_automatic_approval(desk):
    run = desk.process(SCAN_NO_PO)

    assert run["stages"][0]["details"]["kind"] == "scanned_pdf"
    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "PO_INFERRED"
    assert run["po_number"] == "PO-2026-0440"  # not PO-2026-0417, the vendor's other open PO
    assert run["pre_tax"] == 86000  # 1,01,480 including 18% GST, worked back
    assert "TAX_EMBEDDED" in codes(run) and "WITHIN_PO" in codes(run)


def test_resent_invoice_with_a_retyped_number_is_still_a_duplicate(desk):
    run = desk.process(RESENT)

    assert run["outcome"] == "rejected"
    assert run["decision"]["primary_code"] == "DUPLICATE"
    assert "KOS/26-27/0874" in run["decision"]["reason"]
    skipped = [stage["key"] for stage in run["stages"] if stage["state"] == "skipped"]
    assert skipped == ["match", "amounts"]  # no point matching a PO for something already paid


def test_the_same_invoice_submitted_twice_is_rejected_the_second_time(desk):
    assert desk.process(HAPPY)["outcome"] == "approved"

    second = desk.process(HAPPY)

    assert second["outcome"] == "rejected"
    assert second["decision"]["primary_code"] == "DUPLICATE"


def test_a_copy_of_an_invoice_already_waiting_for_review_is_not_queued_twice(desk):
    first = desk.process(SCAN_NO_PO)

    second = desk.process(SCAN_NO_PO)

    assert second["outcome"] == "rejected"
    assert second["decision"]["primary_code"] == "DUPLICATE_IN_QUEUE"
    assert f"#{first['id']}" in second["decision"]["reason"]


def test_reviewer_approval_records_the_invoice_against_the_suggested_po(desk):
    run = desk.process(SCAN_NO_PO)

    resolved = resolve_review(desk.store, run["id"], approve=True, note="Confirmed with marketing")

    assert resolved["resolution"] == "approved"
    balance = desk.store.find_purchase_order("PO-2026-0440")
    assert balance.remaining == 0 and balance.invoices[0].recorded_by == "reviewer"
    with pytest.raises(ResolutionError):
        resolve_review(desk.store, run["id"], approve=False)  # already decided


def test_small_overage_inside_tolerance_is_approved_with_a_note(desk):
    reading = {**READINGS[HAPPY], "subtotal": 243000, "tax_amount": 43740, "total": 286740,
               "line_items": [dict(description="Chairs and desks incl. freight", amount=243000)]}

    run = desk.process(HAPPY, reading)

    assert run["outcome"] == "approved"  # 3,000 over a 2,40,000 PO is 1.25%, under the 2% tolerance
    assert "WITHIN_TOLERANCE" in codes(run)


def test_invoice_missing_its_number_goes_back_to_the_vendor(desk):
    run = desk.process(HAPPY, {**READINGS[HAPPY], "invoice_number": ""})

    assert run["outcome"] == "rejected"
    assert run["decision"]["primary_code"] == "MISSING_FIELDS"
    assert run["explanation"]["message"]["to"] == "vendor"


def test_numbers_that_do_not_add_up_need_a_person(desk):
    run = desk.process(HAPPY, {**READINGS[HAPPY], "total": 288200})

    assert run["outcome"] == "needs_review"
    assert "MATH_MISMATCH" in codes(run)


def test_unknown_vendor_stops_before_any_po_is_touched(desk):
    reading = {**READINGS[HAPPY], "vendor_name": "Kavery Traders", "vendor_tax_id": "29ZZZZZ0000Z1Z0"}

    run = desk.process(HAPPY, reading)

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "VENDOR_UNKNOWN"


def test_when_the_ai_reader_is_unavailable_nothing_is_guessed(desk):
    desk.reader = StubReader()
    desk.reader._readings.clear()  # the stub now raises ReaderError, as the real reader does on an outage

    run = desk.process(HAPPY)

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "READ_FAILED"
    assert run["invoice"] is None


def test_a_crashing_step_sends_the_invoice_to_a_person_instead_of_losing_it(desk):
    class BrokenReader:
        def read(self, document):
            raise RuntimeError("boom")

    desk.reader = BrokenReader()

    run = desk.process(HAPPY)

    assert run["status"] == "completed"
    assert run["decision"]["primary_code"] == "STEP_FAILED"
