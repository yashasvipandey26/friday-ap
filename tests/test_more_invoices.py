"""The second test set: eight invoices of different shapes, each with the outcome the rules should reach."""
from tests.readings import READINGS, RENTOMOJO_RENT, SLICED_PAID

MORE = "more/invoice_0{}"
STAND_IN = "01-kaveri-chairs-and-desks.pdf"  # any PDF; the reading is what matters for these two


def codes(run) -> set[str]:
    return {finding["code"] for stage in run["stages"] for finding in stage["findings"]}


def file(number: int) -> str:
    return next(name for name in READINGS if name.startswith(MORE.format(number)))


def test_clean_itemised_invoice_without_a_po_number_gets_its_po_suggested_not_assumed(desk):
    run = desk.process(file(1))

    assert run["decision"]["primary_code"] == "PO_INFERRED" and run["po_number"] == "PO-2026-0450"
    assert {"MATH_OK", "VENDOR_FOUND", "BANK_NOT_SHOWN", "WITHIN_PO", "TAX_OK"} <= codes(run)
    assert run["outcome"] == "needs_review"  # a guessed PO always gets a person's confirmation


def test_uk_invoice_with_vat_inside_the_price_is_approved_against_its_gbp_po(desk):
    run = desk.process(file(2))

    assert run["outcome"] == "approved"
    assert run["po_number"] == "PO-5521-HA" and run["currency"] == "GBP"
    assert run["pre_tax"] == 4000  # £4,800 including 20% VAT, worked back
    assert {"TAX_EMBEDDED", "BANK_OK", "WITHIN_PO"} <= codes(run)


def test_dot_matrix_scan_is_checked_like_any_other_invoice(desk):
    run = desk.process(file(3))

    assert run["decision"]["primary_code"] == "PO_INFERRED" and run["po_number"] == "PO-2026-0455"
    assert {"MATH_OK", "TAX_OK", "WITHIN_PO"} <= codes(run)


def test_invoice_without_number_or_date_goes_back_to_the_vendor(desk):
    run = desk.process(file(4))

    assert run["outcome"] == "rejected" and run["decision"]["primary_code"] == "MISSING_FIELDS"
    assert run["decision"]["issues"][0]["evidence"]["missing"] == ["invoice number", "invoice date"]
    assert run["explanation"]["message"]["to"] == "vendor"


def test_invoice_that_points_to_a_statement_instead_of_a_total_goes_back_to_the_vendor(desk):
    run = desk.process(file(5))

    assert run["outcome"] == "rejected"
    assert run["decision"]["issues"][0]["evidence"]["missing"] == ["total"]


def test_an_illegible_handwritten_date_is_for_a_person_to_read_not_a_rejection(desk):
    run = desk.process(file(6))

    assert run["outcome"] == "needs_review"
    assert run["decision"]["primary_code"] == "UNREADABLE"
    assert "invoice date couldn't be read" in run["decision"]["reason"]


def test_a_handwritten_advance_means_only_the_balance_is_owed(desk):
    reading = {**READINGS[file(6)], "invoice_date": "2026-10-28", "uncertain_fields": []}

    run = desk.process(file(6), reading)

    assert run["decision"]["primary_code"] == "PART_PAID"
    assert run["decision"]["issues"][0]["evidence"] == {"paid": 10000, "balance": 26250}
    assert run["po_number"] == "PO-2026-0466" and "PO_INFERRED" in codes(run)


def test_an_invoice_stamped_paid_is_not_paid_again(desk):
    run = desk.process(STAND_IN, SLICED_PAID)

    assert run["outcome"] == "needs_review" and run["decision"]["primary_code"] == "ALREADY_PAID"
    assert {"PO_FOUND", "WITHIN_PO", "TAX_OK", "BANK_OK", "STALE"} <= codes(run)


def test_rental_invoice_with_tax_on_each_line_is_matched_to_its_po(desk):
    run = desk.process(STAND_IN, RENTOMOJO_RENT)

    assert "MATH_OK" in codes(run)  # the line total includes its CGST and SGST
    assert run["vendor_name"] == "Rentomojo Limited"  # found by GSTIN despite the long printed name
    assert run["decision"]["primary_code"] == "PO_INFERRED" and run["po_number"] == "PO-2026-0470"
