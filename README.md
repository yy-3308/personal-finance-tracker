# Personal Finance Tracker

A privacy-first personal finance dashboard that runs entirely on your local machine. No cloud services, no third-party APIs — your financial data never leaves your computer.

## Why I Built This

Every personal finance app I tried had the same problems:

- **They're not built for me.** Mint was bloated. YNAB forces a budgeting philosophy I don't follow. Most apps lack support for equity compensation, HSA investments, or multi-institution imports in the formats I actually get.
- **They want all my data.** Linking bank credentials through Plaid or similar services means a third party has access to my full transaction history. One breach and it's all exposed.
- **They cost too much for what they do.** Mint shut down. YNAB is $100/year. Copilot is $70/year. I just want to see my spending and net worth — I shouldn't need a subscription for that.

So I built my own. It runs locally, reads the statements I already download from my banks, and does exactly what I need — nothing more.

## Features

- **Multi-institution import** — Chase, Citi, Amex, Wells Fargo, Fidelity, E\*Trade, CrossCountry Mortgage, HealthEquity HSA
- **Automatic categorization** — keyword-based rules with customizable overrides
- **Spending analysis** — category breakdown pie chart (clickable to filter), month-over-month comparison
- **Income tracking** — salary, dividends, interest, realized gains
- **Investment portfolio** — holdings, unrealized gains, buy/sell activity, portfolio value over time
- **Equity compensation** — RSU grants, vesting events, vested holdings
- **Healthcare (HSA)** — balances, contributions, claims, investment holdings
- **Housing** — mortgage details, payment breakdown, escrow tracking
- **Overview dashboard** — cash in hand, investments, monthly income vs spending
- **Sortable tables** — click any column header to sort
- **Global month picker** — navigate all tabs by month

## Tech Stack

- **Backend:** Flask, SQLAlchemy, SQLite
- **Frontend:** Jinja2 templates, Chart.js, vanilla JavaScript
- **PDF parsing:** pdfplumber
- **XLSX parsing:** openpyxl

## Quick Start

```bash
git clone https://github.com/yy-3308/personal-finance-tracker.git
cd personal-finance-tracker
./start.sh
```

That's it. The script checks for Python, sets up everything automatically, and opens the app in your browser.

<details>
<summary>Manual setup (if you prefer)</summary>

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install openpyxl  # for Amex XLSX imports

# Create import folder
mkdir -p ~/Downloads/spend_tracker

# Run
python app.py
```

Open http://localhost:5001 in your browser.
</details>

## Usage

1. Drop bank/brokerage statements (PDF, CSV, XLSX) into `~/Downloads/spend_tracker/`
2. Go to the **Import** tab and click scan — files are auto-detected by institution
3. Imported files are moved to `~/Downloads/spend_tracker/processed/`
4. Browse your data across the dashboard tabs

## Supported File Formats

| Institution | Format | What's Imported |
|---|---|---|
| Chase | PDF | Checking/credit card transactions, balances |
| Citi | PDF | Credit card & savings transactions, balances |
| Amex | XLSX | Credit card transactions |
| Wells Fargo | PDF, CSV | Credit card transactions, balances |
| Fidelity | PDF | Brokerage holdings, activity, dividends |
| E\*Trade | PDF | Stock plan grants, vesting, holdings |
| CrossCountry Mortgage | PDF | Loan details, payment breakdown |
| HealthEquity | PDF | HSA balances, transactions, investments |

## Project Structure

```
app.py                  # Flask routes and API endpoints
models.py               # SQLAlchemy models
database.py             # DB engine setup
categorizer.py          # Transaction categorization rules
config.py               # Paths and configuration
importer.py             # Generic CSV import
pdf_importer.py         # Chase PDF parser
citi_importer.py        # Citi PDF parser
amex_importer.py        # Amex XLSX parser
wellsfargo_importer.py  # Wells Fargo PDF/CSV parser
fidelity_importer.py    # Fidelity PDF parser
etrade_importer.py      # E*Trade PDF parser
mortgage_importer.py    # CrossCountry Mortgage PDF parser
hsa_importer.py         # HealthEquity HSA PDF parser
templates/              # Jinja2 HTML templates
data/                   # SQLite database (gitignored)
```

## Privacy

All data stays local. The database (`data/finance.db`) and imported files are gitignored. The source code contains no personal information.
