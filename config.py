import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    DB_PATH = os.path.join(BASE_DIR, "data", "finance.db")
    IMPORT_FOLDER = os.path.expanduser("~/Downloads/spend_tracker")
    PROCESSED_FOLDER = os.path.expanduser("~/Downloads/spend_tracker/processed")
    SECRET_KEY = "dev-local-only"

    PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
    PLAID_SECRET = os.getenv("PLAID_SECRET")
    PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")
