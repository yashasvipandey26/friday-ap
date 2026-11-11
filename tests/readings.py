"""What a correct reading of each sample invoice looks like.

The tests feed these to the pipeline in place of the AI reader, so the rules are tested
against known inputs and no test ever calls a model.
"""
from app.ai import Document, ReaderError
from app.domain import ExtractedInvoice

READINGS = {
    "01-kaveri-chairs-and-desks.pdf": dict(
        vendor_name="Kaveri Office Supplies Pvt Ltd", vendor_tax_id="29AAKCK4821M1Z7",
        invoice_number="KOS/26-27/0931", invoice_date="2026-11-04", currency="INR", po_reference="PO-2026-0412",
        line_items=[
            dict(description="Ergonomic Office Chair, Mesh Back (Model EC-210)", quantity=40, unit_price=4500, amount=180000),
            dict(description="Height-Adjustable Desk 1500 mm (Model HD-15)", quantity=6, unit_price=10000, amount=60000),
        ],
        subtotal=240000, tax_amount=43200, tax_rate_percent=18, tax_treatment="separate", total=283200,
        bank_account_number="50200048214821", bank_ifsc="HDFC0001234",
    ),
    "02-northwind-october-freight.pdf": dict(
        vendor_name="Northwind Logistics LLP", vendor_tax_id="06AAHFN7302P1Z3",
        invoice_number="NWL/BLR/1182", invoice_date="2026-11-02", currency="INR", po_reference="PO-2026-0431",
        line_items=[
            dict(description="Full truck load, 32 ft container, Gurugram to Bengaluru (October lanes)",
                 quantity=5, unit_price=28000, amount=140000),
            dict(description="Loading and unloading charges", quantity=1, unit_price=10000, amount=10000),
        ],
        subtotal=150000, tax_amount=27000, tax_rate_percent=18, tax_treatment="separate", total=177000,
        bank_account_number="10094427730", bank_ifsc="IDFB0040101",
        notes="The invoice announces a change of bank account.",
    ),
    "03-brightpath-final-milestone.pdf": dict(
        vendor_name="Brightpath IT Services Pvt Ltd", vendor_tax_id="27AAGCB5519R1Z9",
        invoice_number="BIT/2026/163", invoice_date="2026-11-05", currency="INR", po_reference="PO-2026-0388",
        line_items=[
            dict(description="Milestone 3: UAT and go-live support, as per SOW section 4.3", amount=240000),
            dict(description="Additional change requests CR-07 and CR-09 (custom GST report, vendor portal sync)",
                 amount=60000),
        ],
        subtotal=300000, tax_amount=54000, tax_rate_percent=18, tax_treatment="separate", total=354000,
        bank_account_number="000405118842", bank_ifsc="ICIC0000004",
    ),
    "04-sahyadri-brochures-scan.pdf": dict(
        vendor_name="Sahyadri Print Works", vendor_tax_id="27ABXFS6640K1Z2",
        invoice_number="SPW/1047", invoice_date="2026-11-03", currency="INR",
        po_reference="", po_hint="As per your order dt. 21-10-2026",
        line_items=[dict(
            description="Printing & supply of A4 tri-fold brochures, 130 gsm art paper, 4 colour both sides, "
                        "incl. design corrections", quantity=5000, amount=101480)],
        tax_rate_percent=18, tax_treatment="embedded", total=101480, document_quality="degraded",
    ),
    "05-kaveri-payment-reminder.pdf": dict(
        vendor_name="Kaveri Office Supplies Pvt Ltd", vendor_tax_id="29AAKCK4821M1Z7",
        invoice_number="KOS-26-27-874", invoice_date="2026-10-03", currency="INR", po_reference="PO-2026-0379",
        line_items=[dict(description="Stationery and Pantry Consumables for October (as per attached list)",
                         quantity=1, unit_price=55000, amount=55000)],
        subtotal=55000, tax_amount=9900, tax_rate_percent=18, tax_treatment="separate", total=64900,
        bank_account_number="50200048214821", bank_ifsc="HDFC0001234",
    ),
}


class StubReader:
    """Returns the known reading for a sample file; anything else is 'unreadable'."""

    def __init__(self, overrides: dict[str, dict] | None = None) -> None:
        self._readings = {**READINGS, **(overrides or {})}

    def read(self, document: Document) -> ExtractedInvoice:
        if document.filename not in self._readings:
            raise ReaderError("The AI reader is not available.")
        return ExtractedInvoice(**self._readings[document.filename])
