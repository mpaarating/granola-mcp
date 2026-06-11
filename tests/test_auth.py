"""Tests for the self-refreshing auth manager (src/auth.py).

Pure-logic tests: no network. ``_refresh`` is monkeypatched and all credential
paths are redirected into a temp dir, so the real ~/.granola-mcp is never touched.

Run:  python3 tests/test_auth.py
"""

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth  # noqa: E402


class AuthTokenResolution(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Redirect every credential path into the temp dir.
        auth.CONFIG_DIR = tmp / '.granola-mcp'
        auth.AUTH_FILE = auth.CONFIG_DIR / 'auth.json'
        auth.LEGACY_SUPABASE = tmp / 'supabase.json'
        self._real_refresh = auth._refresh
        self.refresh_calls = []

    def tearDown(self):
        auth._refresh = self._real_refresh
        self._tmp.cleanup()

    def _stub_refresh(self, access='fresh-access', expires_in=3600):
        def _fake(refresh_token):
            self.refresh_calls.append(refresh_token)
            return {
                'access_token': access,
                'refresh_token': refresh_token,
                'expires_in': expires_in,
                'obtained_at': time.time(),
            }

        auth._refresh = _fake

    def test_returns_cached_token_when_fresh_without_refreshing(self):
        auth._save_auth(
            {
                'access_token': 'cached',
                'refresh_token': 'r',
                'expires_in': 3600,
                'obtained_at': time.time(),
            }
        )
        self._stub_refresh()
        self.assertEqual(auth.get_access_token(), 'cached')
        self.assertEqual(self.refresh_calls, [], 'should not refresh a fresh token')

    def test_refreshes_when_access_token_expired(self):
        auth._save_auth(
            {
                'access_token': 'stale',
                'refresh_token': 'r-token',
                'expires_in': 3600,
                # Obtained well in the past -> expired.
                'obtained_at': time.time() - 99999,
            }
        )
        self._stub_refresh(access='new-access')
        self.assertEqual(auth.get_access_token(), 'new-access')
        self.assertEqual(self.refresh_calls, ['r-token'])
        # New token bundle is persisted.
        saved = json.loads(auth.AUTH_FILE.read_text())
        self.assertEqual(saved['access_token'], 'new-access')

    def test_expiry_buffer_treats_near_expiry_as_stale(self):
        # Token expires in less than the buffer window -> considered stale.
        auth_dict = {
            'access_token': 't',
            'refresh_token': 'r',
            'expires_in': 100,
            'obtained_at': time.time() - (100 - auth.EXPIRY_BUFFER_SEC + 10),
        }
        self.assertFalse(auth._is_access_token_fresh(auth_dict))

    def test_bootstraps_refresh_token_from_legacy_file(self):
        auth.LEGACY_SUPABASE.write_text(
            json.dumps({'workos_tokens': json.dumps({'refresh_token': 'legacy-r'})})
        )
        self._stub_refresh(access='bootstrapped')
        self.assertEqual(auth.get_access_token(), 'bootstrapped')
        self.assertEqual(self.refresh_calls, ['legacy-r'])

    def test_raises_when_no_token_anywhere(self):
        self._stub_refresh()
        with self.assertRaises(auth.GranolaAuthError):
            auth.get_access_token()

    def test_auth_file_written_with_owner_only_perms(self):
        auth._save_auth({'access_token': 'a', 'refresh_token': 'r'})
        mode = auth.AUTH_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == '__main__':
    unittest.main(verbosity=2)
