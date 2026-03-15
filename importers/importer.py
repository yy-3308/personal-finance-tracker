import csv
import json
import os
from datetime import datetime

from models import CsvProfile, Transaction


def scan_import_folder(import_folder):
    """Return list of CSV files in import folder (not in processed/)."""
    files = []
    for f in os.listdir(import_folder):
        if f.lower().endswith(".csv") and os.path.isfile(os.path.join(import_folder, f)):
            files.append(os.path.join(import_folder, f))
    return sorted(files)


def detect_csv_format(filepath, db_session):
    """Match a CSV file to a known CsvProfile by comparing column headers."""
    with open(filepath, "r") as f:
        reader = csv.reader(f)
        headers = next(reader)
    headers_set = set(h.strip() for h in headers)

    profiles = db_session.query(CsvProfile).all()
    best_match = None
    best_score = 0
    for profile in profiles:
        mapping = json.loads(profile.column_mapping)
        expected_cols = set(mapping.values())
        score = len(expected_cols & headers_set) / len(expected_cols)
        if score > best_score:
            best_score = score
            best_match = profile
    return best_match if best_score >= 0.5 else None


def parse_csv(filepath, profile, account_id):
    """Parse CSV into Transaction objects using profile's column mapping."""
    mapping = json.loads(profile.column_mapping)
    transactions = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get(mapping["date"], "").strip()
            amount_str = row.get(mapping["amount"], "0").strip()
            description = row.get(mapping.get("description", ""), "").strip()
            category = row.get(mapping.get("category", ""), "Uncategorized").strip() or "Uncategorized"

            try:
                date = datetime.strptime(date_str, profile.date_format).strftime("%Y-%m-%d")
                amount = float(amount_str.replace(",", ""))
            except (ValueError, AttributeError):
                continue

            txn = Transaction(
                date=date, amount=amount, category=category,
                description=description, account_id=account_id,
            )
            transactions.append(txn)
    return transactions


def import_file(filepath, profile, account_id, db_session):
    """Parse CSV and insert only non-duplicate transactions. Returns list of new transactions."""
    transactions = parse_csv(filepath, profile, account_id)
    existing_fps = set(
        fp for (fp,) in db_session.query(Transaction.fingerprint)
        .filter(Transaction.account_id == account_id).all()
    )
    new_txns = [t for t in transactions if t.fingerprint not in existing_fps]
    db_session.add_all(new_txns)
    db_session.commit()
    return new_txns


def move_to_processed(filepath, processed_folder):
    """Move imported file to processed folder."""
    os.makedirs(processed_folder, exist_ok=True)
    dest = os.path.join(processed_folder, os.path.basename(filepath))
    os.rename(filepath, dest)
