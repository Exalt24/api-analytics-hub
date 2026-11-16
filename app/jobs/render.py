"""The tailored resume as DOCX (python-docx) and PDF (Chromium through Playwright). Plain, single column, real text, no table and no image, which is
what an applicant tracking parser reads best; the PDF is checked by reading its text back, not by looking at the file size."""
from __future__ import annotations

import html
import io

from docx import Document
from docx.shared import Pt

HEADINGS = {"SUMMARY", "SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


def to_docx(text: str) -> bytes:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if i == 0:
            doc.add_heading(line.strip(), level=0)
        elif line.strip() in HEADINGS:
            doc.add_heading(line.strip().title(), level=2)
        elif line.startswith("- "):
            doc.add_paragraph(line[2:].strip(), style="List Bullet")
        else:
            doc.add_paragraph(line.strip())
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def docx_text(data: bytes) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def to_html(text: str) -> str:
    out = ["<html><head><meta charset='utf-8'><style>body{font-family:Arial,sans-serif;font-size:10.5pt;margin:0.7in;line-height:1.35}"
           "h1{font-size:18pt;margin:0}h2{font-size:11pt;border-bottom:1px solid #444;margin:14px 0 4px}li{margin:2px 0}ul{margin:2px 0 6px 18px;padding:0}p{margin:3px 0}</style></head><body>"]
    in_list = False
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % html.escape(s[2:]))
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not s:
            continue
        if i == 0:
            out.append("<h1>%s</h1>" % html.escape(s))
        elif s in HEADINGS:
            out.append("<h2>%s</h2>" % html.escape(s.title()))
        else:
            out.append("<p>%s</p>" % html.escape(s))
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "".join(out)


def to_pdf(text: str) -> bytes:
    """Sync Playwright, because it is called from a worker thread. Needs the Chromium that `playwright install chromium` fetches."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(to_html(text))
            return page.pdf(format="Letter", print_background=False)
        finally:
            browser.close()


def pdf_text(data: bytes) -> str:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as d:
        return "\n".join(page.get_text() for page in d)
