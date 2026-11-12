"""The small pieces the decisions rest on."""
from datetime import date

from app.domain import ExtractedInvoice, reference_key
from app.rules import Policy, format_money, mask_account, pre_tax_amount, validate_invoice


def test_reference_key_ignores_punctuation_case_and_leading_zeros():
    assert reference_key("KOS/26-27/0874") == reference_key("kos-26-27-874") == reference_key("KOS 26 27 0874")
    assert reference_key("PO 2026/0412") == reference_key("PO-2026-0412")
    assert reference_key("KOS/26-27/0874") != reference_key("KOS/26-27/0875")


def test_money_uses_indian_grouping_for_rupees():
    assert format_money(283200) == "₹2,83,200"
    assert format_money(12345678.5) == "₹1,23,45,678.50"
    assert format_money(1500, "USD") == "$1,500"


def test_account_numbers_are_masked():
    assert mask_account("50200048214821") == "••••4821"


def test_pre_tax_is_stated_when_the_invoice_shows_a_subtotal():
    invoice = ExtractedInvoice(subtotal=1000, tax_amount=180, total=1180, tax_treatment="separate")
    assert pre_tax_amount(invoice, 18) == (1000, "stated")


def test_pre_tax_is_worked_back_when_tax_is_inside_the_price():
    invoice = ExtractedInvoice(total=101480, tax_treatment="embedded")
    assert pre_tax_amount(invoice, 18) == (86000, "worked_back")


def test_reader_output_is_cleaned_up_on_the_way_in():
    invoice = ExtractedInvoice(vendor_name="  ", total="2,83,200.00", tax_treatment="Separate",
                               line_items=[{"description": "Chairs", "amount": "Rs. 1,180"}])
    assert invoice.vendor_name is None
    assert invoice.total == 283200
    assert invoice.tax_treatment == "separate"
    assert invoice.line_items[0].amount == 1180


def _arithmetic(**fields) -> str:
    invoice = ExtractedInvoice(vendor_name="Rentomojo Limited", invoice_number="R27/Jun18157627",
                               invoice_date="2026-06-30", tax_treatment="separate", **fields)
    findings = validate_invoice(invoice, date(2026, 7, 5), Policy())
    return next(finding.code for finding in findings if finding.code.startswith("MATH"))


def test_line_items_printed_with_their_tax_added_still_add_up():
    # The line's last column is "Net Payable" (taxable value plus CGST and SGST), as on many GST invoices.
    assert _arithmetic(line_items=[{"description": "Water purifier rent", "amount": 228.80}],
                       subtotal=193.90, tax_amount=34.90, total=228.80) == "MATH_OK"


def test_lines_matching_the_total_do_not_hide_a_wrong_subtotal():
    assert _arithmetic(line_items=[{"description": "Water purifier rent", "amount": 228.80}],
                       subtotal=150.00, tax_amount=34.90, total=228.80) == "MATH_MISMATCH"


def test_lines_that_match_neither_subtotal_nor_total_are_flagged():
    assert _arithmetic(line_items=[{"description": "Water purifier rent", "amount": 210.00}],
                       subtotal=193.90, tax_amount=34.90, total=228.80) == "MATH_MISMATCH"
