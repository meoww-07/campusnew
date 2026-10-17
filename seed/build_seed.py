"""Regenerates the seed PDFs and applies narrow no-break spaces in the chat export.

Run from the project root:  python seed/build_seed.py
(The generated files are committed; you only need this to change them.)
"""
from __future__ import annotations

from pathlib import Path

import fitz

SEED = Path(__file__).resolve().parent
CHAT = SEED / "WhatsApp Chat with CSE 3rd Year Sec A.txt"

MIDSEM = """NATIONAL INSTITUTE OF TECHNOLOGY - ACADEMIC SECTION
Circular No. ACAD/2026/114                                  Date: 25 September 2026

Subject: Mid-Semester Examination Schedule - Odd Semester 2026-27 (B.Tech CSE, 5th Semester)

The mid-semester examinations for B.Tech CSE 5th semester will be held as per the schedule below.

Date                      Time                 Course
21 October 2026 (Wed)     10:00 am - 11:30 am  CS301 Database Management Systems
22 October 2026 (Thu)     10:00 am - 11:30 am  CS302 Operating Systems
26 October 2026 (Mon)     10:00 am - 11:30 am  CS303 Computer Networks
27 October 2026 (Tue)     10:00 am - 11:30 am  CS304 Compiler Design
28 October 2026 (Wed)     10:00 am - 11:30 am  CS305 Software Engineering

Venue: Lecture Hall Complex (LH-3 and LH-4). Seating plan will be displayed one day before.

Instructions:
1. Students must report 15 minutes before the start of the examination.
2. College ID card is mandatory. Students without ID cards will not be allowed.
3. Mobile phones and smart watches are strictly prohibited in the examination hall.
4. Non-programmable scientific calculators are allowed only for CS303 Computer Networks.

Dean (Academics)"""

ACCOUNTS_P1 = """ACCOUNTS OFFICE - CIRCULAR
Ref: ACC/FEE/2026/09                                          Date: 5 October 2026

Subject: Fee payment and scholarship deadlines - Odd Semester 2026-27

1. The tuition fee for the odd semester must be paid by 31 October 2026 through the SBI Collect portal.
   A late fee of Rs. 500 per week will be charged after the due date.
2. Hostel and mess fees for the semester must be paid by 5 November 2026.
3. Post-matric and merit scholarship application forms must be submitted to the Accounts Office
   by 25 October 2026, along with the income certificate and the fee receipt.
4. Students with an education loan should submit the bank sanction letter by 31 October 2026
   to avoid the late fee.

For queries contact accounts@college.edu.in or visit Room 104, Admin Block (10 am - 4 pm)."""

ACCOUNTS_P2 = """LIBRARY NOTICE

During the mid-semester examinations, the Central Library reading hall will remain open
from 8:00 am to 11:00 pm, starting 19 October 2026 until 30 October 2026.

Books issued before 1 October 2026 must be returned or renewed by 20 October 2026.
A fine of Rs. 5 per day applies to overdue books.

Librarian"""


def write_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()  # A4
        page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=9.5, fontname="cour")
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def main() -> None:
    write_pdf(SEED / "Midsem_Schedule_Circular.pdf", [MIDSEM])
    write_pdf(SEED / "Accounts_and_Library_Circular.pdf", [ACCOUNTS_P1, ACCOUNTS_P2])
    # Newer Android exports put U+202F (narrow no-break space) before am/pm.
    text = CHAT.read_text(encoding="utf-8").replace("§", " ")
    CHAT.write_text(text, encoding="utf-8")
    print("seed files written")


if __name__ == "__main__":
    main()
