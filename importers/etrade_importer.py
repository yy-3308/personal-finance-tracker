"""Parse E*Trade / Morgan Stanley at Work PDF statements for RSU, ESPP, and holdings."""

import re
from datetime import datetime

import pdfplumber

from importers.parse_utils import clean_amount


def is_etrade_pdf(filepath):
    """Check if a PDF is an E*Trade / Morgan Stanley at Work statement."""
    try:
        with pdfplumber.open(filepath) as pdf:
            first_page = pdf.pages[0].extract_text() or ""
        return "CLIENT STATEMENT" in first_page and "Morgan Stanley" in first_page
    except (FileNotFoundError, PermissionError):
        raise
    except Exception:
        return False


def parse_etrade_pdf(filepath):
    """Parse E*Trade PDF statement. Returns account info, holdings, RSU grants, and activity."""
    with pdfplumber.open(filepath) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Statement period — PDF may concatenate month+day (e.g. "February28")
    period_match = re.search(
        r"For the Period\s+\w+\s*\d{1,2}\s*-\s*(\w+?)(\d{1,2}),?\s*(\d{4})",
        full_text,
    )
    if period_match:
        end_month_name = period_match.group(1).strip()
        end_day = int(period_match.group(2))
        year = int(period_match.group(3))
        try:
            end_date = datetime.strptime(f"{end_month_name} {end_day}, {year}", "%B %d, %Y")
        except ValueError:
            end_date = datetime.strptime(f"{end_month_name} {end_day}, {year}", "%b %d, %Y")
        month = end_date.strftime("%Y-%m")
    else:
        month = datetime.now().strftime("%Y-%m")
        year = datetime.now().year

    # Account number
    acct_match = re.search(r"Account\s+(?:Summary\s+)?(\d{3}-\d{6}-\d{3})", full_text)
    account_number = acct_match.group(1) if acct_match else ""

    # Beginning and ending values
    beg_match = re.search(r"TOTAL BEGINNING VALUE\s+\$?([\d,]+\.\d{2})", full_text)
    beginning_value = clean_amount(beg_match.group(1)) if beg_match else 0

    end_match = re.search(r"TOTAL ENDING VALUE\s+\$?([\d,]+\.\d{2})", full_text)
    ending_value = clean_amount(end_match.group(1)) if end_match else 0

    # Cash balance
    cash_match = re.search(r"Cash,?\s*BDP,?\s*MMFs?\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})", full_text)
    if cash_match:
        cash_balance = clean_amount(cash_match.group(2))  # second is "this period"
    else:
        cash_balance = 0

    # Stock holdings
    holdings = _parse_holdings(full_text)

    # RSU grants
    rsu_grants = _parse_rsu_grants(full_text)

    # Stock plan summary
    plan_summary = _parse_stock_plan_summary(full_text)

    # Vesting activity (security transfers)
    vestings = _parse_vestings(full_text, year)

    # Interest income
    interest = _parse_interest(full_text, year)

    # Unrealized gain/loss
    unrealized_match = re.search(
        r"Unrealized.*?Inception to Date.*?Total Short-Term\s+.*?\$?\(?([\d,]+\.\d{2})\)?",
        full_text, re.DOTALL,
    )
    unrealized_gl = 0
    # Get it from the holdings instead
    for h in holdings:
        unrealized_gl += h.get("gain_loss", 0)

    return {
        "type": "etrade",
        "month": month,
        "account_number": account_number,
        "beginning_value": beginning_value,
        "ending_value": ending_value,
        "cash_balance": cash_balance,
        "equity_value": ending_value - cash_balance,
        "holdings": holdings,
        "rsu_grants": rsu_grants,
        "plan_summary": plan_summary,
        "vestings": vestings,
        "interest": interest,
        "unrealized_gl": unrealized_gl,
    }



def _parse_holdings(text):
    """Extract stock holdings from COMMON STOCKS section."""
    holdings = []

    # Pattern: SYMBOL (TICKER) qty share_price total_cost market_value gain_loss est_ann_income yield
    match = re.search(
        r"COMMON STOCKS(.*?)(?:Percentage|STOCKS\s+\d|TOTAL VALUE)",
        text, re.DOTALL,
    )
    if not match:
        return holdings

    section = match.group(1)
    # Pattern: Description (TICKER) qty $price $cost $market_value $(gain_loss) $income yield%
    holding_pat = re.compile(
        r"(.+?)\((\w+)\)\s+"
        r"([\d,.]+)\s+"           # quantity
        r"\$?([\d,.]+)\s+"        # share price
        r"\$?([\d,.]+)\s+"        # total cost
        r"\$?([\d,.]+)\s+"        # market value
        r"\$?\(?([\d,.]+)\)?\s*"  # gain/loss (may be in parens)
    )

    for m in holding_pat.finditer(section):
        description = m.group(1).strip()
        symbol = m.group(2)
        quantity = clean_amount(m.group(3))
        price = clean_amount(m.group(4))
        cost_basis = clean_amount(m.group(5))
        market_value = clean_amount(m.group(6))
        gl_str = m.group(7)

        # Check if gain/loss was in parentheses (negative)
        raw = section[m.start(7) - 2: m.end(7) + 1] if m.start(7) >= 2 else ""
        gain_loss = clean_amount(gl_str)
        if "(" in raw and ")" in raw:
            gain_loss = -abs(gain_loss)

        holdings.append({
            "symbol": symbol,
            "description": description,
            "quantity": quantity,
            "price": price,
            "cost_basis": cost_basis,
            "market_value": market_value,
            "gain_loss": gain_loss,
        })

    # If regex didn't match, try line-by-line
    if not holdings:
        lines = section.split("\n")
        for line in lines:
            line = line.strip()
            # Look for ticker in parens
            ticker_m = re.search(r"\((\w{1,5})\)", line)
            if not ticker_m:
                continue
            symbol = ticker_m.group(1)
            # Extract numbers after ticker
            after = line[ticker_m.end():]
            nums = re.findall(r"\$?\(?[\d,]+\.\d+\)?", after)
            if len(nums) >= 5:
                quantity = clean_amount(nums[0])
                price = clean_amount(nums[1])
                cost_basis = clean_amount(nums[2])
                market_value = clean_amount(nums[3])
                gl_raw = nums[4]
                gain_loss = clean_amount(gl_raw)
                if "(" in gl_raw:
                    gain_loss = -abs(gain_loss)

                holdings.append({
                    "symbol": symbol,
                    "description": line[:ticker_m.start()].strip(),
                    "quantity": quantity,
                    "price": price,
                    "cost_basis": cost_basis,
                    "market_value": market_value,
                    "gain_loss": gain_loss,
                })

    return holdings


def _parse_rsu_grants(text):
    """Extract RSU grant details from STOCK PLAN DETAILS."""
    grants = []

    # Find the stock plan details section
    section_match = re.search(
        r"STOCK PLAN DETAILS(.*?)(?:ACTIVITY|CASH FLOW|$)",
        text, re.DOTALL,
    )
    if not section_match:
        return grants

    section = section_match.group(1)

    # Pattern: MM/DD/YY grant_number type CUSIP/symbol quantity grant_price market_price value
    grant_pat = re.compile(
        r"(\d{2}/\d{2}/\d{2})\s+"   # grant date
        r"(\w+)\s+"                   # grant number
        r"(RSU|ESPP|SO|SAR)\s+"       # type
        r"(\w+)\s+"                   # CUSIP or symbol
        r"([\d,.]+)\s+"               # quantity
        r"\$?([\d,.]+)\s+"            # grant price
        r"\$?([\d,.]+)\s+"            # market price
        r"\$?([\d,.]+)"              # estimated value
    )

    for m in grant_pat.finditer(section):
        grant_date_str = m.group(1)
        # Parse MM/DD/YY
        try:
            grant_date = datetime.strptime(grant_date_str, "%m/%d/%y").strftime("%Y-%m-%d")
        except ValueError:
            grant_date = grant_date_str

        grants.append({
            "grant_date": grant_date,
            "grant_number": m.group(2),
            "type": m.group(3),
            "symbol": m.group(4),
            "quantity": clean_amount(m.group(5)),
            "grant_price": clean_amount(m.group(6)),
            "market_price": clean_amount(m.group(7)),
            "estimated_value": clean_amount(m.group(8)),
        })

    return grants


def _parse_stock_plan_summary(text):
    """Extract stock plan summary totals."""
    summary = {
        "exercisable_value": 0,
        "potential_value": 0,
        "total_value": 0,
    }

    # Pattern: "Restricted Stock — $72,471.84 $72,471.84 100.00"
    rs_match = re.search(
        r"Restricted Stock\s+[-—]+\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})",
        text,
    )
    if rs_match:
        summary["potential_value"] = clean_amount(rs_match.group(1))
        summary["total_value"] = clean_amount(rs_match.group(2))

    # Check for ESPP
    espp_match = re.search(
        r"ESPP\s+.*?\$?([\d,]+\.\d{2})",
        text,
    )
    if espp_match:
        summary["espp_value"] = clean_amount(espp_match.group(1))

    return summary


def _parse_vestings(text, year):
    """Extract vesting events (security transfers)."""
    vestings = []

    section_match = re.search(
        r"SECURITY TRANSFERS(.*?)(?:TOTAL SECURITY|MESSAGES|$)",
        text, re.DOTALL,
    )
    if not section_match:
        return vestings

    section = section_match.group(1)

    # Pattern: MM/DD Transfer into Account SECURITY_NAME qty $amount
    vest_pat = re.compile(
        r"(\d{1,2}/\d{1,2})\s+Transfer into Account\s+(.+?)\s+([\d,.]+)\s+\$?([\d,]+\.\d{2})"
    )

    for m in vest_pat.finditer(section):
        date_str = m.group(1)
        security = m.group(2).strip()
        quantity = clean_amount(m.group(3))
        amount = clean_amount(m.group(4))

        month_num, day_num = date_str.split("/")
        try:
            full_date = datetime(year, int(month_num), int(day_num)).strftime("%Y-%m-%d")
        except ValueError:
            continue

        vestings.append({
            "date": full_date,
            "security": security,
            "quantity": quantity,
            "amount": amount,
        })

    return vestings


def _parse_interest(text, year):
    """Extract interest income activity."""
    interest = []

    # Pattern: MM/DD Interest Income DESCRIPTION $amount
    int_pat = re.compile(
        r"(\d{1,2}/\d{1,2})\s+Interest Income\s+(.+?)\s+\$?([\d,]+\.\d{2})"
    )

    for m in int_pat.finditer(text):
        date_str = m.group(1)
        description = m.group(2).strip()
        amount = clean_amount(m.group(3))

        month_num, day_num = date_str.split("/")
        try:
            full_date = datetime(year, int(month_num), int(day_num)).strftime("%Y-%m-%d")
        except ValueError:
            continue

        interest.append({
            "date": full_date,
            "description": description,
            "amount": amount,
        })

    return interest
