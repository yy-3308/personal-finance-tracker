# Personal Finance Tracker

A privacy-first personal finance dashboard that runs entirely on your local machine. Connects to banks automatically via Plaid, and imports PDF/XLSX statements for institutions Plaid doesn't support.

## Why I Built This

Every personal finance app I tried had the same problems:

- **They're not built for me.** Mint was bloated. YNAB forces a budgeting philosophy I don't follow. Most apps lack support for equity compensation, HSA investments, or multi-institution imports in the formats I actually get.
- **They cost too much for what they do.** Mint shut down. YNAB is $100/year. Copilot is $70/year. I just want to see my spending and net worth — I shouldn't need a subscription for that.

So I built my own. It runs locally, syncs from banks automatically where possible, and does exactly what I need — nothing more.

## Features

- **Plaid integration** — connect Chase, Citi, Wells Fargo and others for automatic transaction, balance, and investment sync
- **Multi-institution PDF/XLSX import** — Amex, Fidelity, E\*Trade, CrossCountry Mortgage, HealthEquity HSA
- **Automatic categorization** — keyword-based rules with customizable overrides
- **Spending analysis** — category breakdown pie chart (clickable to filter), month-over-month comparison
- **Income tracking** — salary, dividends, interest, realized gains
- **Investment portfolio** — holdings, unrealized gains, buy/sell activity, portfolio value over time
- **Equity compensation** — RSU grants, vesting events, vested holdings
- **Healthcare (HSA)** — balances, contributions, claims, investment holdings
- **Housing** — mortgage details, payment breakdown, escrow tracking
- **Overview dashboard** — net worth, cash in hand, investments, monthly income vs spending
- **Sortable tables** — click any column header to sort
- **Global month picker** — navigate all tabs by month

## Tech Stack

- **Backend:** Flask, SQLAlchemy, SQLite
- **Frontend:** Jinja2 templates, Chart.js, vanilla JavaScript
- **Bank sync:** Plaid API (transactions, balances, investments)
- **PDF parsing:** pdfplumber
- **XLSX parsing:** openpyxl

## Quick Start

```bash
git clone https://github.com/yy-3308/personal-finance-tracker.git
cd personal-finance-tracker
./start.sh
```

That's it. The script checks for Python, sets up everything automatically, and opens the app in your browser at http://localhost:5002.

<details>
<summary>Manual setup (if you prefer)</summary>

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/Downloads/spend_tracker
python app.py
```

</details>

## Plaid Setup (optional)

To enable automatic bank syncing, add a `.env` file in the project root:

```
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_secret
PLAID_ENV=production
```

Get credentials at [dashboard.plaid.com](https://dashboard.plaid.com). Without `.env`, the app works fine using PDF/CSV imports only.

## Usage

### Automatic sync (Plaid)
1. Go to the **Import** tab → click **+ Connect a Bank**
2. Link your bank through Plaid Link
3. Click **Sync Transactions**, **Sync Balances**, or **Sync Investments** to pull fresh data

### Manual import (PDF/XLSX)
1. Drop statements into `~/Downloads/spend_tracker/`
2. Go to the **Import** tab → click **Import All**
3. Processed files move to `~/Downloads/spend_tracker/processed/`

## Supported Institutions

| Institution | Method | What's Imported |
|---|---|---|
| Chase | Plaid | Transactions, balances, investments |
| Citi | Plaid | Transactions, balances |
| Wells Fargo | Plaid | Transactions, balances |
| Amex | XLSX import | Credit card transactions |
| Fidelity | PDF import | Brokerage holdings, activity, dividends |
| E\*Trade | PDF import | Stock plan grants, vesting, holdings |
| CrossCountry Mortgage | PDF import | Loan details, payment breakdown |
| HealthEquity | PDF import | HSA balances, transactions, investments |

## Project Structure

```
app.py                          # Flask routes and API endpoints
models.py                       # SQLAlchemy models
database.py                     # DB engine setup
categorizer.py                  # Transaction categorization rules
config.py                       # Paths and configuration
importers/
  plaid_client.py               # Plaid API client
  plaid_importer.py             # Plaid sync logic (transactions, balances, investments)
  importer.py                   # Generic CSV import
  amex_importer.py              # Amex XLSX parser
  wellsfargo_importer.py        # Wells Fargo PDF/CSV parser
  fidelity_importer.py          # Fidelity PDF parser
  etrade_importer.py            # E*Trade PDF parser
  mortgage_importer.py          # Mortgage PDF parser
  hsa_importer.py               # HealthEquity HSA PDF parser
  parse_utils.py                # Shared parsing utilities
templates/                      # Jinja2 HTML templates
data/                           # SQLite database (gitignored)
```

## Privacy

All data stays local. The database (`data/finance.db`), imported files, and `.env` credentials are gitignored. The source code contains no personal information.
