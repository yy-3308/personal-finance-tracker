import json
import os
from collections import defaultdict
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from config import Config
from database import get_session, init_db
from importers.amex_importer import is_amex_xlsx, parse_amex_xlsx
from importers.wellsfargo_importer import (
    is_wellsfargo_csv, is_wellsfargo_pdf, parse_wellsfargo_csv, parse_wellsfargo_pdf,
)
from importers.etrade_importer import is_etrade_pdf, parse_etrade_pdf
from importers.hsa_importer import is_hsa_pdf, parse_hsa_pdf
from importers.mortgage_importer import is_mortgage_pdf, parse_mortgage_pdf
from importers.fidelity_importer import is_fidelity_pdf, parse_fidelity_pdf, parse_fidelity_statement
from importers.importer import (
    detect_csv_format,
    import_file,
    move_to_processed,
    scan_import_folder,
)
from categorizer import categorize, get_all_categories
from models import (
    Account, Balance, CategoryRule, CsvProfile, Holding, HsaSummary,
    InvestmentActivity, Mortgage, PlaidItem, StockPlanGrant, Transaction, VestingEvent,
)
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
from importers.plaid_client import get_plaid_client
from importers.plaid_importer import (
    create_link_token, exchange_public_token,
    get_institution_name, sync_transactions,
    sync_balances, sync_holdings, sync_investment_transactions,
    create_relink_token,
)


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    os.makedirs(app.config["IMPORT_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PROCESSED_FOLDER"], exist_ok=True)

    init_db(app.config["DB_PATH"])

    def get_db():
        return get_session(app.config["DB_PATH"])

    # ── Overview ──────────────────────────────────────────────────────
    @app.route("/")
    def index():
        session = get_db()
        accounts = session.query(Account).filter(Account.account_type != "mortgage").all()
        # Get latest balance per account
        account_balances = []
        for acct in accounts:
            latest = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month.desc())
                .first()
            )
            account_balances.append({
                "name": acct.name, "type": acct.account_type,
                "institution": acct.institution,
                "balance": latest.balance if latest else 0,
                "month": latest.month if latest else "N/A",
            })
        net_worth = sum(ab["balance"] for ab in account_balances)
        session.close()
        return render_template("index.html", accounts=account_balances, net_worth=net_worth)

    @app.route("/api/overview")
    def api_overview():
        session = get_db()
        # Net worth over time (exclude mortgage)
        mortgage_ids = {a.id for a in session.query(Account).filter(Account.account_type == "mortgage").all()}
        months = sorted(set(m for (m,) in session.query(Balance.month).distinct()))
        net_worth_history = []
        for month in months:
            total = sum(
                b.balance for b in session.query(Balance).filter(Balance.month == month).all()
                if b.account_id not in mortgage_ids
            )
            net_worth_history.append({"month": month, "total": total})

        # Cash vs stocks from latest balances
        cash_types = {"checking", "savings", "hsa"}
        stock_types = {"brokerage"}
        accounts = session.query(Account).all()
        cash_total = 0
        stocks_total = 0
        for acct in accounts:
            latest = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month.desc())
                .first()
            )
            if not latest:
                continue
            if acct.account_type in cash_types:
                cash_total += latest.balance
            elif acct.account_type in stock_types:
                stocks_total += latest.balance

        # Monthly income vs spending (exclude Transfer, Income, Interest from spending)
        excluded = {"Transfer", "Income", "Interest"}
        all_txns = session.query(Transaction).all()
        from collections import defaultdict
        monthly = defaultdict(lambda: {"income": 0, "spending": 0})
        for t in all_txns:
            m = t.date[:7]
            if t.amount > 0 and t.category not in excluded:
                monthly[m]["income"] += t.amount
            elif t.amount < 0 and t.category not in excluded:
                monthly[m]["spending"] += t.amount
        monthly_gain_loss = [
            {"month": m, "income": round(d["income"], 2),
             "spending": round(abs(d["spending"]), 2),
             "net": round(d["income"] + d["spending"], 2)}
            for m, d in sorted(monthly.items())
        ]

        session.close()
        return jsonify({
            "net_worth_history": net_worth_history,
            "cash_total": round(cash_total, 2),
            "stocks_total": round(stocks_total, 2),
            "monthly_gain_loss": monthly_gain_loss,
        })

    # ── Accounts ──────────────────────────────────────────────────────
    @app.route("/accounts", methods=["GET", "POST"])
    def accounts():
        session = get_db()
        if request.method == "POST":
            account = Account(
                name=request.form["name"],
                account_type=request.form["account_type"],
                institution=request.form["institution"],
            )
            session.add(account)
            session.commit()
            flash(f"Account '{account.name}' created.", "success")
            session.close()
            return redirect(url_for("accounts"))

        all_accounts = session.query(Account).all()
        # Get last transaction date per account
        account_data = []
        for acct in all_accounts:
            last_txn = (
                session.query(Transaction)
                .filter(Transaction.account_id == acct.id)
                .order_by(Transaction.date.desc())
                .first()
            )
            account_data.append({
                "id": acct.id, "name": acct.name,
                "account_type": acct.account_type,
                "institution": acct.institution,
                "last_transaction": last_txn.date if last_txn else "No data",
                "txn_count": session.query(Transaction).filter(Transaction.account_id == acct.id).count(),
            })
        session.close()
        return render_template("accounts.html", accounts=account_data)

    # ── Import ────────────────────────────────────────────────────────
    @app.route("/import", methods=["GET"])
    def import_page():
        session = get_db()
        pending_files = _scan_all_files(app.config["IMPORT_FOLDER"])
        profiles = session.query(CsvProfile).all()
        accounts = session.query(Account).all()

        # Build download checklist: group accounts by institution with last update
        institutions = {}
        for acct in accounts:
            inst = acct.institution
            if inst not in institutions:
                institutions[inst] = {"name": inst, "accounts": [], "last_updated": None, "first_date": None}
            last_txn = (
                session.query(Transaction)
                .filter(Transaction.account_id == acct.id)
                .order_by(Transaction.date.desc())
                .first()
            )
            first_txn = (
                session.query(Transaction)
                .filter(Transaction.account_id == acct.id)
                .order_by(Transaction.date.asc())
                .first()
            )
            last_bal = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month.desc())
                .first()
            )
            first_bal = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month.asc())
                .first()
            )
            last_date = last_txn.date if last_txn else (last_bal.month if last_bal else None)
            first_date = first_txn.date if first_txn else (first_bal.month if first_bal else None)
            institutions[inst]["accounts"].append({
                "name": acct.name, "type": acct.account_type,
                "last_date": last_date or "Never", "first_date": first_date or "Never",
            })
            if last_date:
                if not institutions[inst]["last_updated"] or last_date > institutions[inst]["last_updated"]:
                    institutions[inst]["last_updated"] = last_date
            if first_date:
                if not institutions[inst]["first_date"] or first_date < institutions[inst]["first_date"]:
                    institutions[inst]["first_date"] = first_date

        checklist = sorted(institutions.values(), key=lambda x: x["last_updated"] or "0")

        session.close()
        return render_template(
            "import.html",
            pending_files=[os.path.basename(f) for f in pending_files],
            import_folder=app.config["IMPORT_FOLDER"],
            profiles=profiles,
            accounts=accounts,
            checklist=checklist,
        )

    def _scan_all_files(folder):
        """Scan for both CSV and PDF files."""
        files = []
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith((".csv", ".pdf", ".xlsx")) and os.path.isfile(os.path.join(folder, f)):
                files.append(os.path.join(folder, f))
        return files

    def _import_fidelity_csv(filepath, session):
        """Import a Fidelity CSV statement. Returns (num_imported, error_or_None)."""
        result = parse_fidelity_statement(filepath)
        month = datetime.now().strftime("%Y-%m")
        num_holdings = 0

        for acct_data in result["accounts"]:
            acct_name = f"Fidelity {acct_data['account_type']} ({acct_data['account_number'][-4:]})"
            account = session.query(Account).filter(Account.name == acct_name).first()
            if not account:
                account = Account(name=acct_name, account_type="brokerage", institution="Fidelity")
                session.add(account)
                session.commit()

            # Save balance snapshot
            existing_bal = (
                session.query(Balance)
                .filter(Balance.account_id == account.id, Balance.month == month)
                .first()
            )
            if existing_bal:
                existing_bal.balance = acct_data["ending_value"]
            else:
                session.add(Balance(month=month, account_id=account.id, balance=acct_data["ending_value"]))
            session.commit()

        # Save individual holdings
        # Build a map of account_number -> account for holdings
        acct_map = {}
        for acct_data in result["accounts"]:
            acct_name = f"Fidelity {acct_data['account_type']} ({acct_data['account_number'][-4:]})"
            acct_map[acct_data["account_number"]] = session.query(Account).filter(Account.name == acct_name).first()

        # Clear existing holdings for this month, then re-insert
        for acct in acct_map.values():
            if acct:
                session.query(Holding).filter(Holding.account_id == acct.id, Holding.month == month).delete()
        session.commit()

        for h in result["holdings"]:
            acct = acct_map.get(h["account"])
            if not acct:
                continue
            holding = Holding(
                month=month, account_id=acct.id, symbol=h["symbol"],
                description=h["description"], quantity=h["quantity"],
                price=h["price"], beginning_value=h.get("beginning_value", 0),
                ending_value=h["ending_value"], cost_basis=h["cost_basis"],
                gain_loss=h["gain_loss"],
            )
            session.add(holding)
        session.commit()

        num_holdings = len(result["holdings"])
        return num_holdings, None

    def _import_fidelity_pdf(filepath, session):
        """Import a Fidelity investment report PDF. Returns (num_imported, error_or_None)."""
        result = parse_fidelity_pdf(filepath)
        month = result["month"]

        # Create or find the Fidelity brokerage account
        acct_name = "Fidelity Brokerage"
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="brokerage", institution="Fidelity")
            session.add(account)
            session.commit()

        # Save balance snapshot
        existing_bal = (
            session.query(Balance)
            .filter(Balance.account_id == account.id, Balance.month == month)
            .first()
        )
        if existing_bal:
            existing_bal.balance = result["portfolio_value"]
        else:
            session.add(Balance(month=month, account_id=account.id, balance=result["portfolio_value"]))
        session.commit()

        # Clear and re-insert holdings for this month
        session.query(Holding).filter(
            Holding.account_id == account.id, Holding.month == month
        ).delete()
        session.commit()

        for h in result["holdings"]:
            session.add(Holding(
                month=month, account_id=account.id, symbol=h["symbol"],
                description=h["description"], quantity=h["quantity"],
                price=h["price"], beginning_value=h["beginning_value"],
                ending_value=h["ending_value"], cost_basis=h["cost_basis"],
                gain_loss=h["gain_loss"],
            ))
        session.commit()

        # Clear and re-insert activities for this month
        session.query(InvestmentActivity).filter(
            InvestmentActivity.account_id == account.id,
            InvestmentActivity.month == month,
        ).delete()
        session.commit()

        all_activity = result["activities"] + result["dividends"]
        for a in all_activity:
            session.add(InvestmentActivity(
                month=month, account_id=account.id, date=a["date"],
                symbol=a["symbol"], description=a["name"],
                action=a["action"], quantity=a["quantity"],
                price=a["price"], amount=a["amount"],
                realized_gain=a.get("realized_gain", 0),
            ))
        session.commit()

        total = len(result["holdings"]) + len(all_activity)
        return total, None

    def _import_etrade_pdf(filepath, session):
        """Import an E*Trade / Morgan Stanley at Work PDF. Returns (num_imported, error_or_None)."""
        result = parse_etrade_pdf(filepath)
        month = result["month"]

        # Create or find the E*Trade account
        acct_name = f"E*Trade ({result['account_number'][-3:]})" if result["account_number"] else "E*Trade Stock Plan"
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="brokerage", institution="E*Trade")
            session.add(account)
            session.commit()

        # Save balance snapshot (total ending value)
        existing_bal = (
            session.query(Balance)
            .filter(Balance.account_id == account.id, Balance.month == month)
            .first()
        )
        if existing_bal:
            existing_bal.balance = result["ending_value"]
        else:
            session.add(Balance(month=month, account_id=account.id, balance=result["ending_value"]))
        session.commit()

        # Clear and re-insert holdings for this month
        session.query(Holding).filter(
            Holding.account_id == account.id, Holding.month == month
        ).delete()
        session.commit()

        for h in result["holdings"]:
            session.add(Holding(
                month=month, account_id=account.id, symbol=h["symbol"],
                description=h["description"], quantity=h["quantity"],
                price=h["price"], beginning_value=0,
                ending_value=h["market_value"], cost_basis=h["cost_basis"],
                gain_loss=h["gain_loss"],
            ))
        session.commit()

        # Clear and re-insert RSU grants for this month
        session.query(StockPlanGrant).filter(
            StockPlanGrant.account_id == account.id, StockPlanGrant.month == month
        ).delete()
        session.commit()

        for g in result["rsu_grants"]:
            session.add(StockPlanGrant(
                month=month, account_id=account.id,
                grant_date=g["grant_date"], grant_number=g["grant_number"],
                grant_type=g["type"], symbol=g["symbol"],
                quantity=g["quantity"], grant_price=g["grant_price"],
                market_price=g["market_price"], estimated_value=g["estimated_value"],
            ))
        session.commit()

        # Clear and re-insert vesting events for this month
        session.query(VestingEvent).filter(
            VestingEvent.account_id == account.id, VestingEvent.month == month
        ).delete()
        session.commit()

        for v in result["vestings"]:
            session.add(VestingEvent(
                month=month, account_id=account.id,
                date=v["date"], symbol=v.get("security", ""),
                quantity=v["quantity"], amount=v["amount"],
            ))
        session.commit()

        total = len(result["holdings"]) + len(result["rsu_grants"]) + len(result["vestings"])
        return total, None

    def _import_mortgage_pdf(filepath, session):
        """Import a mortgage loan statement PDF. Returns (num_imported, error_or_None)."""
        result = parse_mortgage_pdf(filepath)
        month = result["month"]

        # Create or find the mortgage account
        acct_name = f"Mortgage ({result['loan_number'][-4:]})" if result["loan_number"] else "Mortgage"
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="mortgage", institution=result["lender"])
            session.add(account)
            session.commit()

        # Save balance snapshot (negative = liability)
        existing_bal = (
            session.query(Balance)
            .filter(Balance.account_id == account.id, Balance.month == month)
            .first()
        )
        balance_val = -result["principal_balance"]
        if existing_bal:
            existing_bal.balance = balance_val
        else:
            session.add(Balance(month=month, account_id=account.id, balance=balance_val))
        session.commit()

        # Upsert mortgage details (one record per loan_number per month)
        existing = (
            session.query(Mortgage)
            .filter(Mortgage.account_id == account.id, Mortgage.month == month)
            .first()
        )
        fields = {
            "loan_number": result["loan_number"],
            "lender": result["lender"],
            "property_address": result["property_address"],
            "interest_rate": result["interest_rate"],
            "principal_balance": result["principal_balance"],
            "monthly_payment": result["monthly_payment"],
            "principal_portion": result["principal_portion"],
            "interest_portion": result["interest_portion"],
            "escrow_portion": result["escrow_portion"],
            "escrow_balance": result["escrow_balance"],
            "statement_date": result["statement_date"],
            "payment_due_date": result["payment_due_date"],
            "ytd_principal": result["ytd_principal"],
            "ytd_interest": result["ytd_interest"],
            "ytd_total": result["ytd_total"],
        }
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            session.add(Mortgage(month=month, account_id=account.id, **fields))
        session.commit()

        return 1, None

    def _import_hsa_pdf(filepath, session):
        """Import a HealthEquity HSA statement PDF. Returns (num_imported, error_or_None)."""
        result = parse_hsa_pdf(filepath)
        month = result["month"]

        # Find or create account
        acct_name = "HealthEquity HSA"
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="hsa", institution="HealthEquity")
            session.add(account)
            session.commit()

        # Save balance snapshot (cash + investments)
        existing_bal = (
            session.query(Balance)
            .filter(Balance.account_id == account.id, Balance.month == month)
            .first()
        )
        total_value = result["total_value"]
        if existing_bal:
            existing_bal.balance = total_value
        else:
            session.add(Balance(month=month, account_id=account.id, balance=total_value))
        session.commit()

        # Upsert HSA summary
        existing_summary = (
            session.query(HsaSummary)
            .filter(HsaSummary.account_id == account.id, HsaSummary.month == month)
            .first()
        )
        fields = {
            "beginning_balance": result["beginning_balance"],
            "ending_balance": result["ending_balance"],
            "investment_value": result["investment_value"],
            "contributions": result["contributions"],
            "claims": result["claims"],
            "interest": result["interest"],
            "fees": result["fees"],
            "period_return": result["period_return"],
            "ytd_return": result["ytd_return"],
        }
        if existing_summary:
            for k, v in fields.items():
                setattr(existing_summary, k, v)
        else:
            session.add(HsaSummary(month=month, account_id=account.id, **fields))
        session.commit()

        # Upsert investment holdings
        session.query(Holding).filter(
            Holding.account_id == account.id, Holding.month == month
        ).delete()
        session.commit()
        for inv in result["investments"]:
            session.add(Holding(
                month=month, account_id=account.id, symbol=inv["symbol"],
                description=inv["symbol"], quantity=inv["shares"],
                price=inv["price"], ending_value=inv["value"],
            ))
        session.commit()

        # Import transactions with dedup
        existing_fps = set(
            fp for (fp,) in session.query(Transaction.fingerprint)
            .filter(Transaction.account_id == account.id).all()
        )
        new_count = 0
        for t in result["transactions"]:
            txn = Transaction(
                date=t["date"], amount=t["amount"], category=t["category"],
                description=t["description"], account_id=account.id,
            )
            if txn.fingerprint not in existing_fps:
                session.add(txn)
                existing_fps.add(txn.fingerprint)
                new_count += 1
        session.commit()

        return new_count, None

    def _import_amex_xlsx(filepath, session):
        """Import an AMEX activity XLSX. Returns (num_imported, error_or_None)."""
        result = parse_amex_xlsx(filepath)

        # Find or create account
        acct_name = result["card_name"]
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="credit_card", institution="Amex")
            session.add(account)
            session.commit()

        # Import transactions with dedup
        existing_fps = set(
            fp for (fp,) in session.query(Transaction.fingerprint)
            .filter(Transaction.account_id == account.id).all()
        )
        new_count = 0
        for t in result["transactions"]:
            txn = Transaction(
                date=t["date"], amount=t["amount"], category=t["category"],
                description=t["description"], account_id=account.id,
            )
            if txn.fingerprint not in existing_fps:
                session.add(txn)
                existing_fps.add(txn.fingerprint)
                new_count += 1
        session.commit()

        return new_count, None

    def _import_wellsfargo(filepath, session):
        """Import a Wells Fargo credit card statement (PDF or CSV). Returns (num_imported, error_or_None)."""
        if filepath.lower().endswith(".pdf"):
            result = parse_wellsfargo_pdf(filepath)
        else:
            result = parse_wellsfargo_csv(filepath)

        acct_name = result["card_name"]
        account = session.query(Account).filter(Account.name == acct_name).first()
        if not account:
            account = Account(name=acct_name, account_type="credit_card", institution="Wells Fargo")
            session.add(account)
            session.commit()

        # Import transactions with dedup
        existing_fps = set(
            fp for (fp,) in session.query(Transaction.fingerprint)
            .filter(Transaction.account_id == account.id).all()
        )
        new_count = 0
        for t in result["transactions"]:
            txn = Transaction(
                date=t["date"], amount=t["amount"], category=t["category"],
                description=t["description"], account_id=account.id,
            )
            if txn.fingerprint not in existing_fps:
                session.add(txn)
                existing_fps.add(txn.fingerprint)
                new_count += 1
        session.commit()

        # Save balance from PDF statement
        if result.get("balance") and result.get("month"):
            month = result["month"]
            existing_bal = (
                session.query(Balance)
                .filter(Balance.account_id == account.id, Balance.month == month)
                .first()
            )
            if existing_bal:
                existing_bal.balance = result["balance"]
            else:
                session.add(Balance(month=month, account_id=account.id, balance=result["balance"]))
            session.commit()

        return new_count, None

    def _is_fidelity_csv(filepath):
        """Check if a CSV is a Fidelity statement by checking headers."""
        try:
            with open(filepath, "r") as f:
                header = f.readline()
            return "Ending mkt Value" in header or "Beginning mkt Value" in header
        except Exception:
            return False

    @app.route("/import/scan", methods=["POST"])
    def import_scan():
        session = get_db()
        pending_files = _scan_all_files(app.config["IMPORT_FOLDER"])
        total_imported = 0
        errors = []
        files_processed = 0

        # Check if any Fidelity PDFs are in this batch (PDF is richer than CSV)
        has_fidelity_pdf = any(
            f.lower().endswith(".pdf") and is_fidelity_pdf(f)
            for f in pending_files
        )

        for filepath in pending_files:
            filename = os.path.basename(filepath)

            if filepath.lower().endswith(".pdf"):
                if is_mortgage_pdf(filepath):
                    count, error = _import_mortgage_pdf(filepath, session)
                elif is_hsa_pdf(filepath):
                    count, error = _import_hsa_pdf(filepath, session)
                elif is_etrade_pdf(filepath):
                    count, error = _import_etrade_pdf(filepath, session)
                elif is_wellsfargo_pdf(filepath):
                    count, error = _import_wellsfargo(filepath, session)
                elif is_fidelity_pdf(filepath):
                    count, error = _import_fidelity_pdf(filepath, session)
                else:
                    count, error = 0, f"Unrecognized PDF format: {os.path.basename(filepath)}"
                if error:
                    errors.append(error)
                else:
                    total_imported += count
                    files_processed += 1
                    move_to_processed(filepath, app.config["PROCESSED_FOLDER"])

            elif filepath.lower().endswith(".xlsx") and is_amex_xlsx(filepath):
                count, error = _import_amex_xlsx(filepath, session)
                if error:
                    errors.append(error)
                else:
                    total_imported += count
                    files_processed += 1
                    move_to_processed(filepath, app.config["PROCESSED_FOLDER"])

            elif filepath.lower().endswith(".csv") and is_wellsfargo_csv(filepath):
                count, error = _import_wellsfargo(filepath, session)
                if error:
                    errors.append(error)
                else:
                    total_imported += count
                    files_processed += 1
                    move_to_processed(filepath, app.config["PROCESSED_FOLDER"])

            elif filepath.lower().endswith(".csv") and _is_fidelity_csv(filepath):
                if has_fidelity_pdf:
                    # Skip Fidelity CSV when a richer PDF is available
                    move_to_processed(filepath, app.config["PROCESSED_FOLDER"])
                    files_processed += 1
                    continue
                count, error = _import_fidelity_csv(filepath, session)
                if error:
                    errors.append(error)
                else:
                    total_imported += count
                    files_processed += 1
                    move_to_processed(filepath, app.config["PROCESSED_FOLDER"])

            else:
                # Regular CSV — use profile-based import
                profile = detect_csv_format(filepath, session)
                if not profile:
                    errors.append(f"Unknown format: {filename}")
                    continue

                account = (
                    session.query(Account)
                    .filter(Account.institution == profile.institution, Account.account_type == profile.account_type)
                    .first()
                )
                if not account:
                    account = Account(
                        name=profile.name, account_type=profile.account_type,
                        institution=profile.institution,
                    )
                    session.add(account)
                    session.commit()

                new_txns = import_file(filepath, profile, account.id, session)
                total_imported += len(new_txns)
                files_processed += 1
                move_to_processed(filepath, app.config["PROCESSED_FOLDER"])

        session.close()

        if files_processed > 0:
            flash(f"Imported {total_imported} items from {files_processed} files.", "success")
        if errors:
            flash(f"Errors: {'; '.join(errors)}", "error")
        if files_processed == 0 and not errors:
            flash("No new data found.", "success")

        return redirect(url_for("import_page"))

    @app.route("/import/profile", methods=["POST"])
    def add_csv_profile():
        session = get_db()
        mapping = {
            "date": request.form["col_date"],
            "amount": request.form["col_amount"],
            "description": request.form["col_description"],
        }
        if request.form.get("col_category"):
            mapping["category"] = request.form["col_category"]

        profile = CsvProfile(
            name=request.form["profile_name"],
            institution=request.form["institution"],
            account_type=request.form["account_type"],
            column_mapping=json.dumps(mapping),
            date_format=request.form.get("date_format", "%m/%d/%Y"),
        )
        session.add(profile)
        session.commit()
        session.close()
        flash(f"CSV profile '{profile.name}' created.", "success")
        return redirect(url_for("import_page"))

    # ── Spending ──────────────────────────────────────────────────────
    @app.route("/spending")
    def spending():
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))
        return render_template("spending.html", month=month)

    @app.route("/api/spending")
    def api_spending():
        session = get_db()
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))

        # Categories to exclude from spending view
        excluded = {"Transfer", "Income", "Interest"}

        # Current month expenses (negative amounts, real spending only)
        txns = (
            session.query(Transaction)
            .filter(
                Transaction.date.like(f"{month}%"),
                Transaction.amount < 0,
                ~Transaction.category.in_(excluded),
            )
            .all()
        )

        # Group by category
        cat_totals = defaultdict(float)
        for t in txns:
            cat_totals[t.category] += abs(t.amount)

        categories = [{"name": k, "total": round(v, 2)} for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])]

        # Previous month for comparison
        year, mo = int(month[:4]), int(month[5:])
        if mo == 1:
            prev_month = f"{year - 1}-12"
        else:
            prev_month = f"{year}-{mo - 1:02d}"

        prev_txns = (
            session.query(Transaction)
            .filter(
                Transaction.date.like(f"{prev_month}%"),
                Transaction.amount < 0,
                ~Transaction.category.in_(excluded),
            )
            .all()
        )
        prev_cat_totals = defaultdict(float)
        for t in prev_txns:
            prev_cat_totals[t.category] += abs(t.amount)

        all_cats = set(cat_totals.keys()) | set(prev_cat_totals.keys())
        comparison = []
        for cat in sorted(all_cats):
            current = round(cat_totals.get(cat, 0), 2)
            previous = round(prev_cat_totals.get(cat, 0), 2)
            comparison.append({
                "name": cat, "current": current, "previous": previous,
                "change": round(current - previous, 2),
            })

        # Spending transactions only (negative, excluding transfers/income/interest)
        txn_list = [
            {"id": t.id, "date": t.date, "amount": t.amount, "category": t.category,
             "description": t.description, "account": t.account.name}
            for t in txns
        ]
        txn_list.sort(key=lambda x: x["date"], reverse=True)

        total_spending = round(sum(abs(t.amount) for t in txns), 2)

        session.close()
        return jsonify({
            "categories": categories, "comparison": comparison,
            "transactions": txn_list,
            "total_spending": total_spending,
            "all_categories": get_all_categories(),
        })

    @app.route("/transactions")
    def transactions_page():
        return render_template("transactions.html")

    @app.route("/api/transactions")
    def api_transactions():
        session = get_db()
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))

        all_txns = (
            session.query(Transaction)
            .filter(Transaction.date.like(f"{month}%"))
            .order_by(Transaction.date.desc())
            .all()
        )
        txn_list = [
            {"id": t.id, "date": t.date, "amount": t.amount, "category": t.category,
             "description": t.description, "account": t.account.name}
            for t in all_txns
        ]

        session.close()
        return jsonify({
            "transactions": txn_list,
            "all_categories": get_all_categories(),
        })

    @app.route("/api/transaction/<int:txn_id>/category", methods=["PUT"])
    def update_transaction_category(txn_id):
        """Update a single transaction's category."""
        session = get_db()
        data = request.get_json()
        new_category = data.get("category")
        if not new_category:
            session.close()
            return jsonify({"error": "category required"}), 400

        txn = session.query(Transaction).get(txn_id)
        if not txn:
            session.close()
            return jsonify({"error": "not found"}), 404

        txn.category = new_category
        session.commit()
        session.close()
        return jsonify({"ok": True, "id": txn_id, "category": new_category})

    @app.route("/api/transaction/<int:txn_id>/categorize-all", methods=["PUT"])
    def categorize_all_matching(txn_id):
        """Update this transaction + all with same description, and save a rule."""
        session = get_db()
        data = request.get_json()
        new_category = data.get("category")
        if not new_category:
            session.close()
            return jsonify({"error": "category required"}), 400

        txn = session.query(Transaction).get(txn_id)
        if not txn:
            session.close()
            return jsonify({"error": "not found"}), 404

        # Find a good keyword from the description (first 2-3 significant words)
        keyword = _extract_keyword(txn.description)

        # Update all transactions matching this keyword
        matching = session.query(Transaction).filter(
            Transaction.description.ilike(f"%{keyword}%")
        ).all()
        count = 0
        for t in matching:
            t.category = new_category
            count += 1

        # Save or update the rule
        existing_rule = session.query(CategoryRule).filter(CategoryRule.keyword == keyword.upper()).first()
        if existing_rule:
            existing_rule.category = new_category
        else:
            session.add(CategoryRule(keyword=keyword.upper(), category=new_category))

        session.commit()
        session.close()
        return jsonify({"ok": True, "updated": count, "keyword": keyword, "category": new_category})

    @app.route("/api/categories/rules")
    def get_category_rules():
        """List all user-defined category rules."""
        session = get_db()
        rules = session.query(CategoryRule).order_by(CategoryRule.category).all()
        result = [{"id": r.id, "keyword": r.keyword, "category": r.category} for r in rules]
        session.close()
        return jsonify(result)

    @app.route("/api/categories/rules/<int:rule_id>", methods=["DELETE"])
    def delete_category_rule(rule_id):
        """Delete a category rule."""
        session = get_db()
        rule = session.query(CategoryRule).get(rule_id)
        if rule:
            session.delete(rule)
            session.commit()
        session.close()
        return jsonify({"ok": True})

    def _extract_keyword(description):
        """Extract a meaningful keyword from a transaction description for rule matching."""
        if not description:
            return ""
        # Remove common prefixes and noise
        desc = description.upper()
        for prefix in ["DD *", "TST*", "TST* ", "SQ *", "PAR*", "SP ", "SP *"]:
            if desc.startswith(prefix):
                desc = desc[len(prefix):]
        # Take first meaningful chunk (before location info like city/state)
        parts = desc.split()
        # Return first 2-3 words as the keyword
        keyword = " ".join(parts[:min(3, len(parts))])
        # Strip trailing state codes
        for state in [" TX", " CA", " NV", " FL", " NY", " IL"]:
            if keyword.endswith(state):
                keyword = keyword[:-3].strip()
        return keyword

    # ── Investments ───────────────────────────────────────────────────
    @app.route("/investments")
    def investments():
        return render_template("investments.html")

    @app.route("/api/investments")
    def api_investments():
        session = get_db()
        month = request.args.get("month")
        inv_types = ("brokerage", "rsu", "espp", "401k", "ira")
        inv_accounts = session.query(Account).filter(Account.account_type.in_(inv_types)).all()

        accounts_data = []
        for acct in inv_accounts:
            balances = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month)
                .all()
            )
            history = [{"month": b.month, "balance": b.balance} for b in balances]
            latest = balances[-1].balance if balances else 0
            earliest = balances[0].balance if balances else 0
            accounts_data.append({
                "name": acct.name, "type": acct.account_type,
                "institution": acct.institution,
                "current_balance": latest,
                "total_change": round(latest - earliest, 2),
                "history": history,
            })

        total_current = sum(a["current_balance"] for a in accounts_data)

        # Get individual holdings for the selected month (or best available)
        from sqlalchemy import func
        if month:
            has_data = session.query(Holding).filter(Holding.month == month).first()
            if has_data:
                latest_month = (month,)
            else:
                # Fall back to closest month with data
                month_counts = (
                    session.query(Holding.month, func.count(Holding.id))
                    .group_by(Holding.month)
                    .order_by(func.count(Holding.id).desc())
                    .first()
                )
                latest_month = (month_counts[0],) if month_counts else None
        else:
            month_counts = (
                session.query(Holding.month, func.count(Holding.id))
                .group_by(Holding.month)
                .order_by(func.count(Holding.id).desc())
                .first()
            )
            latest_month = (month_counts[0],) if month_counts else None
        holdings_data = []
        if latest_month:
            holdings = (
                session.query(Holding)
                .filter(Holding.month == latest_month[0])
                .order_by(Holding.ending_value.desc())
                .all()
            )
            holdings_data = [
                {
                    "symbol": h.symbol,
                    "description": h.description,
                    "quantity": h.quantity,
                    "price": h.price,
                    "beginning_value": h.beginning_value,
                    "ending_value": h.ending_value,
                    "cost_basis": h.cost_basis,
                    "gain_loss": h.gain_loss,
                    "period_change": round(h.ending_value - h.beginning_value, 2) if h.beginning_value > 0 else 0,
                    "period_change_pct": round((h.ending_value - h.beginning_value) / h.beginning_value * 100, 1) if h.beginning_value > 0 else 0,
                    "gain_loss_pct": round(h.gain_loss / h.cost_basis * 100, 1) if h.cost_basis > 0 else 0,
                }
                for h in holdings
                if h.symbol != "SPAXX"  # Skip money market
            ]

        # Get investment activities for the selected month (or latest)
        activities_data = []
        dividends_data = []
        realized_gains = {"short_term": 0, "long_term": 0, "total": 0}
        if month:
            activity_month = month
        else:
            latest_activity_month = (
                session.query(InvestmentActivity.month)
                .order_by(InvestmentActivity.month.desc())
                .first()
            )
            activity_month = latest_activity_month[0] if latest_activity_month else (latest_month[0] if latest_month else None)
        if activity_month:
            activities = (
                session.query(InvestmentActivity)
                .filter(InvestmentActivity.month == activity_month)
                .order_by(InvestmentActivity.date)
                .all()
            )
            for a in activities:
                entry = {
                    "date": a.date,
                    "symbol": a.symbol,
                    "description": a.description,
                    "action": a.action,
                    "quantity": a.quantity,
                    "price": a.price,
                    "amount": a.amount,
                    "realized_gain": a.realized_gain,
                }
                if a.action == "dividend":
                    dividends_data.append(entry)
                else:
                    activities_data.append(entry)

            # Compute realized gains from sells
            for a in activities_data:
                if a["action"] == "sold" and a["realized_gain"]:
                    realized_gains["total"] += a["realized_gain"]
                    realized_gains["short_term"] += a["realized_gain"]

        # Compute total bought / sold this period
        total_bought = sum(abs(a["amount"]) for a in activities_data if a["action"] == "bought")
        total_sold = sum(a["amount"] for a in activities_data if a["action"] == "sold")
        total_dividends = sum(d["amount"] for d in dividends_data)

        session.close()
        return jsonify({
            "accounts": accounts_data,
            "total_current": total_current,
            "holdings": holdings_data,
            "activities": activities_data,
            "dividends": dividends_data,
            "realized_gains": realized_gains,
            "total_bought": total_bought,
            "total_sold": total_sold,
            "total_dividends": total_dividends,
        })

    # ── Income ────────────────────────────────────────────────────────
    @app.route("/income")
    def income():
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))
        return render_template("income.html", month=month)

    @app.route("/api/income")
    def api_income():
        session = get_db()
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))

        # Salary: positive transactions categorized as Income
        salary_txns = (
            session.query(Transaction)
            .filter(Transaction.date.like(f"{month}%"), Transaction.category == "Income")
            .order_by(Transaction.date)
            .all()
        )
        salary = [
            {"date": t.date, "description": t.description, "amount": t.amount, "account": t.account.name}
            for t in salary_txns
        ]
        salary_total = sum(t.amount for t in salary_txns)

        # Investment activities for this month
        inv_activities = (
            session.query(InvestmentActivity)
            .filter(InvestmentActivity.month == month)
            .order_by(InvestmentActivity.date)
            .all()
        )

        # Dividends: investment dividends (exclude money market SPAXX = interest)
        dividends = [
            {"date": a.date, "symbol": a.symbol, "description": a.description, "amount": a.amount}
            for a in inv_activities
            if a.action == "dividend" and a.symbol != "SPAXX"
        ]
        dividends_total = sum(d["amount"] for d in dividends)

        # Interest: SPAXX dividends (money market) + any bank interest transactions
        interest_inv = [
            {"date": a.date, "symbol": a.symbol, "description": "Money Market Interest", "amount": a.amount}
            for a in inv_activities
            if a.action == "dividend" and a.symbol == "SPAXX"
        ]
        # Also check for bank interest transactions (positive amounts not categorized as Income/Transfer)
        interest_txns = (
            session.query(Transaction)
            .filter(
                Transaction.date.like(f"{month}%"),
                Transaction.amount > 0,
                Transaction.category.notin_(["Income", "Transfer"]),
            )
            .all()
        )
        interest_bank = [
            {"date": t.date, "symbol": "", "description": t.description, "amount": t.amount}
            for t in interest_txns
        ]
        interest = interest_inv + interest_bank
        interest_total = sum(i["amount"] for i in interest)

        # Realized gains from sold investments
        sells = [
            {
                "date": a.date, "symbol": a.symbol, "description": a.description,
                "amount": a.amount, "realized_gain": a.realized_gain,
            }
            for a in inv_activities
            if a.action == "sold" and a.realized_gain != 0
        ]
        realized_total = sum(s["realized_gain"] for s in sells)

        total_income = salary_total + dividends_total + interest_total + realized_total

        # Monthly history for chart (last 12 months)
        year, mo = int(month[:4]), int(month[5:])
        monthly_history = []
        for i in range(11, -1, -1):
            m_num = mo - i
            m_year = year
            while m_num <= 0:
                m_num += 12
                m_year -= 1
            m_str = f"{m_year}-{m_num:02d}"

            # Salary
            m_salary = sum(
                t.amount for t in session.query(Transaction).filter(
                    Transaction.date.like(f"{m_str}%"), Transaction.category == "Income"
                ).all()
            )
            # Dividends + Interest from investments
            m_inv = session.query(InvestmentActivity).filter(
                InvestmentActivity.month == m_str, InvestmentActivity.action == "dividend"
            ).all()
            m_dividends = sum(a.amount for a in m_inv if a.symbol != "SPAXX")
            m_interest = sum(a.amount for a in m_inv if a.symbol == "SPAXX")
            # Realized gains
            m_gains = sum(
                a.realized_gain for a in session.query(InvestmentActivity).filter(
                    InvestmentActivity.month == m_str, InvestmentActivity.action == "sold"
                ).all() if a.realized_gain
            )

            monthly_history.append({
                "month": m_str,
                "salary": round(m_salary, 2),
                "dividends": round(m_dividends, 2),
                "interest": round(m_interest, 2),
                "realized_gains": round(m_gains, 2),
                "total": round(m_salary + m_dividends + m_interest + m_gains, 2),
            })

        session.close()
        return jsonify({
            "salary": salary, "salary_total": round(salary_total, 2),
            "dividends": dividends, "dividends_total": round(dividends_total, 2),
            "interest": interest, "interest_total": round(interest_total, 2),
            "sells": sells, "realized_total": round(realized_total, 2),
            "total_income": round(total_income, 2),
            "monthly_history": monthly_history,
        })

    # ── Equity Compensation ────────────────────────────────────────────
    @app.route("/equity")
    def equity():
        return render_template("equity.html")

    @app.route("/api/equity")
    def api_equity():
        session = get_db()
        req_month = request.args.get("month")

        # Use requested month if it has data, otherwise fall back to latest
        if req_month:
            has_data = session.query(StockPlanGrant).filter(StockPlanGrant.month == req_month).first()
            month = req_month if has_data else None
        else:
            month = None

        if not month:
            latest_grant_month = (
                session.query(StockPlanGrant.month)
                .order_by(StockPlanGrant.month.desc())
                .first()
            )
            month = latest_grant_month[0] if latest_grant_month else None

        if not month:
            session.close()
            return jsonify({
                "month": None,
                "vested_holdings": [],
                "rsu_grants": [],
                "vestings": [],
                "plan_summary": {"potential_value": 0, "total_value": 0},
                "vested_value": 0,
                "unvested_value": 0,
                "total_equity_value": 0,
                "cash_balance": 0,
                "account_value": 0,
            })

        # Get E*Trade account
        etrade_accounts = session.query(Account).filter(
            Account.institution == "E*Trade"
        ).all()

        # Vested holdings (actual shares in brokerage)
        vested_holdings = []
        account_value = 0
        cash_balance = 0
        for acct in etrade_accounts:
            holdings = (
                session.query(Holding)
                .filter(Holding.account_id == acct.id, Holding.month == month)
                .all()
            )
            for h in holdings:
                vested_holdings.append({
                    "symbol": h.symbol,
                    "description": h.description,
                    "quantity": h.quantity,
                    "price": h.price,
                    "market_value": h.ending_value,
                    "cost_basis": h.cost_basis,
                    "gain_loss": h.gain_loss,
                    "gain_loss_pct": round(h.gain_loss / h.cost_basis * 100, 2) if h.cost_basis else 0,
                })

            # Get balance for account value
            bal = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id, Balance.month == month)
                .first()
            )
            if bal:
                account_value += bal.balance

        vested_value = sum(h["market_value"] for h in vested_holdings)
        cash_balance = round(account_value - vested_value, 2)

        # RSU grants (unvested)
        grants = (
            session.query(StockPlanGrant)
            .filter(StockPlanGrant.month == month)
            .order_by(StockPlanGrant.grant_date)
            .all()
        )
        rsu_grants = [
            {
                "grant_date": g.grant_date,
                "grant_number": g.grant_number,
                "type": g.grant_type,
                "symbol": g.symbol,
                "quantity": g.quantity,
                "grant_price": g.grant_price,
                "market_price": g.market_price,
                "estimated_value": g.estimated_value,
            }
            for g in grants
        ]
        unvested_value = sum(g["estimated_value"] for g in rsu_grants)

        # Vesting events
        vest_events = (
            session.query(VestingEvent)
            .filter(VestingEvent.month == month)
            .order_by(VestingEvent.date)
            .all()
        )
        vestings = [
            {
                "date": v.date,
                "symbol": v.symbol,
                "quantity": v.quantity,
                "amount": v.amount,
            }
            for v in vest_events
        ]
        total_vested_this_period = sum(v["amount"] for v in vestings)
        shares_vested_this_period = sum(v["quantity"] for v in vestings)

        total_equity_value = vested_value + unvested_value

        session.close()
        return jsonify({
            "month": month,
            "vested_holdings": vested_holdings,
            "rsu_grants": rsu_grants,
            "vestings": vestings,
            "vested_value": round(vested_value, 2),
            "unvested_value": round(unvested_value, 2),
            "total_equity_value": round(total_equity_value, 2),
            "cash_balance": cash_balance,
            "account_value": round(account_value, 2),
            "total_vested_this_period": round(total_vested_this_period, 2),
            "shares_vested_this_period": shares_vested_this_period,
            "total_unvested_shares": sum(g["quantity"] for g in rsu_grants),
        })

    # ── Fixed Expenses ────────────────────────────────────────────────
    @app.route("/fixed")
    def fixed():
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))
        return render_template("fixed.html", month=month)

    @app.route("/api/fixed")
    def api_fixed():
        session = get_db()

        # Get mortgage details — use latest available
        mortgage_data = (
            session.query(Mortgage)
            .order_by(Mortgage.month.desc())
            .first()
        )

        if not mortgage_data:
            session.close()
            return jsonify({"mortgages": [], "mortgage": None})

        # Balance history for chart
        acct = session.query(Account).get(mortgage_data.account_id)
        balance_history = []
        if acct:
            balances = (
                session.query(Balance)
                .filter(Balance.account_id == acct.id)
                .order_by(Balance.month)
                .all()
            )
            balance_history = [{"month": b.month, "balance": abs(b.balance)} for b in balances]

        mortgage = {
            "lender": mortgage_data.lender,
            "loan_number": mortgage_data.loan_number,
            "property_address": mortgage_data.property_address,
            "interest_rate": mortgage_data.interest_rate,
            "principal_balance": mortgage_data.principal_balance,
            "monthly_payment": mortgage_data.monthly_payment,
            "principal_portion": mortgage_data.principal_portion,
            "interest_portion": mortgage_data.interest_portion,
            "escrow_portion": mortgage_data.escrow_portion,
            "escrow_balance": mortgage_data.escrow_balance,
            "statement_date": mortgage_data.statement_date,
            "payment_due_date": mortgage_data.payment_due_date,
            "ytd_principal": mortgage_data.ytd_principal,
            "ytd_interest": mortgage_data.ytd_interest,
            "ytd_total": mortgage_data.ytd_total,
            "month": mortgage_data.month,
            "balance_history": balance_history,
        }

        session.close()
        return jsonify({"mortgages": [mortgage], "mortgage": mortgage})

    # ── Healthcare (HSA) ──────────────────────────────────────────────
    @app.route("/healthcare")
    def healthcare():
        return render_template("healthcare.html")

    @app.route("/api/healthcare")
    def api_healthcare():
        session = get_db()
        month = request.args.get("month", datetime.now().strftime("%Y-%m"))

        # Find HSA account
        account = session.query(Account).filter(Account.account_type == "hsa").first()
        if not account:
            session.close()
            return jsonify({"hsa": None})

        # Get summary for requested month, fall back to latest
        summary = (
            session.query(HsaSummary)
            .filter(HsaSummary.account_id == account.id, HsaSummary.month == month)
            .first()
        )
        if not summary:
            summary = (
                session.query(HsaSummary)
                .filter(HsaSummary.account_id == account.id)
                .order_by(HsaSummary.month.desc())
                .first()
            )
        if not summary:
            session.close()
            return jsonify({"hsa": None})

        actual_month = summary.month

        # Get holdings for the same month
        holdings = (
            session.query(Holding)
            .filter(Holding.account_id == account.id, Holding.month == actual_month)
            .all()
        )
        holding_list = [
            {"symbol": h.symbol, "shares": h.quantity, "price": h.price, "value": h.ending_value}
            for h in holdings
        ]

        # Get transactions for the same month
        txns = (
            session.query(Transaction)
            .filter(Transaction.account_id == account.id, Transaction.date.like(f"{actual_month}%"))
            .order_by(Transaction.date.desc())
            .all()
        )
        txn_list = [
            {"date": t.date, "description": t.description, "amount": t.amount, "category": t.category}
            for t in txns
        ]

        # Balance history across all months
        balances = (
            session.query(Balance)
            .filter(Balance.account_id == account.id)
            .order_by(Balance.month)
            .all()
        )
        balance_history = [{"month": b.month, "balance": b.balance} for b in balances]

        hsa_data = {
            "month": actual_month,
            "cash_balance": summary.ending_balance,
            "investment_value": summary.investment_value,
            "total_value": summary.ending_balance + summary.investment_value,
            "beginning_balance": summary.beginning_balance,
            "ending_balance": summary.ending_balance,
            "contributions": summary.contributions,
            "claims": summary.claims,
            "interest": summary.interest,
            "fees": summary.fees,
            "period_return": summary.period_return,
            "ytd_return": summary.ytd_return,
            "holdings": holding_list,
            "transactions": txn_list,
            "balance_history": balance_history,
        }

        session.close()
        return jsonify({"hsa": hsa_data})

    # ── Plaid ──────────────────────────────────────────────────────────
    @app.route("/plaid/link-token")
    def plaid_link_token():
        """Return a fresh link_token for Plaid Link initialization."""
        try:
            client = get_plaid_client()
            token = create_link_token(client)
            return jsonify({"link_token": token})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/plaid/exchange-token", methods=["POST"])
    def plaid_exchange_token():
        """Exchange public_token → access_token, save PlaidItem."""
        data = request.get_json()
        public_token = data.get("public_token")
        if not public_token:
            return jsonify({"error": "missing public_token"}), 400
        try:
            client = get_plaid_client()
            access_token, item_id = exchange_public_token(client, public_token)
            institution_id, institution_name = get_institution_name(client, item_id, access_token)
            session = get_db()
            existing = session.query(PlaidItem).filter_by(item_id=item_id).first()
            if not existing:
                item = PlaidItem(
                    item_id=item_id,
                    access_token=access_token,
                    institution_id=institution_id,
                    institution_name=institution_name,
                )
                session.add(item)
                session.commit()
            session.close()
            return jsonify({"status": "ok", "institution": institution_name})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/plaid/sync", methods=["POST"])
    def plaid_sync():
        """Sync new transactions for all linked Plaid items."""
        session = get_db()
        items = session.query(PlaidItem).all()
        if not items:
            session.close()
            return jsonify({"status": "no linked accounts"})
        client = get_plaid_client()
        total = 0
        results = []
        for item in items:
            try:
                count = sync_transactions(client, item, session)
                total += count
                results.append({"institution": item.institution_name, "added": count})
            except Exception as e:
                results.append({"institution": item.institution_name, "error": str(e)})
        session.close()
        return jsonify({"total_added": total, "results": results})

    @app.route("/plaid/sync-balances", methods=["POST"])
    def plaid_sync_balances():
        """Fetch real-time balances for all linked accounts."""
        session = get_db()
        items = session.query(PlaidItem).all()
        if not items:
            session.close()
            return jsonify({"status": "no linked accounts"})
        client = get_plaid_client()
        total = 0
        results = []
        for item in items:
            try:
                count = sync_balances(client, item, session)
                total += count
                results.append({"institution": item.institution_name, "updated": count})
            except Exception as e:
                results.append({"institution": item.institution_name, "error": str(e)})
        session.close()
        return jsonify({"total_updated": total, "results": results})

    @app.route("/plaid/sync-investments", methods=["POST"])
    def plaid_sync_investments():
        """Sync investment holdings and transactions for all linked accounts."""
        session = get_db()
        items = session.query(PlaidItem).all()
        if not items:
            session.close()
            return jsonify({"status": "no linked accounts"})
        client = get_plaid_client()
        results = []
        for item in items:
            try:
                holdings = sync_holdings(client, item, session)
                txns = sync_investment_transactions(client, item, session)
                results.append({"institution": item.institution_name, "holdings": holdings, "transactions": txns})
            except Exception as e:
                results.append({"institution": item.institution_name, "error": str(e)})
        session.close()
        return jsonify({"results": results})

    @app.route("/plaid/oauth-callback")
    def plaid_oauth_callback():
        """Handle OAuth redirect from banks like Chase."""
        return render_template("plaid_oauth.html")

    @app.route("/plaid/linked-accounts")
    def plaid_linked_accounts():
        """Return list of linked Plaid institutions."""
        session = get_db()
        items = session.query(PlaidItem).all()
        result = [
            {
                "id": i.id,
                "institution": i.institution_name,
                "needs_relink": bool(i.needs_relink),
                "synced": bool(i.transactions_cursor),
            }
            for i in items
        ]
        session.close()
        return jsonify({"items": result})

    @app.route("/plaid/relink/<int:item_id>")
    def plaid_relink_token(item_id):
        """Return a link_token for update mode for a specific Item."""
        session = get_db()
        item = session.query(PlaidItem).filter_by(id=item_id).first()
        session.close()
        if not item:
            return jsonify({"error": "item not found"}), 404
        try:
            client = get_plaid_client()
            token = create_relink_token(client, item.access_token)
            return jsonify({"link_token": token})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/plaid/clear-relink/<int:item_id>", methods=["POST"])
    def plaid_clear_relink(item_id):
        """Clear needs_relink flag after successful update mode."""
        session = get_db()
        item = session.query(PlaidItem).filter_by(id=item_id).first()
        if not item:
            session.close()
            return jsonify({"error": "item not found"}), 404
        item.needs_relink = 0
        session.commit()
        session.close()
        return jsonify({"status": "ok"})

    @app.route("/plaid/disconnect/<int:item_id>", methods=["POST"])
    def plaid_disconnect(item_id):
        """Call /item/remove on Plaid, then delete the PlaidItem from DB."""
        from plaid.model.item_remove_request import ItemRemoveRequest
        session = get_db()
        item = session.query(PlaidItem).filter_by(id=item_id).first()
        if not item:
            session.close()
            return jsonify({"error": "item not found"}), 404
        try:
            client = get_plaid_client()
            client.item_remove(ItemRemoveRequest(access_token=item.access_token))
        except Exception:
            pass  # If Plaid call fails, still remove locally
        session.delete(item)
        session.commit()
        session.close()
        return jsonify({"status": "disconnected"})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5002)
