"""Self-refreshing Granola authentication.

Granola migrated its local token storage from plaintext ``supabase.json`` to an
encrypted ``supabase.json.enc`` (key wrapped via macOS Keychain / ``storage.dek``),
which broke the old "read the access token off disk" approach.

Rather than decrypt Granola's at-rest store (fragile, re-breaks on every Granola
update), this module holds *our own* long-lived ``refresh_token`` and mints fresh
access tokens against Granola's backend refresh endpoint. The token lives in
``~/.granola-mcp/auth.json``, independent of whatever Granola does on disk.

Bootstrap the refresh token once with ``login.py`` (see that script). After that,
access tokens are refreshed automatically as they expire.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

GRANOLA_API = 'https://api.granola.ai'
REFRESH_URL = f'{GRANOLA_API}/v1/refresh-access-token'

CONFIG_DIR = Path.home() / '.granola-mcp'
AUTH_FILE = CONFIG_DIR / 'auth.json'

# Granola's own (now-stale) plaintext token file, used only as a one-time
# bootstrap source if we have no auth.json yet and it still holds a live token.
LEGACY_SUPABASE = (
    Path.home() / 'Library' / 'Application Support' / 'Granola' / 'supabase.json'
)

# Refresh a little before actual expiry to avoid racing the clock.
EXPIRY_BUFFER_SEC = 120
# Fallback lifetime when the refresh response omits expires_in.
DEFAULT_EXPIRES_IN_SEC = 3600


class GranolaAuthError(RuntimeError):
    """Raised when we cannot obtain a usable access token."""


def _load_auth() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    try:
        return json.loads(AUTH_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_auth(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write atomically with owner-only permissions — this file holds a credential.
    tmp = AUTH_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def _bootstrap_refresh_token() -> str | None:
    """Best-effort: lift a refresh_token from Granola's legacy plaintext file.

    Only reached when ``auth.json`` does not exist yet. The legacy file is usually
    stale (Granola stopped refreshing it after the encrypted migration), so this may
    yield a dead token — in which case the user must run ``login.py``.
    """
    if not LEGACY_SUPABASE.exists():
        return None
    try:
        data = json.loads(LEGACY_SUPABASE.read_text())
        tokens = json.loads(data['workos_tokens'])
        return tokens.get('refresh_token')
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _refresh(refresh_token: str) -> dict:
    """Exchange a refresh_token for a fresh token bundle.

    Returns a normalized auth dict ready to persist. Raises GranolaAuthError if the
    refresh token is rejected (Granola returns 401 ``logout_user`` for dead tokens).

    Sends Granola's Electron identity headers (X-Client-Version etc.) — the backend
    rejects unrecognized clients. Header derivation is shared with normal API calls
    via helpers._identity_headers(); imported lazily to avoid a circular import.
    """
    import httpx  # lazy import so the pure logic stays importable without deps

    from granola_mcp.helpers import _identity_headers

    headers = _identity_headers()
    headers['Content-Type'] = 'application/json'
    headers['Accept'] = 'application/json'

    try:
        response = httpx.post(
            REFRESH_URL,
            json={'refresh_token': refresh_token},
            headers=headers,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise GranolaAuthError(f'Network error refreshing Granola token: {exc}') from exc

    if response.status_code == 401:
        raise GranolaAuthError(
            'Granola rejected the refresh token (expired or revoked). '
            'Re-seed it with: python3 login.py'
        )
    if response.status_code != 200:
        raise GranolaAuthError(
            f'Unexpected {response.status_code} from refresh endpoint: '
            f'{response.text[:200]}'
        )

    body = response.json()
    access_token = body.get('access_token')
    if not access_token:
        raise GranolaAuthError(
            f'Refresh response had no access_token: {json.dumps(body)[:200]}'
        )

    return {
        'access_token': access_token,
        # Granola rotates refresh tokens; keep the new one if present, else reuse.
        'refresh_token': body.get('refresh_token', refresh_token),
        'expires_in': body.get('expires_in', DEFAULT_EXPIRES_IN_SEC),
        'obtained_at': time.time(),
    }


def _is_access_token_fresh(auth: dict) -> bool:
    token = auth.get('access_token')
    obtained_at = auth.get('obtained_at')
    expires_in = auth.get('expires_in')
    if not token or obtained_at is None or expires_in is None:
        return False
    return time.time() < (obtained_at + expires_in - EXPIRY_BUFFER_SEC)


def get_access_token() -> str:
    """Return a valid Granola access token, refreshing it if needed.

    Resolution order:
      1. Cached access token in ~/.granola-mcp/auth.json, if still fresh.
      2. Refresh using the stored refresh_token.
      3. One-time bootstrap from Granola's legacy plaintext file, then refresh.
    """
    auth = _load_auth()

    if auth and _is_access_token_fresh(auth):
        return auth['access_token']

    refresh_token = (auth or {}).get('refresh_token') or _bootstrap_refresh_token()
    if not refresh_token:
        raise GranolaAuthError(
            'No Granola refresh token available. Seed one with: python3 login.py'
        )

    refreshed = _refresh(refresh_token)
    _save_auth(refreshed)
    return refreshed['access_token']


def set_refresh_token(refresh_token: str) -> dict:
    """Validate and persist a refresh token (used by the login bootstrap script).

    Performs one refresh round-trip so we fail loudly on a bad token rather than
    silently writing garbage. Returns the persisted auth dict.
    """
    refresh_token = refresh_token.strip()
    if not refresh_token:
        raise GranolaAuthError('Empty refresh token.')
    refreshed = _refresh(refresh_token)
    _save_auth(refreshed)
    return refreshed
