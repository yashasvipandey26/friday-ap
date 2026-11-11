"""The small pieces the decisions rest on."""
from app.domain import ExtractedInvoice, reference_key
from app.rules import format_money, mask_account, pre_tax_amount


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
