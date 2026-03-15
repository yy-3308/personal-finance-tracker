"""Parse HealthEquity HSA account statement PDFs."""

import re
from datetime import datetime

import pdfplumber

from categorizer import categorize
from importers.parse_utils import clean_amount_unsigned


def is_hsa_pdf(filepath):
    """Check if a PDF is a HealthEquity HSA statement."""
    try:
        with pdfplumber.open(filepath) as pdf:
            text = pdf.pages[0].extract_text() or ""
        return "HealthEquity" in text and "Account Statement" in text
    except (FileNotFoundError, PermissionError):
        raise
    except Exception:
        return False


def parse_hsa_pdf(filepath):
    """Parse a HealthEquity HSA statement PDF."""
    with pdfplumber.open(filepath) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
    full_text = "\n".join(pages_text)
    first_page = pages_text[0]

    # Account number
    acct_match = re.search(r"AccountNumber:\s*(\d+)", first_page)
    account_number = acct_match.group(1) if acct_match else ""

    # Statement period
    period_match = re.search(
        r"Period:\s*(\d{2}/\d{2}/\d{2})\s*through\s*(\d{2}/\d{2}/\d{2})", first_page
    )
    if period_match:
        try:
            end_date = datetime.strptime(period_match.group(2), "%m/%d/%y")
            month = end_date.strftime("%Y-%m")
        except ValueError:
            month = datetime.now().strftime("%Y-%m")
    else:
        month = datetime.now().strftime("%Y-%m")

    # Beginning and ending balance
    beg_match = re.search(r"BeginningBalance\s+\$?([\d,]+\.\d{2})", first_page)
    end_match = re.search(r"EndingBalance\s+\$?([\d,]+\.\d{2})", first_page)
    beginning_balance = clean_amount_unsigned(beg_match.group(1)) if beg_match else 0
    ending_balance = clean_amount_unsigned(end_match.group(1)) if end_match else 0

    # Parse transactions (scan all pages — transactions can overflow page 1)
    transactions = _parse_transactions(full_text)

    # Compute totals from transactions
    contributions = sum(t["amount"] for t in transactions if t["category"] == "Contribution")
    claims = sum(abs(t["amount"]) for t in transactions if t["category"] == "Healthcare")
    interest = sum(t["amount"] for t in transactions if t["category"] == "Interest")
    fees = sum(abs(t["amount"]) for t in transactions if t["category"] == "Fee")

    # Parse investment portfolio from page 2+
    investments = _parse_investments(pages_text)
    investment_value = sum(inv["value"] for inv in investments)

    # Investment returns
    period_return = 0
    ytd_return = 0
    ret_match = re.search(r"StatementPeriod:\s*([\d.]+)%", full_text)
    if ret_match:
        period_return = float(ret_match.group(1))
    ytd_match = re.search(r"YearToDate:\s*([\d.]+)%", full_text)
    if ytd_match:
        ytd_return = float(ytd_match.group(1))

    return {
        "type": "hsa",
        "account_number": account_number,
        "month": month,
        "beginning_balance": beginning_balance,
        "ending_balance": ending_balance,
        "cash_balance": ending_balance,
        "investment_value": investment_value,
        "total_value": ending_balance + investment_value,
        "contributions": contributions,
        "claims": claims,
        "interest": interest,
        "fees": fees,
        "period_return": period_return,
        "ytd_return": ytd_return,
        "transactions": transactions,
        "investments": investments,
    }


def _parse_transactions(text):
    """Extract transactions from the HealthEquity statement."""
    transactions = []
    lines = text.split("\n")

    for i, line in enumerate(lines):
        line = line.strip()

        # Must start with MM/DD/YYYY
        match = re.match(r"(\d{2}/\d{2}/\d{4})\s+(.+)", line)
        if not match:
            continue

        date_str = match.group(1)
        rest = match.group(2)

        try:
            txn_date = datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue

        # Find amounts: parenthesized = withdrawal, plain = deposit
        # Pattern: description (amount) running_balance  OR  description amount running_balance
        # The running balance is always the last number on the line.

        # Find all number patterns: plain numbers and parenthesized numbers
        all_nums = re.findall(r"\([\d,]+\.\d{2}\)|[\d,]+\.\d{2}", rest)
        if not all_nums:
            continue

        # Running balance is the last number (always plain, no parens)
        # Transaction amount is the second-to-last (or the first if only 2 numbers)
        if len(all_nums) < 2:
            continue

        raw_amount = all_nums[-2]
        is_withdrawal = raw_amount.startswith("(")
        amount_val = clean_amount_unsigned(raw_amount.replace("(", "").replace(")", ""))

        if is_withdrawal:
            amount = -amount_val
        else:
            amount = amount_val

        # Extract description: everything before the amount
        amt_pos = rest.find(raw_amount)
        description = rest[:amt_pos].strip() if amt_pos > 0 else rest.strip()

        # Categorize
        if "Contribution" in description or "EmployeeContribution" in description:
            category = "Contribution"
        elif "Card:" in description:
            category = "Healthcare"
        elif "Interest" in description:
            category = "Interest"
        elif "AdminFee" in description or "Fee" in description:
            category = "Fee"
        else:
            category = categorize(description)

        # Clean up concatenated description text
        description = _clean_description(description)

        transactions.append({
            "date": txn_date,
            "description": description,
            "amount": amount,
            "category": category,
        })

    return transactions


def _parse_investments(pages_text):
    """Extract investment holdings from the statement."""
    investments = []
    in_portfolio = False

    for page_text in pages_text:
        lines = page_text.split("\n")
        for line in lines:
            line = line.strip()

            if "InvestmentPortfolio" in line:
                in_portfolio = True
                continue

            if "ClosingAccountValue" in line:
                in_portfolio = False
                continue

            if not in_portfolio:
                continue

            # Skip header lines
            if "Fund" in line and "Category" in line:
                continue

            # Look for lines with: SYMBOL shares price value
            # e.g., "VIMAX 4.19 379.17 1,587.96"
            inv_match = re.match(
                r"([A-Z]{2,6})\s+([\d.]+)\s+([\d,.]+)\s+([\d,.]+)", line
            )
            if inv_match:
                investments.append({
                    "symbol": inv_match.group(1),
                    "shares": float(inv_match.group(2)),
                    "price": clean_amount_unsigned(inv_match.group(3)),
                    "value": clean_amount_unsigned(inv_match.group(4)),
                })

    return investments


def _clean_description(desc):
    """Insert spaces into concatenated text from PDF extraction."""
    # Add space before capital letters that follow lowercase
    desc = re.sub(r"([a-z])([A-Z])", r"\1 \2", desc)
    # Add space before numbers that follow letters
    desc = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", desc)
    # Clean Card: prefix
    desc = re.sub(r"Card:\s*", "", desc)
    # Collapse multiple spaces
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc


