from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]

CLIENT_SECRET_FILE = ROOT / "credentials" / "google-oauth.json"
TOKEN_FILE = ROOT / "credentials" / "google-token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_google_credentials() -> Credentials:
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES,
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRET_FILE,
            SCOPES,
        )

        creds = flow.run_local_server(
            port=0,
            open_browser=True,
        )

        TOKEN_FILE.write_text(
            creds.to_json(),
            encoding="utf-8",
        )

    return creds


def get_drive_service():
    creds = get_google_credentials()

    return build(
        "drive",
        "v3",
        credentials=creds,
    )


def get_sheets_service():
    creds = get_google_credentials()

    return build(
        "sheets",
        "v4",
        credentials=creds,
    )