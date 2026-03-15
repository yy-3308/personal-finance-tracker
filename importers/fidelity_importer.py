"""Parse Fidelity CSV and PDF statements into account balances, holdings, and activity."""

import csv
import re
from datetime import datetime

import pdfplumber

from importers.parse_utils import clean_amount


# ── CSV Parsing ──────────────────────────────────────────────────────────────

def parse_fidelity_statement(filepath):
    """Parse Fidelity CSV with account summaries and stock holdings."""
    accounts = []
    holdings = []

    with open(filepath, "r") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        return {"accounts": [], "holdings": []}

    header = [h.strip() for h in rows[0]]
    ending_val_idx = None
    account_idx = None
    account_type_idx = None

    for i, h in enumerate(header):
        if "Ending mkt Value" in h or "Ending Net Value" in h:
            if ending_val_idx is None:
                ending_val_idx = i
        if h == "Account":
            account_idx = i
        if h == "Account Type":
            account_type_idx = i

    if ending_val_idx is None:
        for i, h in enumerate(header):
            if "ending" in h.lower() and "value" in h.lower():
                ending_val_idx = i
                break

    for row in rows[1:]:
        if not row or all(c.strip() == "" for c in row):
            break
        if len(row) <= max(account_idx or 0, ending_val_idx or 0):
            continue
        acct_type = row[account_type_idx].strip() if account_type_idx is not None else ""
        acct_num = row[account_idx].strip() if account_idx is not None else ""
        try:
            ending_val = float(row[ending_val_idx].replace(",", "").strip())
        except (ValueError, IndexError):
            ending_val = 0
        if acct_num:
            accounts.append({
                "account_type": acct_type,
                "account_number": acct_num,
                "ending_value": ending_val,
            })

    holdings_start = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Symbol/CUSIP":
            holdings_start = i
            break

    if holdings_start is not None:
        current_account = None
        for row in rows[holdings_start + 1:]:
            if not row or all(c.strip() == "" for c in row):
                continue
            first = row[0].strip()
            if first.startswith("Z") and len(first) > 5 and len(row) == 1 or (len(row) > 1 and row[1].strip() == ""):
                current_account = first
                continue
            if first in ("Stocks", "Core Account", "Bonds", "Mutual Funds") or first.startswith("Subtotal"):
                continue
            if len(row) >= 7 and current_account:
                symbol = first
                description = row[1].strip() if len(row) > 1 else ""
                try:
                    quantity = float(row[2].replace(",", "").strip())
                    price = float(row[3].replace(",", "").strip())
                    beg_str = row[4].replace(",", "").strip()
                    beginning_value = float(beg_str) if beg_str not in ("unavailable", "") else 0
                    end_str = row[5].replace(",", "").strip()
                    ending_value = float(end_str) if end_str != "unavailable" else quantity * price
                    cb_str = row[6].replace(",", "").strip()
                    cost_basis = float(cb_str) if cb_str not in ("unavailable", "not applicable", "") else 0
                except (ValueError, IndexError):
                    continue
                holdings.append({
                    "account": current_account,
                    "symbol": symbol, "description": description,
                    "quantity": quantity, "price": price,
                    "beginning_value": beginning_value, "ending_value": ending_value,
                    "cost_basis": cost_basis,
                    "gain_loss": round(ending_value - cost_basis, 2) if cost_basis > 0 else 0,
                })

    return {"accounts": accounts, "holdings": holdings}


# ── PDF Parsing ──────────────────────────────────────────────────────────────


def parse_fidelity_pdf(filepath):
    """Parse Fidelity investment report PDF. Returns holdings, activity, dividends, realized gains."""
    with pdfplumber.open(filepath) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Statement period
    period_match = re.search(r"(\w+ \d{1,2}, \d{4})\s*-\s*(\w+ \d{1,2}, \d{4})", full_text)
    if period_match:
        start_date = datetime.strptime(period_match.group(1), "%B %d, %Y")
        end_date = datetime.strptime(period_match.group(2), "%B %d, %Y")
        month = end_date.strftime("%Y-%m")
        year = end_date.year
    else:
        month = datetime.now().strftime("%Y-%m")
        year = datetime.now().year
        start_date = end_date = None

    # Portfolio summary
    pv_match = re.search(r"Your Portfolio Value:\s*\$?([\d,]+\.\d{2})", full_text)
    portfolio_value = clean_amount(pv_match.group(1)) if pv_match else 0

    pc_match = re.search(r"Portfolio Change from Last Period:\s*\$?([-\d,]+\.\d{2})", full_text)
    portfolio_change = clean_amount(pc_match.group(1)) if pc_match else 0

    # Account numbers
    account_numbers = re.findall(r"Account #\s*(Z[\d-]+)", full_text)

    # Parse sections
    holdings = _parse_pdf_holdings(full_text)
    activities = _parse_pdf_activities(full_text, year)
    dividends = _parse_pdf_dividends(full_text, year)
    realized = _parse_pdf_realized_gains(full_text)

    # Map activity symbols using holdings ticker lookup
    _annotate_activity_tickers(activities, holdings)
    _annotate_activity_tickers(dividends, holdings)

    return {
        "type": "fidelity_investment",
        "month": month,
        "portfolio_value": portfolio_value,
        "portfolio_change": portfolio_change,
        "account_numbers": account_numbers,
        "holdings": holdings,
        "activities": activities,
        "dividends": dividends,
        "realized_gains": realized,
    }


def _parse_pdf_holdings(text):
    """Extract holdings from PDF text using qty (3 dec) + price (4 dec) anchor."""
    holdings = []
    lines = text.split("\n")

    # Only process lines within Holdings sections (between "Holdings" and "Activity")
    in_holdings = False

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()

        # Track when we're in a holdings section
        if line == "Holdings" or line.startswith("Holdings"):
            in_holdings = True
            continue
        if line.startswith("Activity") or line.startswith("Additional Information"):
            in_holdings = False
            continue
        if not in_holdings:
            continue

        # Skip headers and totals
        if not line or "Description" in line or "Market Value" in line or "Beginning" in line:
            continue
        if line.startswith("Total ") or line.startswith("--") or "% of account" in line:
            continue
        if "Includes exchange-traded" in line:
            continue

        # Anchor: quantity (3 decimal places) followed by price (4 decimal places)
        anchor = re.search(r"([\d,]+\.\d{3})\s+\$?([\d,]+\.\d{4})", line)
        if not anchor:
            continue

        qty = clean_amount(anchor.group(1))
        price = clean_amount(anchor.group(2))

        # Before the anchor: description + beginning_value
        before = line[:anchor.start()].strip()

        # After the anchor: ending_value, cost_basis, gain_loss, eai
        after = line[anchor.end():].strip()

        # Extract beginning value (last number before qty)
        beg_match = re.search(r"(\$?[\d,]+\.\d{2}|unavailable)\s*$", before)
        if beg_match:
            description = before[:beg_match.start()].strip()
            beg_str = beg_match.group(1)
            beginning_value = 0 if beg_str == "unavailable" else clean_amount(beg_str)
        else:
            description = before
            beginning_value = 0

        # Extract after-anchor numbers
        if "not applicable" in after:
            end_match = re.search(r"\$?([\d,]+\.\d{2})", after)
            ending_value = clean_amount(end_match.group(1)) if end_match else qty * price
            cost_basis = 0
            gain_loss = 0
        else:
            after_nums = re.findall(r"-?\$?[\d,]+\.\d{2}", after)
            ending_value = clean_amount(after_nums[0]) if len(after_nums) >= 1 else qty * price
            cost_basis = clean_amount(after_nums[1]) if len(after_nums) >= 2 else 0
            gain_loss = clean_amount(after_nums[2]) if len(after_nums) >= 3 else 0

        # Remove M prefix (margin indicator)
        if description and description[0] == "M" and len(description) > 1 and description[1].isupper():
            description = description[1:]

        # Collect continuation lines for rest of description (especially ticker)
        j = i + 1
        while j < len(lines):
            next_line = lines[j].strip()
            if not next_line:
                break
            # Stop at new data lines, totals, section boundaries
            if re.search(r"[\d,]+\.\d{3}\s+\$?[\d,]+\.\d{4}", next_line):
                break
            if next_line.startswith("Total ") or next_line.startswith("--"):
                break
            if next_line in ("Core Account", "Exchange Traded Products", "Stocks",
                             "Common Stock", "Equity ETPs", "Other ETPs", "Activity",
                             "Holdings", "Additional Information"):
                break
            if "Includes exchange-traded" in next_line:
                break
            # Skip pure yield lines
            if re.match(r"^[\d.]+%?$", next_line):
                j += 1
                continue
            # Clean trailing yield number from continuation
            cont = re.sub(r"\s+[\d.]+$", "", next_line).strip()
            if cont:
                description += " " + cont
            j += 1

        # Extract ticker from parentheses
        ticker_match = re.search(r"\(([A-Z][A-Z0-9.]*)\)", description)
        ticker = ticker_match.group(1) if ticker_match else ""

        # Clean description
        clean_desc = description
        if ticker:
            clean_desc = re.sub(r"\s*\([A-Z][A-Z0-9.]*\)\s*", " ", clean_desc).strip()
        clean_desc = re.sub(r"\s+[\d.]+%?\s*$", "", clean_desc).strip()

        if not ticker:
            words = clean_desc.split()
            ticker = words[0] if words else "UNKNOWN"

        holdings.append({
            "symbol": ticker,
            "description": clean_desc,
            "quantity": qty,
            "price": price,
            "beginning_value": beginning_value,
            "ending_value": ending_value,
            "cost_basis": cost_basis,
            "gain_loss": gain_loss,
        })

    # Consolidate multiple lots of same symbol
    consolidated = {}
    for h in holdings:
        sym = h["symbol"]
        if sym in consolidated:
            c = consolidated[sym]
            c["quantity"] += h["quantity"]
            c["beginning_value"] += h["beginning_value"]
            c["ending_value"] += h["ending_value"]
            c["cost_basis"] += h["cost_basis"]
            c["gain_loss"] += h["gain_loss"]
            c["price"] = c["ending_value"] / c["quantity"] if c["quantity"] else 0
        else:
            consolidated[sym] = dict(h)

    return list(consolidated.values())


def _parse_pdf_activities(text, year):
    """Extract buy/sell activity from PDF."""
    activities = []

    # Find "Securities Bought & Sold" sections
    sections = re.findall(
        r"Securities Bought & Sold(.*?)(?=Dividends, Interest|Deposits|Core Fund|Total Securities|Net Securities|$)",
        text, re.DOTALL,
    )
    if not sections:
        # Try broader match
        sections = re.findall(r"Securities Bought & Sold(.*?)(?=Total Net|$)", text, re.DOTALL)

    for section in sections:
        lines = section.split("\n")
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            # Pattern: MM/DD name CUSIP "You Bought/Sold" qty price ... amount
            match = re.match(
                r"(\d{2}/\d{2})\s+(.+?)\s+(\w{8,9})\s+You (Bought|Sold)\s+(-?[\d,.]+)\s+\$?([\d,.]+)",
                line,
            )
            if not match:
                continue

            date_str = match.group(1)
            name = match.group(2).strip()
            action = match.group(4).lower()
            qty = abs(clean_amount(match.group(5)))
            price_per = clean_amount(match.group(6))

            # Extract the last number on the line as transaction amount
            all_nums = re.findall(r"-?\$?[\d,]+\.\d{2}", line[match.end():])
            amount = clean_amount(all_nums[-1]) if all_nums else qty * price_per

            # For buys, amount is negative (cost); for sells, positive (proceeds)
            if action == "bought":
                amount = -abs(amount)
            else:
                amount = abs(amount)

            # Check continuation lines for realized gain info
            realized_gain = 0
            if action == "sold" and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                gain_match = re.search(r"(?:Short|Long)-term gain:\s*\$?([\d,]+\.\d{2})", next_line)
                if gain_match:
                    realized_gain = clean_amount(gain_match.group(1))
                loss_match = re.search(r"(?:Short|Long)-term loss:\s*-?\$?([\d,]+\.\d{2})", next_line)
                if loss_match:
                    realized_gain = -clean_amount(loss_match.group(1))

            # Build full date
            month_num = int(date_str[:2])
            day_num = int(date_str[3:])
            try:
                full_date = datetime(year, month_num, day_num).strftime("%Y-%m-%d")
            except ValueError:
                continue

            # Collect description continuations
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                # If next line doesn't start with a date or keyword, it's part of the name
                if next_line and not re.match(r"\d{2}/\d{2}\s", next_line) and "gain:" not in next_line.lower() and "loss:" not in next_line.lower() and "Total" not in next_line:
                    name += " " + next_line.split("Short-term")[0].split("Long-term")[0].strip()

            activities.append({
                "date": full_date,
                "name": name.strip(),
                "symbol": "",  # will be annotated later
                "action": action,
                "quantity": qty,
                "price": price_per,
                "amount": amount,
                "realized_gain": realized_gain,
            })

    return activities


def _parse_pdf_dividends(text, year):
    """Extract dividends from PDF."""
    dividends = []

    sections = re.findall(
        r"Dividends, Interest & Other Income(.*?)(?=Deposits|Core Fund|Total Dividends|$)",
        text, re.DOTALL,
    )

    for section in sections:
        lines = section.split("\n")
        for line in lines:
            line = line.strip()
            # Pattern: MM/DD name CUSIP "Dividend Received" ... amount
            match = re.match(
                r"(\d{2}/\d{2})\s+(.+?)\s+(\w{8,9})\s+(Dividend Received|Interest Earned).*?\$?([\d,]+\.\d{2})$",
                line,
            )
            if not match:
                continue

            date_str = match.group(1)
            name = match.group(2).strip()
            amount = clean_amount(match.group(5))

            month_num = int(date_str[:2])
            day_num = int(date_str[3:])
            try:
                full_date = datetime(year, month_num, day_num).strftime("%Y-%m-%d")
            except ValueError:
                continue

            dividends.append({
                "date": full_date,
                "name": name,
                "symbol": "",
                "action": "dividend",
                "quantity": 0,
                "price": 0,
                "amount": amount,
                "realized_gain": 0,
            })

    return dividends


def _parse_pdf_realized_gains(text):
    """Extract realized gains summary from PDF."""
    result = {"short_term": 0, "long_term": 0, "total": 0}

    # "Net Short-term Gain/Loss 459.08 783.05"  (this period, ytd)
    st_match = re.search(r"Net Short-term Gain/Loss\s+(-?[\d,]+\.\d{2})", text)
    if st_match:
        result["short_term"] = clean_amount(st_match.group(1))

    lt_match = re.search(r"Net Long-term Gain/Loss\s+(-?[\d,]+\.\d{2})", text)
    if lt_match:
        result["long_term"] = clean_amount(lt_match.group(1))

    net_match = re.search(r"Net Gain/Loss\s+\$?([\d,]+\.\d{2})", text)
    if net_match:
        result["total"] = clean_amount(net_match.group(1))
    else:
        result["total"] = result["short_term"] + result["long_term"]

    # Also get YTD if available
    st_ytd = re.search(r"Net Short-term Gain/Loss\s+[\d,]+\.\d{2}\s+([\d,]+\.\d{2})", text)
    if st_ytd:
        result["short_term_ytd"] = clean_amount(st_ytd.group(1))

    return result


def _annotate_activity_tickers(activities, holdings):
    """Match activity names to holdings to get ticker symbols."""
    # Build a lookup: first N words of description -> ticker
    desc_to_ticker = {}
    for h in holdings:
        desc_upper = h["description"].upper()
        # Use first 2 words as key
        words = desc_upper.split()[:2]
        key = " ".join(words)
        desc_to_ticker[key] = h["symbol"]

    for act in activities:
        name_upper = act["name"].upper()
        # Try matching first 2, then first 1 word
        words = name_upper.split()
        for n in (3, 2, 1):
            key = " ".join(words[:n])
            if key in desc_to_ticker:
                act["symbol"] = desc_to_ticker[key]
                break
        if not act["symbol"]:
            # Fallback: use first word of name
            act["symbol"] = words[0] if words else "UNKNOWN"


def is_fidelity_pdf(filepath):
    """Check if a PDF is a Fidelity investment report."""
    try:
        with pdfplumber.open(filepath) as pdf:
            first_page = pdf.pages[0].extract_text() or ""
        return "INVESTMENT REPORT" in first_page and "Fidelity" in first_page
    except (FileNotFoundError, PermissionError):
        raise
    except Exception:
        return False
