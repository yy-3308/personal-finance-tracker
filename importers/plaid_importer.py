"""Plaid import logic: link token, token exchange, transaction sync."""
import json
import plaid
from datetime import date

from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.products import Products

from categorizer import categorize
from models import Account, Balance, Holding, InvestmentActivity, PlaidItem, Transaction


def create_link_token(client, redirect_uri=None):
    """Create a short-lived link_token for initializing Plaid Link."""
    kwargs = dict(
        products=[Products("transactions"), Products("investments")],
        client_name="Personal Finance Tracker",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
    )
    if redirect_uri:
        kwargs["redirect_uri"] = redirect_uri
    request = LinkTokenCreateRequest(**kwargs)
    response = client.link_token_create(request)
    return response["link_token"]


def exchange_public_token(client, public_token):
    """Exchange a public_token for a permanent access_token + item_id."""
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response["access_token"], response["item_id"]


def get_institution_name(client, item_id, access_token):
    """Look up the institution name for a linked item."""
    from plaid.model.item_get_request import ItemGetRequest
    from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest

    item_resp = client.item_get(ItemGetRequest(access_token=access_token))
    institution_id = item_resp["item"]["institution_id"]
    inst_resp = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode("US")],
        )
    )
    return institution_id, inst_resp["institution"]["name"]


def sync_transactions(client, plaid_item, db_session):
    """
    Pull new/modified/removed transactions using /transactions/sync.
    Returns count of new transactions added.
    Sets needs_relink=1 if ITEM_LOGIN_REQUIRED.
    """
    cursor = plaid_item.transactions_cursor
    added_count = 0

    try:
        while True:
            kwargs = dict(access_token=plaid_item.access_token)
            if cursor:
                kwargs["cursor"] = cursor
            request = TransactionsSyncRequest(**kwargs)
            response = client.transactions_sync(request)

            for txn in response["added"]:
                acct_id = _get_or_create_account(db_session, txn["account_id"], plaid_item)
                if acct_id is None:
                    continue
                t = Transaction(
                    date=str(txn["date"]),
                    amount=-txn["amount"],  # Plaid: positive = debit; flip to match app convention
                    description=txn.get("merchant_name") or txn.get("name", ""),
                    category=categorize(txn.get("merchant_name") or txn.get("name", ""), db_session),
                    account_id=acct_id,
                )
                existing = db_session.query(Transaction).filter_by(fingerprint=t.fingerprint).first()
                if not existing:
                    db_session.add(t)
                    added_count += 1

            cursor = response["next_cursor"]
            if not response["has_more"]:
                break

        # Sync succeeded — clear any prior error flag
        plaid_item.needs_relink = 0
        plaid_item.transactions_cursor = cursor
        db_session.commit()

    except plaid.ApiException as e:
        body = json.loads(e.body)
        if body.get("error_code") == "ITEM_LOGIN_REQUIRED":
            plaid_item.needs_relink = 1
            db_session.commit()
        raise

    return added_count


def sync_balances(client, plaid_item, db_session):
    """
    Fetch real-time balances for all accounts under a PlaidItem.
    Upserts a Balance row for the current month per account.
    Returns count of accounts updated.
    """
    from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

    today = date.today()
    month = today.strftime("%Y-%m")

    response = client.accounts_balance_get(
        AccountsBalanceGetRequest(access_token=plaid_item.access_token)
    )

    updated = 0
    for acct in response["accounts"]:
        available = acct["balances"]["available"]
        current = acct["balances"]["current"]
        balance_value = available if available is not None else current
        if balance_value is None:
            continue

        local_acct_id = _get_or_create_account(db_session, acct["account_id"], plaid_item)
        if local_acct_id is None:
            continue

        existing = (
            db_session.query(Balance)
            .filter_by(account_id=local_acct_id, month=month)
            .first()
        )
        if existing:
            existing.balance = balance_value
        else:
            db_session.add(Balance(account_id=local_acct_id, month=month, balance=balance_value))
        updated += 1

    db_session.commit()
    return updated


def sync_holdings(client, plaid_item, db_session):
    """
    Fetch current investment holdings and upsert into the holdings table.
    Returns count of holdings updated.
    """
    from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

    today = date.today()
    month = today.strftime("%Y-%m")

    response = client.investments_holdings_get(
        InvestmentsHoldingsGetRequest(access_token=plaid_item.access_token)
    )

    securities = {s["security_id"]: s for s in response["securities"]}

    updated = 0
    for h in response["holdings"]:
        sec = securities.get(h["security_id"], {})
        ticker = sec.get("ticker_symbol") or sec.get("name", "UNKNOWN")
        acct_id = _get_or_create_account(db_session, h["account_id"], plaid_item)
        if acct_id is None:
            continue

        existing = (
            db_session.query(Holding)
            .filter_by(account_id=acct_id, month=month, symbol=ticker)
            .first()
        )
        quantity = h.get("quantity", 0)
        price = sec.get("close_price") or 0
        cost_basis = h.get("cost_basis") or 0
        ending_value = quantity * price

        if existing:
            existing.quantity = quantity
            existing.price = price
            existing.cost_basis = cost_basis
            existing.ending_value = ending_value
            existing.gain_loss = ending_value - cost_basis
        else:
            db_session.add(Holding(
                month=month,
                account_id=acct_id,
                symbol=ticker,
                description=sec.get("name", ""),
                quantity=quantity,
                price=price,
                cost_basis=cost_basis,
                ending_value=ending_value,
                gain_loss=ending_value - cost_basis,
            ))
        updated += 1

    db_session.commit()
    return updated


def sync_investment_transactions(client, plaid_item, db_session):
    """
    Fetch investment transactions (buy/sell/dividend) and insert new ones.
    Returns count of new activities added.
    """
    from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest

    today = date.today()
    start_date = date(today.year - 2, today.month, today.day)
    month = today.strftime("%Y-%m")

    response = client.investments_transactions_get(
        InvestmentsTransactionsGetRequest(
            access_token=plaid_item.access_token,
            start_date=start_date,
            end_date=today,
        )
    )

    securities = {s["security_id"]: s for s in response["securities"]}
    added = 0

    for txn in response["investment_transactions"]:
        sec = securities.get(txn.get("security_id"), {})
        ticker = sec.get("ticker_symbol") or sec.get("name", "UNKNOWN")
        acct_id = _get_or_create_account(db_session, txn["account_id"], plaid_item)
        if acct_id is None:
            continue

        action_map = {
            "buy": "bought", "sell": "sold",
            "dividend": "dividend", "cash": "cash",
            "transfer": "transfer", "fee": "fee",
        }
        txn_type = str(txn.get("type", "")).lower()
        action = action_map.get(txn_type, txn_type or "other")

        plaid_txn_id = txn.get("investment_transaction_id", "")
        existing = (
            db_session.query(InvestmentActivity)
            .filter_by(account_id=acct_id, description=plaid_txn_id)
            .first()
        )
        if existing:
            continue

        db_session.add(InvestmentActivity(
            month=month,
            account_id=acct_id,
            date=str(txn["date"]),
            symbol=ticker,
            description=plaid_txn_id,
            action=action,
            quantity=txn.get("quantity") or 0,
            price=txn.get("price") or 0,
            amount=txn.get("amount") or 0,
            realized_gain=0,
        ))
        added += 1

    db_session.commit()
    return added


def create_relink_token(client, access_token, redirect_uri=None):
    """Create a link_token for update mode — re-authenticates an existing Item.

    NOTE: Do NOT pass products= here. Update mode reuses existing product
    consents; adding products causes a Plaid API error.
    """
    kwargs = dict(
        client_name="Personal Finance Tracker",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
        access_token=access_token,
    )
    if redirect_uri:
        kwargs["redirect_uri"] = redirect_uri
    request = LinkTokenCreateRequest(**kwargs)
    response = client.link_token_create(request)
    return response["link_token"]


def _get_or_create_account(db_session, plaid_account_id, plaid_item):
    """
    Map a Plaid account_id to our local Account.id.
    Uses institution_name + last 4 chars of account_id as the account name.
    Creates the Account if it doesn't exist yet.
    """
    name = f"{plaid_item.institution_name} ({plaid_account_id[-4:]})"
    acct = db_session.query(Account).filter_by(name=name).first()
    if not acct:
        acct = Account(
            name=name,
            account_type="checking",
            institution=plaid_item.institution_name or "Plaid",
        )
        db_session.add(acct)
        db_session.flush()
    return acct.id
