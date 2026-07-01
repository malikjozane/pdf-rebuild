"""
Run Sheet PDF Rebuilder - v4 (Cell-by-Cell Extraction)
=======================================================
Uses the PDF's own grid lines as exact cell clipping boundaries.
No guessing about column positions — every cell is extracted from
its precise (x0,y0,x1,y1) rectangle defined by the grid.
"""

import fitz
import io
import re
import sys
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    Image as RLImage, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import arabic_reshaper
from bidi.algorithm import get_display

# Register Arabic Font (bundled with the app)
font_path = os.path.join(os.path.dirname(__file__), 'arial_unicode.ttf')
pdfmetrics.registerFont(TTFont('ArabicFont', font_path))
pdfmetrics.registerFontFamily('ArabicFont', normal='ArabicFont', bold='ArabicFont', italic='ArabicFont', boldItalic='ArabicFont')

def fix_arabic(text):
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(str(text)))

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_image_bytes(src_doc, xref):
    try:
        raw = src_doc.extract_image(xref)
        return raw["image"]
    except Exception:
        return None


def bytes_to_rl(img_bytes, width, height):
    buf = io.BytesIO(img_bytes)
    return RLImage(buf, width=width, height=height)


def cell_text(page, x0, y0, x1, y1):
    """Extract text from exact cell rectangle (with 1pt inset)."""
    return page.get_text("text", clip=fitz.Rect(x0+1, y0+1, x1-1, y1-1)).strip()


def get_grid(page):
    """Return sorted (h_lines, v_lines) from the page's path drawings."""
    paths = page.get_drawings()
    h_lines = sorted(set(round(p["rect"].y0) for p in paths
                         if p["rect"].height < 2 and p["rect"].width > 30))
    v_lines = sorted(set(round(p["rect"].x0) for p in paths
                         if p["rect"].width < 2 and p["rect"].height > 30))
    return h_lines, v_lines


# ─── Data Extraction ──────────────────────────────────────────────────────────

# Column name mapping (by position in the column-header row)
# We detect columns by reading the header row and matching labels.
COL_ORDER = ["num", "awb", "consignee", "delivered", "status",
             "cod", "area", "attempted", "sender", "address"]

def identify_columns(page, h_lines, v_lines):
    """
    Read the column header row and return a list of column names
    in order, aligned to v_lines segments.
    """
    # Find the header row: look for a row that contains 'Status' or 'Consignee'
    header_row_idx = None
    for i in range(len(h_lines) - 1):
        row_text = cell_text(page, v_lines[0], h_lines[i], v_lines[-1], h_lines[i+1])
        if "Status" in row_text and "Consignee" in row_text:
            header_row_idx = i
            break
    
    if header_row_idx is None:
        return None, None
    
    y0, y1 = h_lines[header_row_idx], h_lines[header_row_idx + 1]
    
    # Read each cell in the header row
    col_labels = []
    for j in range(len(v_lines) - 1):
        txt = cell_text(page, v_lines[j], y0, v_lines[j+1], y1)
        col_labels.append(txt.replace("\n", " ").strip())
    
    # Map header text → canonical name
    label_map = {
        "#":          "num",
        "AWB":        "awb",
        "Consignee":  "consignee",
        "Delivered":  "delivered",
        "Status":     "status",
        "COD":        "cod",
        "Area":       "area",
        "Attempted":  "attempted",
        "Sender":     "sender",
        "Address":    "address",
    }
    
    col_names = []
    for lbl in col_labels:
        matched = None
        for key, name in label_map.items():
            if key.lower() in lbl.lower():
                matched = name
                break
        col_names.append(matched or lbl.lower() or "extra")
    
    return col_names, header_row_idx


def find_barcode_for_row(page, y0, y1):
    """
    Find the AWB barcode image for a data row by its Y position.
    Barcodes are always in the left portion of the page (x < 200).
    We search by Y overlap with the row, not by exact cell x-bounds,
    because on page 1 the AWB cell only spans x=26-99 but barcodes
    are 28-168 (spanning into the consignee column).
    """
    img_info = page.get_image_info(xrefs=True)
    for img in img_info:
        bx = img["bbox"]
        # Must be in the left zone (barcode column)
        if img["width"] < 50:
            continue
        if bx[2] > 200:   # too far right — not a row barcode
            continue
        # Y overlap: barcode center must fall within row bounds
        bc_center_y = (bx[1] + bx[3]) / 2
        if y0 <= bc_center_y <= y1:
            return img["xref"]
    return None


def extract_all_data(src_doc):
    """Extract all rows using exact grid cell boundaries."""
    all_rows = []
    
    for page_num in range(len(src_doc)):
        page = src_doc[page_num]
        h_lines, v_lines = get_grid(page)
        
        if not h_lines or not v_lines:
            continue
        
        col_names, header_idx = identify_columns(page, h_lines, v_lines)
        if col_names is None:
            continue
        
        print(f"  Page {page_num+1}: cols={col_names} header_row={header_idx}")
        
        # Data rows are all rows after the header row
        for row_i in range(header_idx + 1, len(h_lines) - 1):
            y0 = h_lines[row_i]
            y1 = h_lines[row_i + 1]
            
            row = {name: "" for name in COL_ORDER}
            row["barcode_xref"] = None
            row["page_num"] = page_num
            
            # We might have multiple grid cells mapping to the same col_name (e.g. AWB on page 1)
            # Group cells by col_name to combine them horizontally
            col_text_parts = {name: [] for name in set(col_names) if name != "extra"}
            
            for col_j, col_name in enumerate(col_names):
                if col_name == "extra":
                    continue
                if col_j >= len(v_lines) - 1:
                    break
                cx0 = v_lines[col_j]
                cx1 = v_lines[col_j + 1]
                
                txt = cell_text(page, cx0, y0, cx1, y1)
                col_text_parts[col_name].append(txt)
                
                if col_name == "awb" and row["barcode_xref"] is None:
                    # Extract barcode image from this cell area (only look it up once per row)
                    row["barcode_xref"] = find_barcode_for_row(page, y0, y1)

            # Horizontally combine split cells
            for name, txt_parts in col_text_parts.items():
                if not txt_parts:
                    continue
                if name == "awb":
                    # Use words extraction to properly combine text from split cells
                    # We must use the FULL width of the AWB block, which is the last v_line before 'consignee'
                    consignee_idx = col_names.index("consignee")
                    first_cx0 = v_lines[1]
                    last_cx1 = v_lines[consignee_idx]
                    
                    words = page.get_text("words", clip=fitz.Rect(first_cx0+1, y0+1, last_cx1-1, y1-1))
                    lines_by_y = {}
                    for w in words:
                        mid_y = round((w[1] + w[3]) / 2 / 5) * 5
                        if mid_y not in lines_by_y:
                            lines_by_y[mid_y] = []
                        lines_by_y[mid_y].append(w)
                        
                    extracted_lines = []
                    for mid_y in sorted(lines_by_y.keys()):
                        line_words = sorted(lines_by_y[mid_y], key=lambda x: x[0])
                        text = "".join(w[4] for w in line_words)
                        extracted_lines.append(text)
                    
                    awb = ""
                    orderref = ""
                    for line in extracted_lines:
                        digits = re.sub(r'\D', '', line)
                        if len(digits) >= 12:
                            awb = line
                        elif not awb and len(digits) >= 8:
                            awb = line
                        elif line.strip() and awb and line != awb:
                            orderref = line.strip()
                            
                    row["awb"] = awb
                    row["orderref"] = orderref
                    
                else:
                    if len(txt_parts) == 1:
                        row[name] = txt_parts[0]
                    else:
                        # Combine horizontally
                        lines_groups = [txt.split('\n') for txt in txt_parts]
                        max_lines = max(len(lg) for lg in lines_groups)
                        combined_lines = []
                        for i in range(max_lines):
                            parts = []
                            for lg in lines_groups:
                                if i < len(lg):
                                    parts.append(lg[i].strip())
                            combined_lines.append(" ".join(parts))
                        row[name] = '\n'.join(combined_lines)
            
            # Only include rows that have a row number
            num_m = re.search(r'\d+', row.get("num", ""))
            if num_m:
                row["num"] = num_m.group()
                
                # Clean COD: remove "Dispatched" prefix that sometimes bleeds in
                row["cod"] = row["cod"].replace("Dispatched", "").replace("d\n", "").strip()
                all_rows.append(row)
    
    return all_rows


def extract_header(src_doc):
    """Extract title, subtitle, summary, logo, and header barcode from page 1."""
    page = src_doc[0]
    h = {}
    h["title"]    = page.get_text("text", clip=fitz.Rect(0, 15, 580, 45)).split("\n")[0].strip()
    h["subtitle"] = page.get_text("text", clip=fitz.Rect(0, 45, 580, 65)).split("\n")[0].strip()
    
    summary_raw = page.get_text("text", clip=fitz.Rect(583, 19, 840, 93)).split("\n")
    summary = {}
    for i in range(0, len(summary_raw) - 1, 2):
        k = summary_raw[i].strip()
        v = summary_raw[i+1].strip() if i + 1 < len(summary_raw) else ""
        if k:
            summary[k] = v
    h["summary"] = summary
    
    img_info = page.get_image_info(xrefs=True)
    h["logo_xref"] = next(
        (img["xref"] for img in img_info if img["bbox"][0] < 100 and img["bbox"][1] < 100),
        None
    )
    h["hdr_barcode_xref"] = next(
        (img["xref"] for img in img_info if 200 < img["bbox"][0] < 500 and 60 < img["bbox"][1] < 100),
        None
    )
    return h


# ─── PDF Builder ──────────────────────────────────────────────────────────────

def build_pdf(src_doc, header, rows, output_path):
    PAGE_W, PAGE_H = landscape(A4)
    MARGIN = 8
    avail_w = PAGE_W - 2 * MARGIN   # ~825pt
    styles = getSampleStyleSheet()

    def ps(name, **kw):
        d = dict(parent=styles["Normal"], fontSize=6.5, leading=8.5,
                 spaceAfter=0, spaceBefore=0)
        d.update(kw)
        return ParagraphStyle(name, **d)

    tiny = ps("tny", fontSize=5.5, leading=7, fontName="ArabicFont")
    tinyb = ps("tnyb", fontSize=5.5, leading=7, fontName="ArabicFont")
    smb = ps("smb", fontSize=7, leading=8, alignment=TA_CENTER, fontName="ArabicFont")
    chdr = ps("chdr", fontSize=6, alignment=TA_CENTER, fontName="ArabicFont")

    # Document setup (landscape A4 with small margins)
    MARGIN = 12
    avail_w = A4[1] - (2 * MARGIN)
    doc = SimpleDocTemplate(
        output_path,
        pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    story = []

    # ── Page Header ────────────────────────────────────────────────────────────
    logo_cell = ""
    if header.get("logo_xref"):
        img_bytes = extract_image_bytes(src_doc, header["logo_xref"])
        if img_bytes:
            logo_cell = bytes_to_rl(img_bytes, 38, 38)

    title_parts = [
        Paragraph(f"<b>{fix_arabic(header['title'])}</b>",
                  ps("t", fontSize=8, alignment=TA_CENTER,
                     fontName="ArabicFont", leading=11))
    ]
    if header.get("subtitle"):
        title_parts.append(Paragraph(fix_arabic(header["subtitle"]),
                                     ps("st", fontSize=7, alignment=TA_CENTER, fontName="ArabicFont", leading=9)))
    if header.get("hdr_barcode_xref"):
        hb = extract_image_bytes(src_doc, header["hdr_barcode_xref"])
        if hb:
            title_parts += [Spacer(1, 2), bytes_to_rl(hb, 90, 18)]

    summary_rows = [[Paragraph(f"<b>{k}</b>", tiny), Paragraph(v, tiny)]
                    for k, v in header["summary"].items()]
    SW = [78, 82]
    summ = Table(summary_rows, colWidths=SW)
    summ.setStyle(TableStyle([
        ("GRID",          (0,0),(-1,-1), 0.4, colors.black),
        ("BACKGROUND",    (0,0),(0,-1),  colors.Color(0.9,0.9,0.9)),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0),(-1,-1), 3),
        ("RIGHTPADDING",  (0,0),(-1,-1), 3),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
    ]))

    LOGO_W = 46
    SUMM_W = sum(SW)
    CTR_W  = avail_w - LOGO_W - SUMM_W

    hdr = Table([[logo_cell, title_parts, summ]], colWidths=[LOGO_W, CTR_W, SUMM_W])
    hdr.setStyle(TableStyle([
        ("BOX",           (0,0),(-1,-1), 0.8, colors.black),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("ALIGN",         (1,0),(1,0),   "CENTER"),
        ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 3))

    # ── Data Table ─────────────────────────────────────────────────────────────
    # Column widths (must sum to avail_w ~825pt)
    CW = {
        "num":       16,
        "awb":       104,
        "consignee": 90,
        "delivered": 20,
        "status":    46,
        "cod":       40,
        "area":      74,
        "attempted": 50,
        "sender":    64,
        "address":   None,
    }
    CW["address"] = avail_w - sum(v for v in CW.values() if v)
    col_names_ordered = list(CW.keys())
    col_widths = [CW[k] for k in col_names_ordered]

    # Column header row
    hdr_labels = ["#", "AWB Number", "Consignee", "Del.", "Status",
                  "COD", "Area/Zone", "Attempted On", "Sender", "Address"]
    tbl_data = [[Paragraph(fix_arabic(lbl), chdr) for lbl in hdr_labels]]
    row_heights = [16]

    for row in rows:
        # ── AWB cell: AWB number + barcode + order ref ──────────────────────
        awb_cell = []
        if row.get("awb"):
            awb_cell.append(Paragraph(f"<b>{fix_arabic(row['awb'])}</b>", tinyb))
        if row.get("barcode_xref"):
            img_bytes = extract_image_bytes(src_doc, row["barcode_xref"])
            if img_bytes:
                awb_cell.append(bytes_to_rl(img_bytes, CW["awb"] - 4, 14))
        if row.get("orderref"):
            awb_cell.append(Paragraph(f"<b>{fix_arabic(row['orderref'])}</b>", tinyb))

        # ── Consignee: strip "Name:" and "Phone:" prefixes ──────────────────
        consignee = (row.get("consignee", "")
                     .replace("Name:\n", "").replace("Name: ", "")
                     .replace("Phone:\n", "Ph: ").replace("Phone: ", "Ph: ")
                     .strip())

        # ── COD: already cleaned ─────────────────────────────────────────────
        cod = row.get("cod", "").strip()

        tbl_data.append([
            Paragraph(fix_arabic(row.get("num", "")),       smb),
            awb_cell,
            Paragraph(fix_arabic(consignee),                tiny),
            "",                                  # Delivered checkbox (blank)
            Paragraph(fix_arabic(row.get("status", "")),    tiny),
            Paragraph(fix_arabic(cod),                      tiny),
            Paragraph(fix_arabic(row.get("area", "")),      tiny),
            Paragraph(fix_arabic(row.get("attempted", "")), tiny),
            Paragraph(fix_arabic(row.get("sender", "")),    tiny),
            Paragraph(fix_arabic(row.get("address", "")),   tiny),
        ])
        row_heights.append(None)   # auto-size to content

    data_tbl = Table(tbl_data, colWidths=col_widths,
                     rowHeights=row_heights, repeatRows=1)

    ts = [
        ('FONT', (0, 0), (-1, -1), 'ArabicFont', 5.8),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('FONT', (0, 0), (-1, 0), 'ArabicFont', 6),
        ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ('BOX', (0, 0), (-1, -1), 0.25, colors.black),
        # Align specific columns
        ('ALIGN', (0, 1), (0, -1), 'CENTER'),
        ('VALIGN', (0, 1), (0, -1), 'MIDDLE'),
        ('ALIGN', (1, 1), (1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'LEFT'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'CENTER'),
        ("LEFTPADDING",   (0,0),(-1,-1), 2),
        ("RIGHTPADDING",  (0,0),(-1,-1), 2),
        ("TOPPADDING",    (0,0),(-1,-1), 1),
        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
    ]
    # Alternating row shading
    for i in range(1, len(tbl_data)):
        bg = colors.white if i % 2 == 1 else colors.Color(0.95, 0.95, 0.97)
        ts.append(("BACKGROUND", (0, i), (-1, i), bg))

    data_tbl.setStyle(TableStyle(ts))
    story.append(data_tbl)

    doc.build(story)
    print(f"\n✅  Saved → {output_path}")
    print(f"    {len(rows)} delivery rows  |  {len(tbl_data)-1} table rows")


# ─── Main ─────────────────────────────────────────────────────────────────────

def process_pdf(inp, out):
    print(f"Source: {inp}")
    src = fitz.open(inp)

    print("\nExtracting header...")
    header = extract_header(src)
    print(f"  Title:  {header['title']}")
    print(f"  Driver: {header['summary']}")

    print("\nExtracting rows (cell-by-cell from grid)...")
    rows = extract_all_data(src)
    print(f"\n  Total rows found: {len(rows)}")
    for r in rows:
        print(f"  [{r['num']:>2}]  AWB={r['awb'][:13]:13}  "
              f"status={repr(r['status']):<12} cod={repr(r['cod']):<10} area={repr(r['area'])}")

    print("\nBuilding output PDF...")
    build_pdf(src, header, rows, out)
    src.close()


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "document.pdf"
    out = sys.argv[2] if len(sys.argv) > 2 else "document_rebuilt.pdf"
    process_pdf(inp, out)

if __name__ == "__main__":
    main()
