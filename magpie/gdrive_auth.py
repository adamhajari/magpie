"""Google Drive OAuth2 authentication and credential management."""

import json
import pathlib
import webbrowser
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# OAuth2 scopes for Google Drive indexing and executor operations
# Full read-write access needed for both indexing and future executor features (move to trash, copy files)
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Standard paths for credentials
GDRIVE_TOKEN_PATH = pathlib.Path.home() / ".magpie" / "gdrive_token.json"
GDRIVE_CREDENTIALS_PATH = pathlib.Path.home() / ".magpie" / "gdrive_client_secret.json"


def get_credentials(
    token_path: Optional[pathlib.Path] = None,
    credentials_path: Optional[pathlib.Path] = None,
) -> Credentials:
    """Get valid Google Drive API credentials, authenticating if necessary.

    Args:
        token_path: Path to cached token JSON. Defaults to ~/.magpie/gdrive_token.json
        credentials_path: Path to OAuth credentials JSON from Google Cloud Console.
                         Defaults to ~/.magpie/gdrive_client_secret.json

    Returns:
        Valid google.oauth2.credentials.Credentials object

    Raises:
        FileNotFoundError: If credentials_path doesn't exist and token can't be refreshed.
    """
    token_path = token_path or GDRIVE_TOKEN_PATH
    credentials_path = credentials_path or GDRIVE_CREDENTIALS_PATH

    # If we have a cached token, try to load and refresh it
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds, token_path)
        return creds

    # No cached token — need to run OAuth flow
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Google OAuth credentials not found at {credentials_path}\n"
            "Download your OAuth 2.0 Client ID JSON from Google Cloud Console and save it there.\n"
            "See the Google Drive setup guide in docs/ for instructions."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    # Save the token for future use
    _save_token(creds, token_path)
    return creds


def _save_token(creds: Credentials, token_path: pathlib.Path) -> None:
    """Save credentials to a token file."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)  # Restrict to owner only
