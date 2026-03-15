"""Parse Wells Fargo credit card statements (PDF and year-end CSV)."""

import csv
import re
from datetime import datetime

import pdfplumber

from categorizer import categorize
from importers.parse_utils import clean_amount_unsigned

# Map Wells Fargo CSV categories to our categories
_WF_CAT_MAP = {
    "Food/Drink": "Dining",
    "Entertainment": "Entertainment",
    "Travel": "Travel",
    "Merchandise": "Shopping",
    "Automotive": "Gas",
    "Health Care": "Healthcare",
    "Insurance": "Insurance",
    "Education": "Education",
    "Home Improvement": "Housing",
    "Personal Care": "Personal Care",
}


def is_wellsfargo_pdf(filepath):
    """Check if a PDF is a Wells Fargo credit card statement."""
    try:
        with pdfplumber.open(filepath) as pdf:
            text = pdf.pages[0].extract_text() or ""
        return "Wells Fargo" in text and ("Summary of Account Activity" in text or "Billing Cycle" in text)
    except (FileNotFoundError, PermissionError):
        raise
    except Exception:
        return False


def is_wellsfargo_csv(filepath):
    """Check if a CSV is a Wells Fargo year-end export."""
    try:
        with open(filepath, "r") as f:
            header = f.readline()
        return "Master Category" in header and "Payment Method" in header
    except Exception:
        return False


def parse_wellsfargo_pdf(filepath):
    """Parse a Wells Fargo credit card statement PDF."""
    with pdfplumber.open(filepath) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
    full_text = "\n".join(pages_text)
    first_page = pages_text[0]

    # Account number
    acct_match = re.search(r"Account Number Ending in (\d+)", first_page)
    last4 = acct_match.group(1) if acct_match else "0000"

    # Balance
    bal_match = re.search(r"New Balance\s+\$?([\d,]+\.\d{2})", first_page)
    balance = clean_amount_unsigned(bal_match.group(1)) if bal_match else 0

    # Billing cycle for month
    cycle_match = re.search(r"Billing Cycle\s+(\d{2}/\d{2}/\d{4})\s+to\s+(\d{2}/\d{2}/\d{4})", first_page)
    if cycle_match:
        try:
            end_date = datetime.strptime(cycle_match.group(2), "%m/%d/%Y")
            month = end_date.strftime("%Y-%m")
            year = end_date.year
        except ValueError:
            month = datetime.now().strftime("%Y-%m")
            year = datetime.now().year
    else:
        month = datetime.now().strftime("%Y-%m")
        year = datetime.now().year

    # Card name
    card_name = "Wells Fargo Card"
    if "One Key" in full_text or "OneKey" in full_text:
        card_name = "Wells Fargo OneKey+"
    elif "Active Cash" in full_text:
        card_name = "Wells Fargo Active Cash"
    elif "Autograph" in full_text:
        card_name = "Wells Fargo Autograph"

    # Parse transactions from Transaction Summary section
    transactions = []
    lines = full_text.split("\n")
    in_transactions = False

    for line in lines:
        line = line.strip()

        if "Transaction Summary" in line or "Trans Date" in line:
            in_transactions = True
            continue
        if in_transactions and ("Fees Charged" in line or "TOTAL FEES" in line):
            in_transactions = False
            continue

        if not in_transactions:
            continue

        # Pattern: MM/DD MM/DD reference_number description $amount
        match = re.match(
            r"(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+\d+\s+(.+?)\s+\$?([\d,]+\.\d{2})$",
            line,
        )
        if not match:
            # Try without reference number merged into description
            match = re.match(
                r"(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+\$?([\d,]+\.\d{2})$",
                line,
            )
        if not match:
            continue

        post_date = match.group(2)
        description = match.group(3).strip()
        amount = clean_amount_unsigned(match.group(4))

        # Remove reference numbers, hashes, and trailing booking codes
        description = re.sub(r"^[\dA-Z]{8,}\s+", "", description)
        description = re.sub(r"^[\dA-Z]{8,}\s+", "", description)
        description = re.sub(r"\s+\d{10,}", "", description)  # trailing long numbers

        m_num, d_num = int(post_date[:2]), int(post_date[3:])
        try:
            full_date = datetime(year, m_num, d_num).strftime("%Y-%m-%d")
        except ValueError:
            try:
                full_date = datetime(year - 1, m_num, d_num).strftime("%Y-%m-%d")
            except ValueError:
                continue

        category = categorize(description)

        transactions.append({
            "date": full_date,
            "description": description,
            "amount": -amount,  # charges are expenses
            "category": category,
        })

    return {
        "type": "wellsfargo_credit_card",
        "card_name": card_name,
        "last4": last4,
        "balance": -balance,
        "month": month,
        "transactions": transactions,
    }


def parse_wellsfargo_csv(filepath):
    """Parse a Wells Fargo year-end CSV export."""
    transactions = []
    last4 = ""

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("Date", "").strip()
            description = row.get("Description", "").strip()
            payee = row.get("Payee", "").strip()
            amount_str = row.get("Amount", "").strip()
            master_cat = row.get("Master Category", "").strip()
            payment_method = row.get("Payment Method", "").strip()

            if not date_str or not amount_str:
                continue

            # Extract last digits from payment method
            if not last4:
                digits_match = re.search(r"\.\.\.(\d+)", payment_method)
                if digits_match:
                    last4 = digits_match.group(1)

            # Parse date
            try:
                txn_date = datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue

            # Parse amount (remove $ sign)
            amount_val = clean_amount_unsigned(amount_str)
            is_negative = "-" in amount_str

            # Use payee as description if shorter/cleaner
            desc = payee if payee and len(payee) < len(description) else description
            # Clean up description
            desc = re.sub(r"\s{2,}", " ", desc).strip()

            # Map category
            category = _WF_CAT_MAP.get(master_cat)
            if not category:
                if "Miscellaneous" in master_cat:
                    category = "Other"
                else:
                    category = categorize(desc)

            # Flip sign: positive in CSV = charge (expense), negative = credit/refund
            if is_negative:
                amount = amount_val  # credit/refund → positive
            else:
                amount = -amount_val  # charge → negative (expense)

            transactions.append({
                "date": txn_date,
                "description": desc,
                "amount": amount,
                "category": category,
            })

    # Determine months
    months = sorted(set(t["date"][:7] for t in transactions))

    return {
        "type": "wellsfargo_credit_card",
        "card_name": "Wells Fargo OneKey+",
        "last4": last4,
        "transactions": transactions,
        "months": months,
    }


