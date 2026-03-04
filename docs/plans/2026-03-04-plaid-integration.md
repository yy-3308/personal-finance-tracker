# Plaid Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Plaid Link to the Import page so the user can connect bank accounts and sync transactions automatically, instead of manually downloading PDFs/CSVs.

**Architecture:** A new `PlaidItem` model stores `access_token` + `item_id` per linked institution. A `plaid_client.py` module sets up the API client (with SSL cert fix). New Flask routes handle the Link token flow and transaction sync. The Import page gets a "Connect a Bank" card using Plaid Link JS.

**Tech Stack:** `plaid-python`, `python-dotenv`, PIL (already installed), Plaid Link JS (CDN), Flask, SQLAlchemy, SQLite

---

### Task 1: Add PlaidItem model

**Files:**
- Modify: `models.py`

**Step 1: Add the model to `models.py`**

Append this class at the end of the file:

```python
class PlaidItem(Base):
    """Linked bank via Plaid — stores access token per institution."""
    __tablename__ = "plaid_items"
    id = Column(Integer, primary_key=True)
    item_id = Column(String, nullable=False, unique=True)
    access_token = Column(String, nullable=False)
    institution_id = Column(String)
    institution_name = Column(String)
    # cursor for incremental transaction sync (Plaid /transactions/sync)
    transactions_cursor = Column(String)
```

**Step 2: Verify the DB migrates cleanly**

Run:
```bash
source venv/bin/activate && python3 -c "
from database import init_db
init_db('data/finance.db')
print('OK')
"
```
Expected: `OK` (SQLAlchemy creates the new table via `create_all`)

**Step 3: Commit**

```bash
git add models.py
git commit -m "feat: add PlaidItem model"
```

---

### Task 2: Create Plaid client module

**Files:**
- Create: `plaid_client.py`

**Step 1: Create `plaid_client.py`**

```python
"""Plaid API client — configured from environment variables."""
import certifi
import os
import plaid
from plaid.api import plaid_api

# macOS Python 3.12 doesn't ship with SSL certs — use certifi's bundle
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "development": plaid.Environment.Development,
    "production": plaid.Environment.Production,
}


def get_plaid_client():
    """Return a configured PlaidApi client."""
    from config import Config
    env = _ENV_MAP.get(Config.PLAID_ENV, plaid.Environment.Sandbox)
    configuration = plaid.Configuration(
        host=env,
        api_key={
            "clientId": Config.PLAID_CLIENT_ID,
            "secret": Config.PLAID_SECRET,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)
```

**Step 2: Smoke test**

```bash
source venv/bin/activate && python3 -c "
from plaid_client import get_plaid_client
client = get_plaid_client()
print('client OK:', type(client).__name__)
"
```
Expected: `client OK: PlaidApi`

**Step 3: Commit**

```bash
git add plaid_client.py
git commit -m "feat: add plaid client module with ssl fix"
```

---

### Task 3: Create plaid_importer.py

**Files:**
- Create: `plaid_importer.py`

**Step 1: Create `plaid_importer.py`**

```python
"""Plaid import logic: link token, token exchange, transaction sync."""
from datetime import date, timedelta

from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.products import Products

from categorizer import categorize
from models import Account, Balance, PlaidItem, Transaction


def create_link_token(client, redirect_uri=None):
    """Create a short-lived link_token for initializing Plaid Link."""
    kwargs = dict(
        products=[Products("transactions")],
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
    """
    cursor = plaid_item.transactions_cursor  # None on first sync
    added_count = 0

    while True:
        kwargs = dict(access_token=plaid_item.access_token)
        if cursor:
            kwargs["cursor"] = cursor
        request = TransactionsSyncRequest(**kwargs)
        response = client.transactions_sync(request)

        # Find or create Account for each transaction
        for txn in response["added"]:
            acct_id = _get_or_create_account(
                db_session,
                plaid_account_id=txn["account_id"],
                plaid_item=plaid_item,
            )
            if acct_id is None:
                continue

            t = Transaction(
                date=str(txn["date"]),
                amount=-txn["amount"],  # Plaid: positive = debit; flip to match app convention
                description=txn.get("merchant_name") or txn.get("name", ""),
                category=categorize(txn.get("merchant_name") or txn.get("name", ""), db_session),
                account_id=acct_id,
            )
            # Only insert if not duplicate
            existing = db_session.query(Transaction).filter_by(fingerprint=t.fingerprint).first()
            if not existing:
                db_session.add(t)
                added_count += 1

        cursor = response["next_cursor"]
        if not response["has_more"]:
            break

    plaid_item.transactions_cursor = cursor
    db_session.commit()
    return added_count


def _get_or_create_account(db_session, plaid_account_id, plaid_item):
    """
    Map a Plaid account_id to our local Account.id.
    Uses institution_name + account_id suffix as the account name.
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
```

**Step 2: Smoke test the imports**

```bash
source venv/bin/activate && python3 -c "
from plaid_importer import create_link_token
from plaid_client import get_plaid_client
token = create_link_token(get_plaid_client())
print('link_token:', token[:40], '...')
"
```
Expected: prints a link token starting with `link-sandbox-`

**Step 3: Commit**

```bash
git add plaid_importer.py
git commit -m "feat: add plaid_importer with link token, exchange, sync"
```

---

### Task 4: Add Flask routes for Plaid

**Files:**
- Modify: `app.py`

**Step 1: Add imports at top of `app.py`**

After the existing imports (around line 30), add:

```python
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
from plaid_client import get_plaid_client
from plaid_importer import (
    create_link_token, exchange_public_token,
    get_institution_name, sync_transactions,
)
from models import PlaidItem
```

**Step 2: Add routes inside `create_app()`, just before `return app` (line 1653)**

```python
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

    @app.route("/plaid/oauth-callback")
    def plaid_oauth_callback():
        """Handle OAuth redirect from banks like Chase. Re-opens Link with receivedRedirectUri."""
        return render_template("plaid_oauth.html")

    @app.route("/plaid/linked-accounts")
    def plaid_linked_accounts():
        """Return list of linked Plaid institutions."""
        session = get_db()
        items = session.query(PlaidItem).all()
        result = [{"id": i.id, "institution": i.institution_name, "cursor": bool(i.transactions_cursor)} for i in items]
        session.close()
        return jsonify({"items": result})
```

**Step 3: Verify app starts without error**

```bash
source venv/bin/activate && python3 -c "
from app import create_app
app = create_app()
print('routes:', [r.rule for r in app.url_map.iter_rules() if 'plaid' in r.rule])
"
```
Expected: prints the 5 plaid routes

**Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add plaid routes (link-token, exchange, sync, oauth-callback)"
```

---

### Task 5: Create OAuth callback template

**Files:**
- Create: `templates/plaid_oauth.html`

**Step 1: Create `templates/plaid_oauth.html`**

```html
{% extends "base.html" %}
{% block content %}
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
  // Re-initialize Link with the redirect URI after OAuth bank redirect
  fetch('/plaid/link-token')
    .then(r => r.json())
    .then(({ link_token }) => {
      const handler = Plaid.create({
        token: link_token,
        receivedRedirectUri: window.location.href,
        onSuccess: (public_token, metadata) => {
          fetch('/plaid/exchange-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ public_token }),
          })
          .then(r => r.json())
          .then(data => {
            window.location.href = '/import?connected=' + encodeURIComponent(data.institution || 'bank');
          });
        },
        onExit: () => { window.location.href = '/import'; },
      });
      handler.open();
    });
</script>
<div style="padding: 3rem; text-align: center; color: var(--text-muted);">
  Completing bank connection…
</div>
{% endblock %}
```

**Step 2: Commit**

```bash
git add templates/plaid_oauth.html
git commit -m "feat: add plaid oauth callback template"
```

---

### Task 6: Update import.html with Connect a Bank section

**Files:**
- Modify: `templates/import.html`

**Step 1: Add Plaid Link script tag to `<head>` section**

In `templates/base.html`, find the `<head>` block and verify there is a place for page-level scripts, OR simply include the script directly in `import.html`.

**Step 2: Insert "Connect a Bank" card into `import.html`**

Add this block at the very top of `{% block content %}`, before the existing `<div class="page-header">`:

```html
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>

{% block content %}
```

Then add a new card after the page-header div (after line 6, before `{% if checklist %}`):

```html
<div class="card animate-in" style="margin-bottom: 1rem;">
  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
    <div>
      <h3 style="margin: 0;">Connect a Bank</h3>
      <p style="color: var(--text-secondary); font-size: 0.85rem; margin: 0.25rem 0 0;">
        Link accounts via Plaid for automatic transaction sync.
      </p>
    </div>
    <div style="display: flex; gap: 0.5rem;">
      <button id="plaid-sync-btn" class="btn" style="font-size:0.85rem;">Sync Transactions</button>
      <button id="plaid-link-btn" class="btn btn-primary" style="font-size:0.85rem;">+ Connect a Bank</button>
    </div>
  </div>

  <div id="plaid-linked" style="font-size: 0.85rem; color: var(--text-muted);">Loading linked accounts…</div>
  <div id="plaid-status" style="font-size: 0.85rem; margin-top: 0.5rem;"></div>
</div>

<script>
  // Load linked accounts
  fetch('/plaid/linked-accounts')
    .then(r => r.json())
    .then(({ items }) => {
      const el = document.getElementById('plaid-linked');
      if (!items.length) {
        el.textContent = 'No linked accounts yet.';
      } else {
        el.innerHTML = items.map(i =>
          `<span style="margin-right:1rem;">&#10003; ${i.institution}</span>`
        ).join('');
      }
    });

  // Connect a Bank
  document.getElementById('plaid-link-btn').addEventListener('click', () => {
    fetch('/plaid/link-token')
      .then(r => r.json())
      .then(({ link_token, error }) => {
        if (error) { alert('Error: ' + error); return; }
        const handler = Plaid.create({
          token: link_token,
          onSuccess: (public_token, metadata) => {
            fetch('/plaid/exchange-token', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ public_token }),
            })
            .then(r => r.json())
            .then(data => {
              document.getElementById('plaid-status').innerHTML =
                `<span style="color:var(--positive);">&#10003; Connected: ${data.institution}</span>`;
              location.reload();
            });
          },
          onExit: (err) => { if (err) console.error(err); },
        });
        handler.open();
      });
  });

  // Sync Transactions
  document.getElementById('plaid-sync-btn').addEventListener('click', () => {
    const status = document.getElementById('plaid-status');
    status.textContent = 'Syncing…';
    fetch('/plaid/sync', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        if (data.error) {
          status.innerHTML = `<span style="color:var(--negative);">Error: ${data.error}</span>`;
        } else {
          status.innerHTML = `<span style="color:var(--positive);">&#10003; Synced — ${data.total_added} new transactions added</span>`;
        }
      });
  });
</script>
```

**Step 3: Test in browser**

```bash
source venv/bin/activate && python3 app.py
```

Open http://localhost:5001/import — you should see the "Connect a Bank" card with the button.

Click "Connect a Bank" → Plaid Link modal opens → use Sandbox credentials: `user_good` / `pass_good` → select a bank → confirm.

**Step 4: Commit**

```bash
git add templates/import.html
git commit -m "feat: add connect a bank ui with plaid link"
```

---

### Task 7: Add investment sync

**Files:**
- Modify: `plaid_importer.py`
- Modify: `app.py`
- Modify: `templates/import.html`

**Step 1: Update `create_link_token()` in `plaid_importer.py` to include investments product**

Change:
```python
products=[Products("transactions")],
```
To:
```python
products=[Products("transactions"), Products("investments")],
```

**Step 2: Append `sync_holdings()` and `sync_investment_transactions()` to `plaid_importer.py`**

```python
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

    # Build security lookup: security_id -> security object
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
    from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions

    today = date.today()
    start_date = date(today.year - 2, today.month, today.day)  # 24 months max
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

        # Map Plaid type to our action strings
        action_map = {
            "buy": "bought", "sell": "sold",
            "dividend": "dividend", "cash": "cash",
            "transfer": "transfer", "fee": "fee",
        }
        action = action_map.get(txn.get("type", "").lower(), txn.get("type", "other"))

        # Deduplicate by plaid transaction_id stored in description field
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
```

Also add `Holding` and `InvestmentActivity` to the imports at the top of `plaid_importer.py`:
```python
from models import Account, Balance, Holding, InvestmentActivity, PlaidItem, Transaction
```

**Step 3: Add `/plaid/sync-investments` route to `app.py`**

Inside `create_app()`, alongside the other Plaid routes:

```python
    @app.route("/plaid/sync-investments", methods=["POST"])
    def plaid_sync_investments():
        """Sync investment holdings and transactions for all linked accounts."""
        from plaid_importer import sync_holdings, sync_investment_transactions
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
```

**Step 4: Add "Sync Investments" button to `import.html`**

In the Plaid card's button group:

```html
<button id="plaid-invest-btn" class="btn" style="font-size:0.85rem;">Sync Investments</button>
```

And the JS handler:

```javascript
  document.getElementById('plaid-invest-btn').addEventListener('click', () => {
    const status = document.getElementById('plaid-status');
    status.textContent = 'Syncing investments…';
    fetch('/plaid/sync-investments', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        const summary = data.results.map(r =>
          r.error ? `${r.institution}: error` : `${r.institution}: ${r.holdings} holdings, ${r.transactions} txns`
        ).join(' · ');
        status.innerHTML = `<span style="color:var(--positive);">&#10003; ${summary}</span>`;
      });
  });
```

**Step 5: Commit**

```bash
git add plaid_importer.py app.py templates/import.html
git commit -m "feat: add plaid investment holdings and transaction sync"
```

---

### Task 8: Add balance sync

**Files:**
- Modify: `plaid_importer.py`
- Modify: `app.py`
- Modify: `templates/import.html`

**Step 1: Add `sync_balances()` to `plaid_importer.py`**

Append to the bottom of the file:

```python
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

        local_acct = _get_or_create_account(db_session, acct["account_id"], plaid_item)
        if local_acct is None:
            continue

        existing = (
            db_session.query(Balance)
            .filter_by(account_id=local_acct, month=month)
            .first()
        )
        if existing:
            existing.balance = balance_value
        else:
            db_session.add(Balance(account_id=local_acct, month=month, balance=balance_value))
        updated += 1

    db_session.commit()
    return updated
```

**Step 2: Add `/plaid/sync-balances` route to `app.py`**

Inside `create_app()`, alongside the other Plaid routes:

```python
    @app.route("/plaid/sync-balances", methods=["POST"])
    def plaid_sync_balances():
        """Fetch real-time balances for all linked accounts."""
        from plaid_importer import sync_balances
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
```

**Step 3: Add "Sync Balances" button to `import.html`**

In the Plaid card's button group, add alongside the existing sync button:

```html
<button id="plaid-balance-btn" class="btn" style="font-size:0.85rem;">Sync Balances</button>
```

And the JS handler:

```javascript
  document.getElementById('plaid-balance-btn').addEventListener('click', () => {
    const status = document.getElementById('plaid-status');
    status.textContent = 'Fetching balances…';
    fetch('/plaid/sync-balances', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        if (data.error) {
          status.innerHTML = `<span style="color:var(--negative);">Error: ${data.error}</span>`;
        } else {
          status.innerHTML = `<span style="color:var(--positive);">&#10003; Updated ${data.total_updated} account balances</span>`;
        }
      });
  });
```

**Step 4: Commit**

```bash
git add plaid_importer.py app.py templates/import.html
git commit -m "feat: add plaid balance sync"
```

---

### Task 9: End-to-end test in Sandbox

**Step 1: Connect a test account**

1. Start the app: `python3 app.py`
2. Go to http://localhost:5001/import
3. Click "Connect a Bank"
4. In Plaid Link, select any institution (e.g. "Chase")
5. Enter Sandbox credentials: `user_good` / `pass_good`
6. Complete the flow

**Step 2: Sync transactions**

Click "Sync Transactions" — should show `X new transactions added`.

**Step 3: Sync balances**

Click "Sync Balances" — should show `Updated X account balances`.

**Step 4: Sync investments**

Click "Sync Investments" — should show holdings and transaction counts per institution.

**Step 5: Verify data**

- http://localhost:5001/transactions — Plaid transactions with auto-categorization
- http://localhost:5001/ — net worth reflects synced balances
- http://localhost:5001/investments — holdings and buy/sell activity from Plaid

**Step 6: Final commit**

```bash
git add .
git commit -m "feat: plaid integration complete (sandbox)"
```

---

### Task 10: Add Link update mode (re-authentication)

**Files:**
- Modify: `models.py`
- Modify: `plaid_importer.py`
- Modify: `app.py`
- Modify: `templates/import.html`

**Step 1: Add `needs_relink` column to `PlaidItem` in `models.py`**

Add one field to the `PlaidItem` class:

```python
needs_relink = Column(Integer, default=0)  # 1 = ITEM_LOGIN_REQUIRED, 0 = healthy
```

Then verify the DB migrates:

```bash
source venv/bin/activate && python3 -c "
from database import init_db
init_db('data/finance.db')
print('OK')
"
```

**Step 2: Set `needs_relink` on sync failure in `plaid_importer.py`**

In `sync_transactions()`, wrap the Plaid API call to catch `ITEM_LOGIN_REQUIRED`:

```python
import plaid

def sync_transactions(client, plaid_item, db_session):
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
                    amount=-txn["amount"],
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
        import json
        body = json.loads(e.body)
        if body.get("error_code") == "ITEM_LOGIN_REQUIRED":
            plaid_item.needs_relink = 1
            db_session.commit()
        raise

    return added_count
```

**Step 3: Add `create_relink_token()` to `plaid_importer.py`**

Append to the file:

```python
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
```

**Step 4: Add `/plaid/relink/<int:item_id>` and `/plaid/clear-relink/<int:item_id>` routes to `app.py`**

Inside `create_app()`, alongside the other Plaid routes:

```python
    @app.route("/plaid/relink/<int:item_id>")
    def plaid_relink_token(item_id):
        """Return a link_token for update mode for a specific Item."""
        from plaid_importer import create_relink_token
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
```

**Step 5: Update `/plaid/linked-accounts` to expose item id and error state**

Find the existing `plaid_linked_accounts` route and update the result dict:

```python
        result = [
            {
                "id": i.id,
                "institution": i.institution_name,
                "synced": bool(i.transactions_cursor),
            }
            for i in items
        ]
```

**Step 6: Update the linked accounts display in `import.html`**

"Fix" button only appears when `needs_relink` is true. On success, calls `/plaid/clear-relink` to dismiss the prompt.

Replace the existing linked accounts JS block:

```javascript
  fetch('/plaid/linked-accounts')
    .then(r => r.json())
    .then(({ items }) => {
      const el = document.getElementById('plaid-linked');
      if (!items.length) {
        el.textContent = 'No linked accounts yet.';
      } else {
        el.innerHTML = items.map(i => `
          <span style="margin-right:1rem;">
            ${i.needs_relink
              ? `<span style="color:var(--negative);">&#9888; ${i.institution}</span>
                 <a href="#" onclick="relinkItem(${i.id}, event)"
                    style="font-size:0.75rem; margin-left:0.4rem; color:var(--negative); font-weight:600;">Fix connection</a>`
              : `<span style="color:var(--positive);">&#10003; ${i.institution}</span>`
            }
          </span>
        `).join('');
      }
    });

  function relinkItem(itemId, e) {
    e.preventDefault();
    const status = document.getElementById('plaid-status');
    status.textContent = 'Opening re-authentication…';
    fetch('/plaid/relink/' + itemId)
      .then(r => r.json())
      .then(({ link_token, error }) => {
        if (error) { status.textContent = 'Error: ' + error; return; }
        const handler = Plaid.create({
          token: link_token,
          onSuccess: () => {
            // Clear the needs_relink flag, then reload to dismiss the prompt
            fetch('/plaid/clear-relink/' + itemId, { method: 'POST' })
              .then(() => {
                status.innerHTML = '<span style="color:var(--positive);">&#10003; Re-authenticated successfully</span>';
                location.reload();
              });
          },
          onExit: (err) => { if (err) console.error(err); },
        });
        handler.open();
      });
  }
```

**Step 7: Test update mode in Sandbox**

In Sandbox, Plaid doesn't auto-expire Items — to test manually:
1. Call `/plaid/relink/<item_id>` in the browser
2. Confirm the Link modal opens in abbreviated re-auth mode (shorter flow than initial)
3. Complete it — verify sync still works after re-auth

**Step 8: Commit**

```bash
git add models.py plaid_importer.py app.py templates/import.html
git commit -m "feat: add plaid link update mode with needs_relink tracking"
```

---

### Task 11: Add /item/remove (disconnect account)

**Files:**
- Modify: `app.py`
- Modify: `templates/import.html`

**Step 1: Add `/plaid/disconnect/<int:item_id>` route to `app.py`**

```python
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
```

**Step 2: Add "Disconnect" button to linked accounts display in `import.html`**

In the linked accounts JS, add a disconnect link alongside the institution name:

```javascript
        el.innerHTML = items.map(i => `
          <span style="margin-right:1.25rem;">
            ${i.needs_relink
              ? `<span style="color:var(--negative);">&#9888; ${i.institution}</span>
                 <a href="#" onclick="relinkItem(${i.id}, event)"
                    style="font-size:0.75rem; margin-left:0.4rem; color:var(--negative); font-weight:600;">Fix connection</a>`
              : `<span style="color:var(--positive);">&#10003; ${i.institution}</span>`
            }
            <a href="#" onclick="disconnectItem(${i.id}, '${i.institution}', event)"
               style="font-size:0.7rem; margin-left:0.4rem; color:var(--text-muted);">Disconnect</a>
          </span>
        `).join('');
```

And the handler:

```javascript
  function disconnectItem(itemId, name, e) {
    e.preventDefault();
    if (!confirm(`Disconnect ${name}? This will stop syncing transactions from this account.`)) return;
    fetch('/plaid/disconnect/' + itemId, { method: 'POST' })
      .then(r => r.json())
      .then(() => location.reload());
  }
```

**Step 3: Commit**

```bash
git add app.py templates/import.html
git commit -m "feat: add plaid item/remove disconnect flow"
```
