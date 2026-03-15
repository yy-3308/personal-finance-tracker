import os
import shutil

import pytest

from database import get_session, init_db
from importers.importer import detect_csv_format, import_file, parse_csv, scan_import_folder
from models import Account, CsvProfile, Transaction

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def db_session(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    session = get_session(db_path)
    chase_profile = CsvProfile(
        name="Chase Checking", institution="Chase", account_type="checking",
        column_mapping='{"date": "Posting Date", "amount": "Amount", "description": "Description"}',
        date_format="%m/%d/%Y",
    )
    amex_profile = CsvProfile(
        name="Amex Credit", institution="Amex", account_type="credit_card",
        column_mapping='{"date": "Date", "amount": "Amount", "description": "Description"}',
        date_format="%m/%d/%Y",
    )
    session.add_all([chase_profile, amex_profile])
    session.commit()
    yield session
    session.close()


def test_detect_chase_format(db_session):
    filepath = os.path.join(FIXTURES, "chase_checking.csv")
    profile = detect_csv_format(filepath, db_session)
    assert profile is not None
    assert profile.institution == "Chase"


def test_detect_amex_format(db_session):
    filepath = os.path.join(FIXTURES, "amex_credit.csv")
    profile = detect_csv_format(filepath, db_session)
    assert profile is not None
    assert profile.institution == "Amex"


def test_parse_chase_csv(db_session):
    filepath = os.path.join(FIXTURES, "chase_checking.csv")
    profile = detect_csv_format(filepath, db_session)
    account = Account(name="Chase Checking", account_type="checking", institution="Chase")
    db_session.add(account)
    db_session.commit()
    transactions = parse_csv(filepath, profile, account.id)
    assert len(transactions) == 3
    assert transactions[0].amount == -45.50
    assert transactions[2].amount == 5000.00


def test_deduplication(db_session):
    filepath = os.path.join(FIXTURES, "chase_checking.csv")
    profile = detect_csv_format(filepath, db_session)
    account = Account(name="Chase Checking", account_type="checking", institution="Chase")
    db_session.add(account)
    db_session.commit()
    txns1 = import_file(filepath, profile, account.id, db_session)
    assert len(txns1) == 3
    txns2 = import_file(filepath, profile, account.id, db_session)
    assert len(txns2) == 0
    assert db_session.query(Transaction).count() == 3


def test_scan_import_folder(tmp_path, db_session):
    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    processed_dir = import_dir / "processed"
    processed_dir.mkdir()
    shutil.copy(os.path.join(FIXTURES, "chase_checking.csv"), str(import_dir))
    files = scan_import_folder(str(import_dir))
    assert len(files) == 1
    assert files[0].endswith(".csv")
