"""Dropbox OAuth2 authentication and credential management."""

import json
import pathlib
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import dropbox
import requests

# Standard paths for credentials
DROPBOX_TOKEN_PATH = pathlib.Path.home() / ".magpie" / "dropbox_token.json"
DROPBOX_APP_CONFIG_PATH = pathlib.Path.home() / ".magpie" / "dropbox_app.json"


def get_dropbox_client(
    token_path: Optional[pathlib.Path] = None,
    app_config_path: Optional[pathlib.Path] = None,
) -> dropbox.Dropbox:
    """Get valid Dropbox API client, authenticating if necessary.

    Args:
        token_path: Path to cached token JSON. Defaults to ~/.magpie/dropbox_token.json
        app_config_path: Path to app config JSON with app_key and app_secret.
                        Defaults to ~/.magpie/dropbox_app.json

    Returns:
        Valid dropbox.Dropbox client object

    Raises:
        FileNotFoundError: If app_config_path doesn't exist or token can't be refreshed.
    """
    token_path = token_path or DROPBOX_TOKEN_PATH
    app_config_path = app_config_path or DROPBOX_APP_CONFIG_PATH

    # If we have a cached token, try to load and use it
    if token_path.exists():
        try:
            token_data = json.loads(token_path.read_text())
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            stored_app_key = token_data.get("app_key")

            if access_token:
                # Need app_key and app_secret for token refresh
                app_key_to_use = stored_app_key
                app_secret_to_use = None
                if app_config_path.exists():
                    config = json.loads(app_config_path.read_text())
                    app_key_to_use = app_key_to_use or config.get("app_key")
                    app_secret_to_use = config.get("app_secret")

                # Only pass refresh_token if we have one
                kwargs = {
                    "oauth2_access_token": access_token,
                    "app_key": app_key_to_use,
                }
                if app_secret_to_use:
                    kwargs["app_secret"] = app_secret_to_use
                if refresh_token:
                    kwargs["oauth2_refresh_token"] = refresh_token

                client = dropbox.Dropbox(**kwargs)
                # Test the token by making a simple API call
                try:
                    client.users_get_current_account()
                    return client
                except dropbox.exceptions.AuthError:
                    # Token expired or invalid, try to refresh if we have a refresh token
                    if refresh_token and app_config_path.exists():
                        return _refresh_and_get_client(app_config_path, token_path, refresh_token)
                    raise
        except (json.JSONDecodeError, KeyError):
            pass

    # No valid cached token — need to run OAuth flow
    if not app_config_path.exists():
        raise FileNotFoundError(
            f"Dropbox app config not found at {app_config_path}\n"
            "Create a Dropbox app at https://www.dropbox.com/developers and save its config:\n"
            '{"app_key": "...", "app_secret": "..."}'
        )

    return _run_oauth_flow(app_config_path, token_path)


def _refresh_and_get_client(
    app_config_path: pathlib.Path,
    token_path: pathlib.Path,
    refresh_token: str,
) -> dropbox.Dropbox:
    """Refresh an expired access token using the refresh token."""
    config = json.loads(app_config_path.read_text())
    app_key = config.get("app_key")
    app_secret = config.get("app_secret")

    if not app_key or not app_secret:
        raise ValueError("app_config must contain 'app_key' and 'app_secret'")

    try:
        response = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": app_key,
                "client_secret": app_secret,
            },
        )
        result = response.json()
        if not response.ok:
            error_msg = result.get("error_description", result.get("error", response.text))
            raise Exception(f"Dropbox token refresh error: {error_msg}")

        access_token = result.get("access_token")
        new_refresh_token = result.get("refresh_token", refresh_token)

        # Save the new token (include app_key for future refresh cycles)
        token_data = {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "app_key": app_key,
        }
        _save_token(token_data, token_path)
        return dropbox.Dropbox(
            oauth2_access_token=access_token,
            oauth2_refresh_token=new_refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )
    except Exception as e:
        raise Exception(f"Failed to refresh Dropbox token: {e}")


def _run_oauth_flow(
    app_config_path: pathlib.Path,
    token_path: pathlib.Path,
) -> dropbox.Dropbox:
    """Run the Dropbox OAuth2 code-based flow."""
    config = json.loads(app_config_path.read_text())
    app_key = config.get("app_key")
    app_secret = config.get("app_secret")

    if not app_key or not app_secret:
        raise ValueError("app_config must contain 'app_key' and 'app_secret'")

    # Build the authorization URL
    auth_url = "https://www.dropbox.com/oauth2/authorize?" + urlencode({
        "client_id": app_key,
        "response_type": "code",
        "token_access_type": "offline",
    })

    print(f"\n[blue]Open this URL in your browser:[/blue]\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Ask user for the authorization code
    auth_code = input("Paste the authorization code here: ").strip()

    # Exchange code for tokens
    try:
        response = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "code": auth_code,
                "grant_type": "authorization_code",
                "client_id": app_key,
                "client_secret": app_secret,
            },
        )
        result = response.json()
        if not response.ok:
            error_msg = result.get("error_description", result.get("error", response.text))
            print(f"\n[red]Debug: Status {response.status_code}, Response: {result}[/red]")
            raise Exception(f"Dropbox OAuth error: {error_msg}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to reach Dropbox OAuth endpoint: {e}")
    except Exception as e:
        raise Exception(f"Failed to complete Dropbox OAuth: {e}")

    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")

    if not access_token:
        raise Exception("No access token in OAuth response")

    # Debug: show what we got
    print(f"\n[blue]Debug: OAuth response keys: {list(result.keys())}[/blue]")
    print(f"[blue]Debug: Has refresh_token: {bool(refresh_token)}[/blue]")

    # Save the token for future use (include app_key for token refresh)
    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "app_key": app_key,
    }
    _save_token(token_data, token_path)

    # Pass credentials needed for token refresh
    kwargs = {
        "oauth2_access_token": access_token,
        "app_key": app_key,
        "app_secret": app_secret,
    }
    if refresh_token:
        kwargs["oauth2_refresh_token"] = refresh_token

    return dropbox.Dropbox(**kwargs)


def _save_token(token_data: dict, token_path: pathlib.Path) -> None:
    """Save token data to a token file."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token_data, indent=2))
    token_path.chmod(0o600)  # Restrict to owner only
