"""Parse mortgage loan statement PDFs (CrossCountry Mortgage, Mr. Cooper, etc.)."""

import re
from datetime import datetime

import pdfplumber

from importers.parse_utils import clean_amount_unsigned


def is_mortgage_pdf(filepath):
    """Check if a PDF is a mortgage loan statement."""
    try:
        with pdfplumber.open(filepath) as pdf:
            first_page = pdf.pages[0].extract_text() or ""
        return "MORTGAGE LOAN STATEMENT" in first_page
    except (FileNotFoundError, PermissionError):
        raise
    except Exception:
        return False


def parse_mortgage_pdf(filepath):
    """Parse a mortgage statement PDF. Returns loan details and payment info."""
    with pdfplumber.open(filepath) as pdf:
        # Only need first page for all the data
        text = pdf.pages[0].extract_text() or ""

    result = {
        "lender": "",
        "loan_number": "",
        "property_address": "",
        "statement_date": "",
        "payment_due_date": "",
        "interest_rate": 0,
        "principal_balance": 0,
        "monthly_payment": 0,
        "principal_portion": 0,
        "interest_portion": 0,
        "escrow_portion": 0,
        "escrow_balance": 0,
        "ytd_principal": 0,
        "ytd_interest": 0,
        "ytd_escrow": 0,
        "ytd_total": 0,
        "last_payment_principal": 0,
        "last_payment_interest": 0,
        "last_payment_date": "",
        "late_fee_date": "",
        "month": "",
    }

    # Lender
    if "CrossCountry" in text:
        result["lender"] = "CrossCountry Mortgage"
    elif "Mr. Cooper" in text:
        result["lender"] = "Mr. Cooper"
    elif "Nationstar" in text:
        result["lender"] = "Nationstar / Mr. Cooper"
    else:
        # Try to extract from the text
        result["lender"] = "Mortgage Lender"

    # Loan number
    ln_match = re.search(r"Loan Number:\s*(\d+)", text)
    if ln_match:
        result["loan_number"] = ln_match.group(1)

    # Statement date
    sd_match = re.search(r"Statement Date:\s*(\d{2}/\d{2}/\d{4})", text)
    if sd_match:
        result["statement_date"] = sd_match.group(1)
        try:
            dt = datetime.strptime(sd_match.group(1), "%m/%d/%Y")
            result["month"] = dt.strftime("%Y-%m")
        except ValueError:
            result["month"] = datetime.now().strftime("%Y-%m")

    # Payment due date
    pd_match = re.search(r"Payment Due Date:\s*(\d{2}/\d{2}/\d{4})", text)
    if pd_match:
        result["payment_due_date"] = pd_match.group(1)

    # Late fee date
    late_match = re.search(r"on or after (\d{2}/\d{2}/\d{4})", text)
    if late_match:
        result["late_fee_date"] = late_match.group(1)

    # Property address
    prop_match = re.search(r"Property Address:\s*\n\s*(.+?)(?:\n|$)", text)
    if prop_match:
        addr = prop_match.group(1).strip()
        # Check for continuation on next line
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if "Property Address:" in line:
                # Collect address lines after
                addr_lines = []
                for j in range(i + 1, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith("Account") and not next_line.startswith("You are"):
                        addr_lines.append(next_line)
                    else:
                        break
                if addr_lines:
                    addr = ", ".join(addr_lines)
                break
        result["property_address"] = addr

    # Interest rate
    rate_match = re.search(r"Interest Rate\s+([\d.]+)%", text)
    if rate_match:
        result["interest_rate"] = float(rate_match.group(1))

    # Principal balance
    bal_match = re.search(r"(?:Interest Bearing )?Principal Balance\s+\$?([\d,]+\.\d{2})", text)
    if bal_match:
        result["principal_balance"] = clean_amount_unsigned(bal_match.group(1))

    # Monthly payment (Amount Due or Regular Monthly Payment)
    pmt_match = re.search(r"Regular Monthly Payment\s+\$?([\d,]+\.\d{2})", text)
    if pmt_match:
        result["monthly_payment"] = clean_amount_unsigned(pmt_match.group(1))
    else:
        due_match = re.search(r"Amount Due:\s+\$?([\d,]+\.\d{2})", text)
        if due_match:
            result["monthly_payment"] = clean_amount_unsigned(due_match.group(1))

    # Escrow balance
    esc_match = re.search(r"Escrow Balance\s+\$?([\d,]+\.\d{2})", text)
    if esc_match:
        result["escrow_balance"] = clean_amount_unsigned(esc_match.group(1))

    # Payment breakdown — scope to "Explanation of Amounts Due" section
    amounts_section = re.search(
        r"Explanation of Amounts? Due(.*?)(?:Past Payment|Transaction Activity|$)",
        text, re.DOTALL,
    )
    amt_text = amounts_section.group(1) if amounts_section else ""

    prin_match = re.search(r"Principal\s+\$?([\d,]+\.\d{2})", amt_text)
    if prin_match:
        result["principal_portion"] = clean_amount_unsigned(prin_match.group(1))

    int_match = re.search(r"Interest\s+\$?([\d,]+\.\d{2})", amt_text)
    if int_match:
        result["interest_portion"] = clean_amount_unsigned(int_match.group(1))

    esc_pmt_match = re.search(r"Escrow Amount.*?\$?([\d,]+\.\d{2})", amt_text)
    if esc_pmt_match:
        result["escrow_portion"] = clean_amount_unsigned(esc_pmt_match.group(1))

    # Year to date — scope to "Past Payment Breakdown" section
    ytd_section_match = re.search(
        r"Past Payment Breakdown(.*?)(?:Transaction Activity|$)",
        text, re.DOTALL,
    )
    ytd_text = ytd_section_match.group(1) if ytd_section_match else ""

    ytd_prin = re.search(r"Principal\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", ytd_text)
    if ytd_prin:
        result["last_payment_principal"] = clean_amount_unsigned(ytd_prin.group(1))
        result["ytd_principal"] = clean_amount_unsigned(ytd_prin.group(2))

    ytd_int = re.search(r"Interest\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", ytd_text)
    if ytd_int:
        result["last_payment_interest"] = clean_amount_unsigned(ytd_int.group(1))
        result["ytd_interest"] = clean_amount_unsigned(ytd_int.group(2))

    ytd_esc = re.search(r"Escrow \(Taxes & Insurance\)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", ytd_text)
    if ytd_esc:
        result["ytd_escrow"] = clean_amount_unsigned(ytd_esc.group(2))

    ytd_total_match = re.search(r"Total\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", ytd_text)
    if ytd_total_match:
        result["ytd_total"] = clean_amount_unsigned(ytd_total_match.group(2))

    # Transaction activity - last payment date
    txn_match = re.search(r"(\d{2}/\d{2}/\d{4})\s+Payment\s+\$?([\d,]+\.\d{2})", text)
    if txn_match:
        result["last_payment_date"] = txn_match.group(1)

    return result


