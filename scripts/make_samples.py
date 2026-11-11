"""Generate the sample invoices in samples/.

Four vendors, four different layouts, one of them turned into a slightly crooked, noisy scan with
no text layer. Run it again whenever you change the test data:

    pip install -r requirements-dev.txt
    python scripts/make_samples.py
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pymupdf  # used only to turn a page into an image for the fake scan
import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, Table, TableStyle

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
PAGE_WIDTH, PAGE_HEIGHT = A4

BUYER_NAME = "Meridian Retail Pvt Ltd"
BUYER_ADDRESS = ["4th Floor, Prestige Tech Park", "Outer Ring Road, Bengaluru 560103", "Karnataka, India"]
BUYER_GSTIN = "29AAECM1187Q1ZV"


# --------------------------------------------------------------------------- small helpers


def rupees(amount: float) -> str:
    """283200 -> '2,83,200.00' (Indian digit grouping)."""
    whole, paise = divmod(round(amount * 100), 100)
    digits = str(whole)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) + f".{paise:02d}"


_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _below_thousand(number: int) -> str:
    hundreds, rest = divmod(number, 100)
    words = [f"{_ONES[hundreds]} Hundred"] if hundreds else []
    if rest:
        words.append(_ONES[rest] if rest < 20 else f"{_TENS[rest // 10]} {_ONES[rest % 10]}".strip())
    return " ".join(words)


def in_words(amount: float) -> str:
    number = round(amount)
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        count, number = divmod(number, size)
        if count:
            parts.append(f"{_below_thousand(count)} {name}")
    if number:
        parts.append(_below_thousand(number))
    return "Rupees " + " ".join(parts) + " Only"


def write_lines(canvas: Canvas, x: float, y: float, lines: list[str], font: str = "Helvetica",
                size: float = 9, leading: float | None = None, align: str = "left") -> float:
    """Draw lines of text downwards from y and return the y below the last one."""
    leading = leading or size * 1.35
    canvas.setFont(font, size)
    for line in lines:
        {"left": canvas.drawString, "right": canvas.drawRightString, "center": canvas.drawCentredString}[align](x, y, line)
        y -= leading
    return y


def draw_table(canvas: Canvas, rows: list[list], widths: list[float], x: float, top: float, style: list) -> float:
    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle(style))
    _, height = table.wrapOn(canvas, PAGE_WIDTH, PAGE_HEIGHT)
    table.drawOn(canvas, x, top - height)
    return top - height


def cell(text: str, size: float = 9, font: str = "Helvetica") -> Paragraph:
    return Paragraph(text, ParagraphStyle("cell", fontName=font, fontSize=size, leading=size * 1.3))


# --------------------------------------------------------------------------- layout 1: boxed GST tax invoice


def kaveri_tax_invoice(path: Path, *, number: str, date: str, po_number: str, po_date: str,
                       items: list[tuple[str, str, float, str, float]], stamp: str | None = None) -> None:
    """The dense, ruled 'Tax Invoice' that accounting packages print. CGST + SGST (same state)."""
    canvas = Canvas(str(path), pagesize=A4)
    left, right, top = 15 * mm, PAGE_WIDTH - 15 * mm, PAGE_HEIGHT - 15 * mm
    width = right - left
    middle = left + width * 0.52

    write_lines(canvas, PAGE_WIDTH / 2, top - 4, ["TAX INVOICE"], "Helvetica-Bold", 13, align="center")
    write_lines(canvas, right, top - 4, ["(Original for Recipient)"], "Helvetica-Oblique", 8, align="right")

    box_top = top - 12
    canvas.setLineWidth(0.7)
    canvas.rect(left, box_top - 40 * mm, width, 40 * mm)
    canvas.line(middle, box_top, middle, box_top - 40 * mm)
    y = write_lines(canvas, left + 3 * mm, box_top - 5 * mm, ["Kaveri Office Supplies Pvt Ltd"], "Helvetica-Bold", 11)
    write_lines(canvas, left + 3 * mm, y - 1, [
        "No. 27, 2nd Main Road, Peenya Industrial Area",
        "Bengaluru 560058, Karnataka",
        "GSTIN/UIN: 29AAKCK4821M1Z7",
        "State Name: Karnataka, Code: 29",
        "Phone: +91 80 5550 0142",
        "E-mail: accounts@kaverioffice.example",
    ], size=8.5)

    row_height = 10 * mm
    half = middle + (right - middle) / 2
    labels = [("Invoice No.", number, "Dated", date),
              ("Buyer's Order No.", po_number, "Order Dated", po_date),
              ("Mode/Terms of Payment", "30 Days", "Delivery Note", "DN/1846"),
              ("Dispatched through", "Own vehicle", "Destination", "Bengaluru")]
    for index, (label_a, value_a, label_b, value_b) in enumerate(labels):
        row_top = box_top - index * row_height
        if index:
            canvas.line(middle, row_top, right, row_top)
        for x, label, value in ((middle, label_a, value_a), (half, label_b, value_b)):
            write_lines(canvas, x + 2 * mm, row_top - 3.6 * mm, [label], size=7)
            write_lines(canvas, x + 2 * mm, row_top - 8 * mm, [value], "Helvetica-Bold", 9)
    canvas.line(half, box_top, half, box_top - 40 * mm)

    buyer_top = box_top - 40 * mm
    canvas.rect(left, buyer_top - 24 * mm, width, 24 * mm)
    write_lines(canvas, left + 3 * mm, buyer_top - 4.5 * mm, ["Buyer (Bill to)"], size=7)
    y = write_lines(canvas, left + 3 * mm, buyer_top - 9 * mm, [BUYER_NAME], "Helvetica-Bold", 10)
    write_lines(canvas, left + 3 * mm, y - 1, [", ".join(BUYER_ADDRESS[:2]), f"GSTIN/UIN: {BUYER_GSTIN}",
                                               "State Name: Karnataka, Code: 29"], size=8.5)

    subtotal = sum(amount for *_, amount in items)
    cgst = sgst = round(subtotal * 0.09, 2)
    total = subtotal + cgst + sgst
    rows = [["Sl", "Description of Goods", "HSN/SAC", "Quantity", "Rate", "Amount"]]
    rows += [[str(index), cell(description), hsn, quantity_text, rupees(rate), rupees(amount)]
             for index, (description, hsn, rate, quantity_text, amount) in enumerate(items, start=1)]
    rows += [["", "", "", "", "", ""] for _ in range(4 - len(items))]
    rows += [["", cell("Taxable value", font="Helvetica-Bold"), "", "", "", rupees(subtotal)],
             ["", cell("<i>Output CGST @ 9%</i>"), "", "", "9 %", rupees(cgst)],
             ["", cell("<i>Output SGST @ 9%</i>"), "", "", "9 %", rupees(sgst)],
             ["", cell("Total", font="Helvetica-Bold"), "", "", "", f"Rs. {rupees(total)}"]]
    table_bottom = draw_table(
        canvas, rows, [10 * mm, width - 112 * mm, 22 * mm, 26 * mm, 24 * mm, 30 * mm], left, buyer_top - 24 * mm,
        [("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
         ("BOX", (0, 0), (-1, -1), 0.7, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.black),
         ("LINEBEFORE", (1, 0), (-1, -1), 0.5, colors.black), ("LINEABOVE", (0, -4), (-1, -4), 0.5, colors.black),
         ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black), ("FONT", (-1, -1), (-1, -1), "Helvetica-Bold", 10),
         ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("ALIGN", (0, 0), (0, -1), "CENTER"),
         ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)],
    )

    words_top = table_bottom
    canvas.rect(left, words_top - 14 * mm, width, 14 * mm)
    write_lines(canvas, left + 3 * mm, words_top - 4.5 * mm, ["Amount Chargeable (in words)"], size=7)
    write_lines(canvas, left + 3 * mm, words_top - 10 * mm, [f"INR {in_words(total)[7:]}"], "Helvetica-Bold", 9.5)

    footer_top = words_top - 14 * mm
    canvas.rect(left, footer_top - 38 * mm, width, 38 * mm)
    canvas.line(middle, footer_top, middle, footer_top - 38 * mm)
    write_lines(canvas, left + 3 * mm, footer_top - 5 * mm, ["Company's Bank Details"], "Helvetica-Bold", 8.5)
    write_lines(canvas, left + 3 * mm, footer_top - 10 * mm, [
        "Bank Name      : HDFC Bank",
        "A/c No.            : 50200048214821",
        "Branch & IFSC : Peenya, HDFC0001234",
    ], size=8.5)
    write_lines(canvas, left + 3 * mm, footer_top - 27 * mm, [
        "Declaration: We declare that this invoice shows the actual price of the",
        "goods described and that all particulars are true and correct.",
    ], size=7)
    write_lines(canvas, right - 3 * mm, footer_top - 5 * mm, ["for Kaveri Office Supplies Pvt Ltd"], "Helvetica-Bold", 8.5,
                align="right")
    write_lines(canvas, right - 3 * mm, footer_top - 34 * mm, ["Authorised Signatory"], size=8, align="right")
    write_lines(canvas, PAGE_WIDTH / 2, footer_top - 43 * mm, ["This is a Computer Generated Invoice"], size=7.5, align="center")

    if stamp:
        canvas.saveState()
        canvas.translate(left + width * 0.76, buyer_top - 12 * mm)  # the empty half of the buyer box
        canvas.rotate(6)
        canvas.setStrokeColor(colors.Color(0.78, 0.1, 0.1, alpha=0.75))
        canvas.setFillColor(colors.Color(0.78, 0.1, 0.1, alpha=0.75))
        canvas.setLineWidth(1.8)
        canvas.roundRect(-41 * mm, -6.5 * mm, 82 * mm, 13 * mm, 2.5 * mm)
        canvas.setFont("Helvetica-Bold", 11.5)
        canvas.drawCentredString(0, -1.5 * mm, stamp)
        canvas.restoreState()
    canvas.save()


# --------------------------------------------------------------------------- layout 2: modern invoice with a notice


def northwind_invoice(path: Path) -> None:
    """A clean modern layout. Everything matches the PO; only the bank details have changed."""
    canvas = Canvas(str(path), pagesize=A4)
    left, right = 18 * mm, PAGE_WIDTH - 18 * mm
    teal = colors.HexColor("#0F4C5C")

    canvas.setFillColor(teal)
    canvas.rect(0, PAGE_HEIGHT - 36 * mm, PAGE_WIDTH, 36 * mm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    write_lines(canvas, left, PAGE_HEIGHT - 17 * mm, ["Northwind Logistics LLP"], "Helvetica-Bold", 19)
    write_lines(canvas, left, PAGE_HEIGHT - 24 * mm, ["Road freight across North and South India"], size=9)
    write_lines(canvas, right, PAGE_HEIGHT - 19 * mm, ["INVOICE"], "Helvetica-Bold", 22, align="right")
    canvas.setFillColor(colors.black)

    y = PAGE_HEIGHT - 48 * mm
    write_lines(canvas, left, y, ["Billed to"], "Helvetica-Bold", 8)
    write_lines(canvas, left, y - 5 * mm, [BUYER_NAME, *BUYER_ADDRESS, f"GSTIN {BUYER_GSTIN}"], size=9.5, leading=13)
    details = [("Invoice no.", "NWL/BLR/1182"), ("Invoice date", "02 Nov 2026"),
               ("PO reference", "PO-2026-0431"), ("Payment due", "02 Dec 2026")]
    for index, (label, value) in enumerate(details):
        write_lines(canvas, right - 62 * mm, y - index * 6.5 * mm, [label], size=9)
        write_lines(canvas, right, y - index * 6.5 * mm, [value], "Helvetica-Bold", 9.5, align="right")

    rows = [["Description", "Qty", "Rate (INR)", "Amount (INR)"],
            [cell("Full truck load, 32 ft container, Gurugram to Bengaluru (October lanes)", 9.5), "5 trips",
             rupees(28000), rupees(140000)],
            [cell("Loading and unloading charges", 9.5), "1", rupees(10000), rupees(10000)]]
    table_bottom = draw_table(
        canvas, rows, [right - left - 86 * mm, 22 * mm, 30 * mm, 34 * mm], left, PAGE_HEIGHT - 88 * mm,
        [("FONT", (0, 0), (-1, -1), "Helvetica", 9.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
         ("TEXTCOLOR", (0, 0), (-1, 0), teal), ("LINEBELOW", (0, 0), (-1, 0), 1.2, teal),
         ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#C9D3D6")), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
         ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)],
    )

    y = table_bottom - 9 * mm
    for label, value, bold in (("Subtotal", rupees(150000), False), ("IGST @ 18%", rupees(27000), False),
                               ("Total due (INR)", rupees(177000), True)):
        font, size = ("Helvetica-Bold", 12) if bold else ("Helvetica", 10)
        if bold:
            canvas.setStrokeColor(teal)
            canvas.line(right - 80 * mm, y + 6 * mm, right, y + 6 * mm)
        write_lines(canvas, right - 80 * mm, y, [label], font, size)
        write_lines(canvas, right, y, [value], font, size, align="right")
        y -= 8.5 * mm if not bold else 0

    notice_top = y - 14 * mm
    canvas.setFillColor(colors.HexColor("#FFF4D6"))
    canvas.setStrokeColor(colors.HexColor("#D9A21B"))
    canvas.roundRect(left, notice_top - 50 * mm, right - left, 50 * mm, 2 * mm, stroke=1, fill=1)
    canvas.setFillColor(colors.black)
    write_lines(canvas, left + 5 * mm, notice_top - 8 * mm, ["Important: our bank account has changed"], "Helvetica-Bold", 11)
    write_lines(canvas, left + 5 * mm, notice_top - 14.5 * mm, [
        "With effect from 1 November 2026 our collections have moved to a new bank. Please update your",
        "records and remit this invoice, and all future payments, to the account below.",
    ], size=9, leading=12.5)
    write_lines(canvas, left + 5 * mm, notice_top - 28 * mm, [
        "Beneficiary:   Northwind Logistics",
        "Bank:              IDFC FIRST Bank, Sector 29, Gurugram",
        "Account no.:   10094427730",
        "IFSC:              IDFB0040101",
    ], "Helvetica-Bold", 9.5, leading=13)

    canvas.setFillColor(colors.HexColor("#444444"))
    write_lines(canvas, left, 26 * mm, [
        "Northwind Logistics LLP, Plot 88, Udyog Vihar Phase IV, Gurugram 122015, Haryana. GSTIN 06AAHFN7302P1Z3",
        "Payment queries: accounts.northwind@fastmail.example or +91 98100 55501. Interest at 18% p.a. on overdue invoices.",
    ], size=7.5, leading=10.5)
    canvas.save()


# --------------------------------------------------------------------------- layout 3: services letterhead


def brightpath_invoice(path: Path) -> None:
    """A consulting-style invoice: third bill against one PO, with unapproved extras added."""
    canvas = Canvas(str(path), pagesize=A4)
    left, right = 22 * mm, PAGE_WIDTH - 22 * mm
    blue = colors.HexColor("#1F3A93")

    canvas.setFillColor(blue)
    write_lines(canvas, left, PAGE_HEIGHT - 24 * mm, ["Brightpath IT Services"], "Helvetica-Bold", 18)
    canvas.setFillColor(colors.HexColor("#555555"))
    write_lines(canvas, left, PAGE_HEIGHT - 30 * mm, ["Technology consulting and integration"], size=9)
    write_lines(canvas, right, PAGE_HEIGHT - 19 * mm, [
        "Brightpath IT Services Pvt Ltd", "Unit 502, Cerebrum IT Park, Kalyani Nagar", "Pune 411014, Maharashtra",
        "GSTIN: 27AAGCB5519R1Z9", "finance@brightpath.example",
    ], size=8, leading=10.5, align="right")
    canvas.setStrokeColor(blue)
    canvas.setLineWidth(1.5)
    canvas.line(left, PAGE_HEIGHT - 44 * mm, right, PAGE_HEIGHT - 44 * mm)
    canvas.setFillColor(colors.black)

    write_lines(canvas, left, PAGE_HEIGHT - 56 * mm, ["Invoice"], "Helvetica-Bold", 16)
    fields = [("Invoice #", "BIT/2026/163"), ("Date", "05-Nov-2026"), ("Purchase Order", "PO-2026-0388"),
              ("Project", "ERP integration for Meridian Retail"), ("Billing stage", "Milestone 3 of 3")]
    y = PAGE_HEIGHT - 66 * mm
    for label, value in fields:
        write_lines(canvas, left, y, [label], size=9)
        write_lines(canvas, left + 34 * mm, y, [value], "Helvetica-Bold", 9.5)
        y -= 5.6 * mm
    write_lines(canvas, right - 62 * mm, PAGE_HEIGHT - 66 * mm, ["Bill To"], "Helvetica-Bold", 8.5)
    write_lines(canvas, right - 62 * mm, PAGE_HEIGHT - 71 * mm, [BUYER_NAME, *BUYER_ADDRESS, f"GSTIN: {BUYER_GSTIN}"],
                size=9, leading=12)

    rows = [["#", "Description of services", "SAC", "Amount (INR)"],
            ["1", cell("Milestone 3: UAT and go-live support, as per SOW section 4.3"), "998313", rupees(240000)],
            ["2", cell("Additional change requests CR-07 and CR-09 (custom GST report, vendor portal sync)"),
             "998313", rupees(60000)],
            ["", "", "Subtotal", rupees(300000)],
            ["", "", "IGST @ 18%", rupees(54000)],
            ["", "", "Total", rupees(354000)]]
    table_bottom = draw_table(
        canvas, rows, [10 * mm, right - left - 78 * mm, 32 * mm, 36 * mm], left, PAGE_HEIGHT - 104 * mm,
        [("FONT", (0, 0), (-1, -1), "Helvetica", 9.5), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EDF8")), ("LINEBELOW", (0, 0), (-1, 2), 0.5, colors.grey),
         ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("FONT", (2, -1), (-1, -1), "Helvetica-Bold", 11),
         ("LINEABOVE", (2, -1), (-1, -1), 1, blue), ("VALIGN", (0, 0), (-1, -1), "TOP"),
         ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)],
    )
    y = write_lines(canvas, left, table_bottom - 8 * mm, [f"Amount in words: {in_words(354000)}"], "Helvetica-Oblique", 9)
    y = write_lines(canvas, left, y - 8 * mm, ["Payment details"], "Helvetica-Bold", 9.5)
    y = write_lines(canvas, left, y - 1.5 * mm, [
        "Account name: Brightpath IT Services Pvt Ltd", "Bank: ICICI Bank, Kalyani Nagar, Pune",
        "Account number: 000405118842", "IFSC: ICIC0000004", "Terms: 30 days from invoice date",
    ], size=9, leading=12.5)
    write_lines(canvas, right, y - 12 * mm, ["For Brightpath IT Services Pvt Ltd", "", "", "Authorised signatory"],
                size=9, leading=12, align="right")
    canvas.save()


# --------------------------------------------------------------------------- layout 4: bill-book page, then scanned


def sahyadri_bill(path: Path) -> None:
    """A small print shop's bill: no PO number, one bundled line, tax included in the price."""
    canvas = Canvas(str(path), pagesize=A4)
    left, right, center = 24 * mm, PAGE_WIDTH - 24 * mm, PAGE_WIDTH / 2
    canvas.setLineWidth(1.4)
    canvas.rect(left - 6 * mm, 40 * mm, right - left + 12 * mm, PAGE_HEIGHT - 62 * mm)
    canvas.setLineWidth(0.5)
    canvas.rect(left - 4.5 * mm, 41.5 * mm, right - left + 9 * mm, PAGE_HEIGHT - 65 * mm)

    y = write_lines(canvas, center, PAGE_HEIGHT - 38 * mm, ["SAHYADRI PRINT WORKS"], "Times-Bold", 23, align="center")
    y = write_lines(canvas, center, y - 1 * mm, ["Offset & Digital Printers, Stationers"], "Times-Italic", 11, align="center")
    y = write_lines(canvas, center, y - 1 * mm, ["Shop No. 14, Laxmi Road, Narayan Peth, Pune 411030",
                                                 "GSTIN: 27ABXFS6640K1Z2        Mob: 98220 55119"],
                    "Times-Roman", 10.5, leading=14, align="center")
    canvas.setLineWidth(1)
    canvas.line(left - 4.5 * mm, y, right + 4.5 * mm, y)

    y -= 9 * mm
    write_lines(canvas, center, y, ["INVOICE"], "Times-Bold", 13, align="center")
    y -= 10 * mm
    write_lines(canvas, left, y, ["No."], "Times-Roman", 11)
    write_lines(canvas, left + 10 * mm, y, ["SPW/1047"], "Courier-Bold", 13)
    write_lines(canvas, right - 34 * mm, y, ["Date:"], "Times-Roman", 11)
    write_lines(canvas, right, y, ["03-11-2026"], "Courier-Bold", 12, align="right")

    y -= 11 * mm
    write_lines(canvas, left, y, ["To,"], "Times-Roman", 11)
    y = write_lines(canvas, left + 8 * mm, y - 5.5 * mm, ["M/s Meridian Retail Pvt Ltd",
                                                           "Prestige Tech Park, Outer Ring Road, Bengaluru - 560103",
                                                           f"GSTIN: {BUYER_GSTIN}"], "Courier", 10.5, leading=14)
    y -= 3 * mm
    write_lines(canvas, left, y, ["Ref:"], "Times-Roman", 11)
    write_lines(canvas, left + 10 * mm, y, ["As per your order dt. 21-10-2026"], "Courier", 10.5)

    rows = [["Sr.", "Particulars", "Qty", "Amount (Rs.)"],
            ["1", cell("Printing & supply of A4 tri-fold brochures, 130 gsm art paper, 4 colour both sides, "
                       "incl. design corrections", 10.5, "Courier"), "5000 nos", "1,01,480.00"],
            ["", "", "", ""], ["", "", "", ""],
            ["", "", "TOTAL  Rs.", "1,01,480.00"]]
    table_bottom = draw_table(
        canvas, rows, [12 * mm, right - left - 82 * mm, 32 * mm, 38 * mm], left, y - 8 * mm,
        [("FONT", (0, 0), (-1, 0), "Times-Bold", 11), ("FONT", (0, 1), (-1, -2), "Courier", 10.5),
         ("FONT", (0, -1), (-1, -1), "Times-Bold", 12), ("FONT", (-1, -1), (-1, -1), "Courier-Bold", 12.5),
         ("BOX", (0, 0), (-1, -1), 0.9, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.9, colors.black),
         ("LINEBEFORE", (1, 0), (-1, -2), 0.6, colors.black), ("LINEBEFORE", (-1, -1), (-1, -1), 0.6, colors.black),
         ("LINEABOVE", (0, -1), (-1, -1), 0.9, colors.black), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
         ("ALIGN", (0, 0), (0, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
         ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)],
    )
    y = write_lines(canvas, right, table_bottom - 6 * mm, ["(Amount is inclusive of GST @ 18%)"], "Times-Italic", 10.5,
                    align="right")
    y = write_lines(canvas, left, y - 5 * mm, [in_words(101480)], "Courier", 10.5)

    write_lines(canvas, left, 58 * mm, ["Terms: Payment within 15 days by cheque / NEFT.", "Subject to Pune jurisdiction.  E. & O.E."],
                "Times-Roman", 9, leading=12)
    write_lines(canvas, right, 80 * mm, ["For Sahyadri Print Works"], "Times-Bold", 11, align="right")
    canvas.setLineWidth(1.1)
    signature = canvas.beginPath()
    signature.moveTo(right - 44 * mm, 64 * mm)
    signature.curveTo(right - 38 * mm, 76 * mm, right - 33 * mm, 56 * mm, right - 27 * mm, 69 * mm)
    signature.curveTo(right - 24 * mm, 75 * mm, right - 19 * mm, 60 * mm, right - 9 * mm, 67 * mm)
    canvas.drawPath(signature)
    write_lines(canvas, right, 52 * mm, ["Proprietor"], "Times-Roman", 10, align="right")
    canvas.save()


def make_it_a_scan(clean_pdf: Path, scanned_pdf: Path) -> None:
    """Rasterise the page, then rotate, fade, shade and add noise: an image-only PDF like a phone scan."""
    page = pymupdf.open(clean_pdf)[0]
    pixels = page.get_pixmap(dpi=160)
    image = Image.open(io.BytesIO(pixels.tobytes("png"))).convert("L")
    image = image.rotate(1.3, resample=Image.BICUBIC, fillcolor=255)

    rng = np.random.default_rng(seed=7)
    page_array = np.asarray(image).astype(np.float32)
    height, width = page_array.shape
    shading = np.linspace(0.9, 1.0, width)[None, :] * np.linspace(1.0, 0.95, height)[:, None]
    page_array = 255 - (255 - page_array) * 0.86  # ink fades
    page_array = page_array * shading * 0.97  # uneven light on off-white paper
    page_array += rng.normal(0, 6.5, page_array.shape)  # sensor noise
    scan = Image.fromarray(np.clip(page_array, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.65))
    scan.save(scanned_pdf, "PDF", resolution=160.0, quality=62)


# --------------------------------------------------------------------------- the inbox

INBOX = [
    {"id": "kaveri-furniture", "file": "01-kaveri-chairs-and-desks.pdf", "sender": "Kaveri Office Supplies",
     "subject": "Invoice for the chairs and desks delivered last week",
     "preview": "Please find attached our tax invoice against your PO-2026-0412. Kindly process at the earliest."},
    {"id": "northwind-freight", "file": "02-northwind-october-freight.pdf", "sender": "Northwind Logistics",
     "subject": "October freight invoice, and please note our new bank details",
     "preview": "Attaching the invoice for the October lanes. We have changed banks, so please use the new account."},
    {"id": "brightpath-milestone", "file": "03-brightpath-final-milestone.pdf", "sender": "Brightpath IT Services",
     "subject": "ERP integration: final milestone invoice",
     "preview": "Go-live is complete. Invoice for milestone 3 attached, including the two change requests."},
    {"id": "sahyadri-brochures", "file": "04-sahyadri-brochures-scan.pdf", "sender": "Sahyadri Print Works",
     "subject": "Bill for brochures (scan attached)",
     "preview": "Sir, brochures were delivered on Friday. Sending a scan of the bill, original is with the driver."},
    {"id": "kaveri-reminder", "file": "05-kaveri-payment-reminder.pdf", "sender": "Kaveri Office Supplies",
     "subject": "Reminder: is this invoice still pending?",
     "preview": "Our records show this October invoice as unpaid. Resending a copy for your reference."},
]


def main() -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    kaveri_tax_invoice(
        SAMPLES_DIR / INBOX[0]["file"], number="KOS/26-27/0931", date="4-Nov-2026",
        po_number="PO-2026-0412", po_date="12-Oct-2026",
        items=[("Ergonomic Office Chair, Mesh Back (Model EC-210)", "94013000", 4500, "40 Nos", 180000),
               ("Height-Adjustable Desk 1500 mm (Model HD-15)", "94033090", 10000, "6 Nos", 60000)],
    )
    northwind_invoice(SAMPLES_DIR / INBOX[1]["file"])
    brightpath_invoice(SAMPLES_DIR / INBOX[2]["file"])

    clean_bill = SAMPLES_DIR / "_sahyadri_clean.pdf"
    sahyadri_bill(clean_bill)
    make_it_a_scan(clean_bill, SAMPLES_DIR / INBOX[3]["file"])
    clean_bill.unlink()

    kaveri_tax_invoice(
        SAMPLES_DIR / INBOX[4]["file"], number="KOS-26-27-874", date="3-Oct-2026",
        po_number="PO-2026-0379", po_date="25-Sep-2026", stamp="REMINDER - PAYMENT OVERDUE",
        items=[("Stationery and Pantry Consumables for October (as per attached list)", "48201090", 55000, "1 Lot", 55000)],
    )
    (SAMPLES_DIR / "manifest.json").write_text(json.dumps(INBOX, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(INBOX)} invoices and manifest.json to {SAMPLES_DIR}")


if __name__ == "__main__":
    main()
