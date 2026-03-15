"""Plaid API client — configured from environment variables."""
import certifi
import os
import plaid
from plaid.api import plaid_api

# macOS Python 3.12 doesn't ship with SSL certs — use certifi's bundle
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "development": "https://development.plaid.com",
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
