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
    # ------------------------------------------------------------- the second test set (samples/more)
    "more/invoice_01_clean_itemised_gst_separate.pdf": dict(
        vendor_name="Sharma Office Supplies Pvt. Ltd.", vendor_tax_id="05AABCS4412K1Z7",
        invoice_number="SOS/2026-27/0412", invoice_date="2026-10-03", currency="INR", po_reference="",
        line_items=[
            dict(description="A4 Copier Paper 75gsm (ream)", quantity=40, unit_price=285, amount=11400),
            dict(description="Whiteboard Markers (box of 10)", quantity=12, unit_price=190, amount=2280),
            dict(description="Stapler, Heavy Duty", quantity=5, unit_price=425, amount=2125),
            dict(description="Ring Binder 2D A4", quantity=30, unit_price=110, amount=3300),
        ],
        subtotal=19105, tax_amount=3438.90, tax_rate_percent=18, tax_treatment="separate", total=22543.90,
        bank_account_number="", bank_ifsc="HDFC0000412",
    ),
    "more/invoice_02_clean_bundled_vat_embedded.pdf": dict(
        vendor_name="Bluepeak Consulting Group Ltd", vendor_tax_id="GB 284 7712 06", invoice_number="BPC-1187",
        invoice_date="2026-10-09", currency="GBP", po_reference="PO 5521-HA",
        line_items=[dict(description="Consulting services, October 2026 - all-in fee covering discovery workshops, "
                                     "stakeholder interviews, final strategy deck and month-end support retainer.",
                         amount=4800)],
        tax_rate_percent=20, tax_treatment="embedded", total=4800,
        bank_account_number="40918826", bank_ifsc="20-45-17",
        notes="A 2% discount applies if paid within 7 days of issue.",
    ),
    "more/invoice_03_scanned_dotmatrix_itemised.pdf": dict(
        vendor_name="Rathore & Sons Hardware", vendor_tax_id="05AAGFR2290D1ZK", invoice_number="2291",
        invoice_date="2026-10-17", currency="INR", po_reference="",
        line_items=[
            dict(description="OPC 53 Grade Cement 50kg bag", quantity=60, unit_price=395, amount=23700),
            dict(description="TMT Bar 12mm (per kg)", quantity=450, unit_price=68, amount=30600),
            dict(description="Binding Wire (per kg)", quantity=25, unit_price=82, amount=2050),
        ],
        subtotal=56350, tax_amount=10143, tax_rate_percent=18, tax_treatment="separate", total=66493,
        document_quality="degraded",
    ),
    "more/invoice_04_missing_number_and_date.pdf": dict(
        vendor_name="Lotus Catering Co.", invoice_number="", invoice_date="", currency="INR",
        line_items=[
            dict(description="Veg lunch buffet (per plate)", quantity=120, unit_price=350, amount=42000),
            dict(description="Tea / coffee service (per head)", quantity=120, unit_price=40, amount=4800),
            dict(description="Setup, crockery & service staff (flat)", quantity=1, unit_price=3500, amount=3500),
        ],
        subtotal=50300, tax_amount=2515, tax_rate_percent=5, tax_treatment="separate", total=52815,
    ),
    "more/invoice_05_missing_total.pdf": dict(
        vendor_name="Northwind Freight Solutions", invoice_number="NF-88204", invoice_date="2026-10-21",
        currency="USD",
        line_items=[
            dict(description="Ocean freight, 1 x 40' HC container", amount=3250),
            dict(description="Bunker adjustment factor (BAF)", amount=412.5),
            dict(description="Documentation fee", amount=85),
            dict(description="Customs clearance (origin)", amount=190),
        ],
        subtotal=3937.5, tax_amount=0, tax_rate_percent=0, tax_treatment="none", total=None,
        notes="The amount due says 'see account statement'.",
    ),
    "more/invoice_06_scanned_lowres_bundled_handwritten.pdf": dict(
        vendor_name="Gupta Tailors & Uniforms", invoice_number="0873", invoice_date="", currency="INR",
        line_items=[dict(description="Staff uniform sets (shirt + trouser) as per order, 25 nos @ 1,450 "
                                     "(stitching + fabric, incl. GST)", quantity=25, unit_price=1450, amount=36250)],
        tax_treatment="embedded", total=36250, amount_paid=10000, document_quality="degraded",
        uncertain_fields=["invoice_date"], notes="Handwritten: adv. recd Rs 10,000, bal. 26,250 due on delivery.",
    ),
}

# Two more of the test invoices, kept out of the repository: one is a third-party sample, the other a real
# customer's bill. The tests use what a correct reading of each looks like, with a stand-in PDF.
SLICED_PAID = dict(
    vendor_name="DEMO - Sliced Invoices", invoice_number="INV-3337", invoice_date="2016-01-25", currency="USD",
    po_reference="12345", line_items=[dict(description="Web Design", quantity=1, unit_price=85, amount=85)],
    subtotal=85, tax_amount=8.5, tax_rate_percent=10, tax_treatment="separate", total=93.5, amount_paid=93.5,
    bank_account_number="1234 1234", bank_ifsc="4321 432", notes="A large PAID watermark is across the invoice.",
)
RENTOMOJO_RENT = dict(
    vendor_name="Rentomojo Limited (formerly known as Rentomojo Private Limited and Edunetwork Private Limited)",
    vendor_tax_id="09AADCE0327Q1ZE", invoice_number="R27/Jun18157627", invoice_date="2026-06-30", currency="INR",
    line_items=[dict(description="Water Purifier with Mineral Enhancer", quantity=1, unit_price=193.9, amount=228.8)],
    subtotal=193.9, tax_amount=34.9, tax_treatment="separate", total=228.8,
)


class StubReader:
    """Returns the known reading for a sample file; anything else is 'unreadable'."""

    def __init__(self, overrides: dict[str, dict] | None = None) -> None:
        self._readings = {**READINGS, **(overrides or {})}

    def read(self, document: Document) -> ExtractedInvoice:
        if document.filename not in self._readings:
            raise ReaderError("The AI reader is not available.")
        return ExtractedInvoice(**self._readings[document.filename])
