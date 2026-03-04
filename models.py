import hashlib

from sqlalchemy import Column, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import Base


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    account_type = Column(String, nullable=False)
    institution = Column(String, nullable=False)
    transactions = relationship("Transaction", back_populates="account")
    balances = relationship("Balance", back_populates="account")


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    date = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    category = Column(String, default="Uncategorized")
    description = Column(String)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    fingerprint = Column(String, index=True)
    account = relationship("Account", back_populates="transactions")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._compute_fingerprint()

    def _compute_fingerprint(self):
        raw = f"{self.date}|{self.amount}|{self.description}|{self.account_id}"
        self.fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:16]


class Balance(Base):
    __tablename__ = "balances"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    balance = Column(Float, nullable=False)
    account = relationship("Account", back_populates="balances")


class Holding(Base):
    __tablename__ = "holdings"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)          # YYYY-MM
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    symbol = Column(String, nullable=False)
    description = Column(String)
    quantity = Column(Float, default=0)
    price = Column(Float, default=0)
    beginning_value = Column(Float, default=0)
    ending_value = Column(Float, default=0)
    cost_basis = Column(Float, default=0)
    gain_loss = Column(Float, default=0)            # unrealized: ending_value - cost_basis
    account = relationship("Account")


class CategoryRule(Base):
    """User-defined merchant → category mapping. Checked before keyword defaults."""
    __tablename__ = "category_rules"
    id = Column(Integer, primary_key=True)
    keyword = Column(String, nullable=False, unique=True)  # uppercase substring to match
    category = Column(String, nullable=False)


class InvestmentActivity(Base):
    """Buy/sell/dividend activity from investment accounts."""
    __tablename__ = "investment_activities"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)           # YYYY-MM (statement period)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    date = Column(String, nullable=False)            # YYYY-MM-DD
    symbol = Column(String, nullable=False)
    description = Column(String)
    action = Column(String, nullable=False)          # bought, sold, dividend
    quantity = Column(Float, default=0)
    price = Column(Float, default=0)
    amount = Column(Float, default=0)                # total transaction amount
    realized_gain = Column(Float, default=0)         # for sells only
    account = relationship("Account")


class StockPlanGrant(Base):
    """RSU/ESPP grant from employer stock plan."""
    __tablename__ = "stock_plan_grants"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)           # YYYY-MM (statement period)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    grant_date = Column(String, nullable=False)      # YYYY-MM-DD
    grant_number = Column(String)
    grant_type = Column(String, nullable=False)      # RSU, ESPP, SO
    symbol = Column(String, nullable=False)
    quantity = Column(Float, default=0)              # unvested shares
    grant_price = Column(Float, default=0)
    market_price = Column(Float, default=0)
    estimated_value = Column(Float, default=0)       # pre-tax potential value
    account = relationship("Account")


class VestingEvent(Base):
    """RSU vesting or ESPP purchase event (security transfer)."""
    __tablename__ = "vesting_events"
    id = Column(Integer, primary_key=True)
    month = Column(String, nullable=False)           # YYYY-MM
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    date = Column(String, nullable=False)            # YYYY-MM-DD
    symbol = Column(String, nullable=False)
    quantity = Column(Float, default=0)
    amount = Column(Float, default=0)                # market value at vest
    account = relationship("Account")


class Mortgage(Base):
    """Mortgage loan details from statement."""
    __tablename__ = "mortgages"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    loan_number = Column(String)
    lender = Column(String)
    property_address = Column(String)
    interest_rate = Column(Float, default=0)
    principal_balance = Column(Float, default=0)
    monthly_payment = Column(Float, default=0)
    principal_portion = Column(Float, default=0)
    interest_portion = Column(Float, default=0)
    escrow_portion = Column(Float, default=0)
    escrow_balance = Column(Float, default=0)
    statement_date = Column(String)
    payment_due_date = Column(String)
    ytd_principal = Column(Float, default=0)
    ytd_interest = Column(Float, default=0)
    ytd_total = Column(Float, default=0)
    month = Column(String, nullable=False)           # YYYY-MM
    account = relationship("Account")


class HsaSummary(Base):
    """HSA account summary from statement."""
    __tablename__ = "hsa_summaries"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    month = Column(String, nullable=False)
    beginning_balance = Column(Float, default=0)
    ending_balance = Column(Float, default=0)
    investment_value = Column(Float, default=0)
    contributions = Column(Float, default=0)
    claims = Column(Float, default=0)
    interest = Column(Float, default=0)
    fees = Column(Float, default=0)
    period_return = Column(Float, default=0)
    ytd_return = Column(Float, default=0)
    account = relationship("Account")


class CsvProfile(Base):
    __tablename__ = "csv_profiles"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    institution = Column(String, nullable=False)
    column_mapping = Column(String, nullable=False)
    date_format = Column(String, default="%Y-%m-%d")
    account_type = Column(String, default="checking")


class PlaidItem(Base):
    """Linked bank via Plaid — stores access token per institution."""
    __tablename__ = "plaid_items"
    id = Column(Integer, primary_key=True)
    item_id = Column(String, nullable=False, unique=True)
    access_token = Column(String, nullable=False)
    institution_id = Column(String)
    institution_name = Column(String)
    needs_relink = Column(Integer, default=0)  # 1 = ITEM_LOGIN_REQUIRED, 0 = healthy
    transactions_cursor = Column(String)
