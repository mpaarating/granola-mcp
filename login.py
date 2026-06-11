#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "httpx",
# ]
# ///
"""One-time bootstrap for the Granola MCP self-refreshing auth.

The MCP server no longer reads Granola's local (now-encrypted) token store. Instead
it keeps its own refresh token in ~/.granola-mcp/auth.json and mints access tokens
itself. This script seeds that refresh token once.

Where to get the refresh token (you own this account — this is your own credential):
  1. Open the Granola web app at https://notes.granola.ai and sign in.
  2. Open DevTools -> Application -> Local Storage -> https://notes.granola.ai
  3. Find the "workos_tokens" entry and copy the value of its "refresh_token" field.
     (Alternatively, DevTools -> Network -> any api.granola.ai request -> find the
     refresh_token in a refresh-access-token call payload.)

Usage:
  python3 login.py            # prompts; paste the refresh token (input hidden)
  echo "<token>" | python3 login.py    # pipe it in (avoids shell history)

The token is validated against Granola's refresh endpoint before being saved, so a
bad paste fails immediately instead of silently breaking the server later.
"""

import sys
from pathlib import Path

# Make `src` importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.auth import AUTH_FILE, GranolaAuthError, set_refresh_token  # noqa: E402


def _read_token() -> str:
    if not sys.stdin.isatty():
        # Piped input — read the first non-empty line.
        for line in sys.stdin:
            line = line.strip()
            if line:
                return line
        return ''
    # Interactive — hide the input like a password prompt.
    import getpass

    return getpass.getpass('Paste Granola refresh token (input hidden): ')


def main() -> int:
    token = _read_token()
    if not token:
        print('No token provided. Aborting.', file=sys.stderr)
        return 1

    try:
        result = set_refresh_token(token)
    except GranolaAuthError as exc:
        print(f'Failed to validate refresh token: {exc}', file=sys.stderr)
        return 1

    expires_in = result.get('expires_in')
    print(f'Success. Saved refresh token to {AUTH_FILE}.')
    print(f'Minted an access token (valid ~{expires_in}s); it will auto-refresh.')
    print('Restart the Granola MCP (/mcp) to pick up the new auth.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
