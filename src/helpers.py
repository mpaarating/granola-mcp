"""Helper functions for Granola MCP server."""

import base64
import hashlib
import json
import platform
import plistlib
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

# Mirror Granola Electron app identity headers; api.granola.ai rejects requests
# without them as `{"message": "Unsupported client"}` (HTTP 200 envelope).
# Header derivation adapted from granola-py-client (MIT — Anjor Kanekar,
# github.com/anjor/granola-py-client).
_DEFAULT_APP_VERSION = '7.220.0'

# Refresh the JWT this many seconds before its `exp` claim.
_TOKEN_REFRESH_BUFFER_SECONDS = 60

# In-process cache for the WorkOS access/refresh tokens. supabase.json is
# read-only ground truth — we never write back. Granola desktop is no longer
# observed to update supabase.json post-March-2026 DB encryption, so the MCP
# must self-refresh.
_TOKEN_CACHE: dict[str, str | int] = {}


def _read_supabase_tokens() -> dict:
    """Read the workos_tokens dict from Granola's supabase.json."""
    supabase_file = Path.home() / 'Library' / 'Application Support' / 'Granola' / 'supabase.json'
    if not supabase_file.exists():
        raise FileNotFoundError(
            f'Granola auth file not found at {supabase_file}. '
            'Is Granola installed and authenticated?'
        )
    with supabase_file.open() as f:
        data = json.load(f)
    if 'workos_tokens' not in data:
        raise ValueError('No workos_tokens found in Granola auth file')
    raw = data['workos_tokens']
    return json.loads(raw) if isinstance(raw, str) else raw


def _jwt_exp_seconds(access_token: str) -> int | None:
    """Decode JWT `exp` claim (unix seconds). None if undecodable."""
    parts = access_token.split('.')
    if len(parts) < 2:
        return None
    try:
        payload_b64 = parts[1] + '=' * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get('exp')
    return int(exp) if isinstance(exp, (int, float)) else None


def _refresh_workos_access_token(refresh_token: str) -> dict:
    """POST /v1/refresh-access-token with identity headers (no Authorization)."""
    headers = _identity_headers()
    headers['Content-Type'] = 'application/json'
    headers['Accept'] = 'application/json'
    response = httpx.post(
        'https://api.granola.ai/v1/refresh-access-token',
        json={'refresh_token': refresh_token},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    new_tokens = response.json()
    if not isinstance(new_tokens, dict) or not new_tokens.get('access_token'):
        raise ValueError(f'Refresh did not return an access_token: {new_tokens}')
    return new_tokens


def get_auth_token() -> str:
    """
    Return a fresh WorkOS access token, refreshing via /v1/refresh-access-token
    if the cached or file-resident token is at/near expiry.

    Raises:
        FileNotFoundError: If Granola data directory doesn't exist
        ValueError: If token data is malformed
    """
    cached_token = _TOKEN_CACHE.get('access_token')
    cached_exp = _TOKEN_CACHE.get('exp')
    now = int(time.time())

    if isinstance(cached_token, str) and isinstance(cached_exp, int) and cached_exp - now > _TOKEN_REFRESH_BUFFER_SECONDS:
        return cached_token

    file_tokens = _read_supabase_tokens()
    file_access = file_tokens.get('access_token')
    refresh_token = _TOKEN_CACHE.get('refresh_token') or file_tokens.get('refresh_token')

    if isinstance(file_access, str):
        file_exp = _jwt_exp_seconds(file_access)
        if file_exp is not None and file_exp - now > _TOKEN_REFRESH_BUFFER_SECONDS:
            _TOKEN_CACHE['access_token'] = file_access
            _TOKEN_CACHE['exp'] = file_exp
            if isinstance(refresh_token, str):
                _TOKEN_CACHE['refresh_token'] = refresh_token
            return file_access

    if not isinstance(refresh_token, str):
        raise ValueError('No refresh_token available to refresh the expired access_token')

    new_tokens = _refresh_workos_access_token(refresh_token)
    new_access = new_tokens['access_token']
    new_exp = _jwt_exp_seconds(new_access) or now + int(new_tokens.get('expires_in', 0) or 0)
    _TOKEN_CACHE['access_token'] = new_access
    _TOKEN_CACHE['exp'] = new_exp
    _TOKEN_CACHE['refresh_token'] = new_tokens.get('refresh_token', refresh_token)
    return new_access


def _granola_app_version() -> str:
    """Read CFBundleShortVersionString from the installed Granola.app, else fallback."""
    plist = Path('/Applications/Granola.app/Contents/Info.plist')
    if plist.exists():
        try:
            with plist.open('rb') as f:
                v = plistlib.load(f).get('CFBundleShortVersionString')
            if isinstance(v, str):
                return v
        except (OSError, plistlib.InvalidFileException):
            pass
    return _DEFAULT_APP_VERSION


def _hashed_device_id() -> str | None:
    """sha256(IOPlatformUUID) on macOS; None elsewhere or on lookup failure."""
    if platform.system() != 'Darwin':
        return None
    try:
        out = subprocess.run(
            ['ioreg', '-d2', '-c', 'IOPlatformExpertDevice'],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        if 'IOPlatformUUID' in line:
            parts = line.split('"')
            for i, p in enumerate(parts):
                if p == 'IOPlatformUUID' and i + 2 < len(parts):
                    return hashlib.sha256(parts[i + 2].encode('utf-8')).hexdigest()
    return None


def _os_version() -> str:
    """macOS product version, e.g. '15.0'. Empty string if unobtainable."""
    try:
        return (
            subprocess.run(
                ['sw_vers', '-productVersion'],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            or platform.mac_ver()[0]
        )
    except (subprocess.SubprocessError, OSError):
        return platform.mac_ver()[0] or ''


def _identity_headers() -> dict[str, str]:
    """Granola-Electron identity headers, sans Authorization. Sent on every api.granola.ai call."""
    headers = {
        'X-Client-Version': _granola_app_version(),
        'X-Granola-Platform': 'macOS',
        'X-Granola-Os-Version': _os_version(),
    }
    device_id = _hashed_device_id()
    if device_id:
        headers['X-Granola-Device-Id'] = device_id
    return headers


def get_auth_headers() -> dict[str, str]:
    """HTTP headers mimicking Granola's Electron app to bypass 'Unsupported client'."""
    return {
        'Authorization': f'Bearer {get_auth_token()}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        **_identity_headers(),
    }


def convert_utc_to_local(utc_timestamp: str | None) -> str | None:
    """
    Convert UTC ISO timestamp to system local timezone.

    Parses ISO 8601 UTC timestamp string, converts to system local time,
    and returns as ISO string with timezone offset.

    Args:
        utc_timestamp: ISO 8601 timestamp string ending in 'Z' (e.g., '2025-10-16T00:05:25.376Z')
                       or None

    Returns:
        ISO 8601 timestamp string in system local timezone (e.g., '2025-10-15T17:05:25.376-07:00')
        or None if input is None

    Example:
        >>> convert_utc_to_local('2025-10-16T00:05:25.376Z')
        '2025-10-15T17:05:25.376000-07:00'  # If system is in Pacific time
    """
    if utc_timestamp is None:
        return None

    # Parse UTC timestamp (replace 'Z' with '+00:00' for parsing)
    utc_dt = datetime.fromisoformat(utc_timestamp.replace('Z', '+00:00'))

    # Convert to system local timezone
    local_dt = utc_dt.astimezone()

    # Return as ISO format string with timezone offset
    return local_dt.isoformat()


def analyze_markdown_metadata(markdown: str) -> dict:
    """
    Extract structural and content metrics from markdown.

    Args:
        markdown: Markdown text to analyze

    Returns:
        Dictionary with section_count, bullet_count, heading_breakdown, word_count
    """
    lines = markdown.split('\n')

    # Count headings by level
    heading_breakdown = {'h1': 0, 'h2': 0, 'h3': 0}
    section_count = 0  # H3 headings

    for line in lines:
        if line.startswith('### '):
            heading_breakdown['h3'] += 1
            section_count += 1
        elif line.startswith('## '):
            heading_breakdown['h2'] += 1
        elif line.startswith('# '):
            heading_breakdown['h1'] += 1

    # Count bullet points (lines starting with - or * after optional whitespace)
    bullet_count = sum(1 for line in lines if re.match(r'^\s*[-*]\s', line))

    # Count words (split on whitespace, filter empty)
    words = markdown.split()
    word_count = len([w for w in words if w.strip()])

    return {
        'section_count': section_count,
        'bullet_count': bullet_count,
        'heading_breakdown': heading_breakdown,
        'word_count': word_count,
    }


def prosemirror_to_markdown(content: dict, depth: int = 0) -> str:
    """
    Convert ProseMirror JSON to Markdown.

    Handles nested lists with proper indentation.
    Supports headings, paragraphs, lists, code blocks, horizontal rules, and links.

    Args:
        content: ProseMirror JSON node
        depth: Current nesting depth for lists (used for indentation)
    """
    if not isinstance(content, dict):
        return ''

    node_type = content.get('type', '')

    # Document root
    if node_type == 'doc':
        children = content.get('content', [])
        return '\n\n'.join(prosemirror_to_markdown(child, depth) for child in children)

    # Headings
    if node_type == 'heading':
        level = content.get('attrs', {}).get('level', 1)
        text = extract_text(content)
        return f'{"#" * level} {text}'

    # Paragraph
    if node_type == 'paragraph':
        text = extract_text(content)
        return text if text else ''

    # Horizontal rule
    if node_type == 'horizontalRule':
        return '---'

    # Bullet list
    if node_type == 'bulletList':
        items = content.get('content', [])
        lines = []
        for item in items:
            if item.get('type') == 'listItem':
                item_lines = process_list_item(item, depth)
                lines.extend(item_lines)
        return '\n'.join(lines)

    # Ordered list
    if node_type == 'orderedList':
        items = content.get('content', [])
        lines = []
        for i, item in enumerate(items, 1):
            if item.get('type') == 'listItem':
                item_lines = process_list_item(item, depth, ordered=i)
                lines.extend(item_lines)
        return '\n'.join(lines)

    # Code block
    if node_type == 'codeBlock':
        text = extract_text(content)
        return f'```\n{text}\n```'

    # Fallback: extract text
    return extract_text(content)


def process_list_item(item: dict, depth: int, ordered: int | None = None) -> list[str]:
    """
    Process a list item with support for nested lists.

    Args:
        item: ProseMirror listItem node
        depth: Current nesting depth
        ordered: If provided, use numbered list format

    Returns:
        List of markdown lines for this item
    """
    indent = '  ' * depth
    bullet = f'{ordered}.' if ordered else '-'
    lines = []

    item_content = item.get('content', [])
    first_line_parts = []
    nested_content = []

    for node in item_content:
        node_type = node.get('type', '')

        # Paragraph content goes on the same line as the bullet
        if node_type == 'paragraph':
            text = extract_text(node)
            if text:
                first_line_parts.append(text)

        # Nested lists get indented on subsequent lines
        elif node_type in ['bulletList', 'orderedList']:
            nested_md = prosemirror_to_markdown(node, depth + 1)
            if nested_md:
                nested_content.append(nested_md)

    # Build the first line with the bullet
    first_line_text = ' '.join(first_line_parts)
    lines.append(f'{indent}{bullet} {first_line_text}')

    # Add nested content
    for nested in nested_content:
        lines.append(nested)

    return lines


def extract_text(node: dict) -> str:
    """Recursively extract all text from a ProseMirror node."""
    if isinstance(node, str):
        return node

    if not isinstance(node, dict):
        return ''

    # Direct text node
    if node.get('type') == 'text':
        text = node.get('text', '')
        # Handle marks (bold, italic, links, etc.)
        marks = node.get('marks', [])
        for mark in marks:
            mark_type = mark.get('type')
            if mark_type == 'bold':
                text = f'**{text}**'
            elif mark_type == 'italic':
                text = f'*{text}*'
            elif mark_type == 'code':
                text = f'`{text}`'
            elif mark_type == 'link':
                href = mark.get('attrs', {}).get('href', '')
                if href:
                    text = f'[{text}]({href})'
        return text

    # Recurse through children
    content = node.get('content', [])
    texts = [extract_text(child) for child in content]

    # Join with space for inline, newline for block
    node_type = node.get('type', '')
    if node_type in ['paragraph', 'listItem']:
        # Join with space, but normalize multiple spaces
        # This handles cases like "text: " + "[link]" -> "text: [link]" not "text:  [link]"
        result = ' '.join(text for text in texts if text)
        # Normalize multiple spaces to single space
        import re

        result = re.sub(r' +', ' ', result)
        return result
    else:
        return ''.join(texts)
