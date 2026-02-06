"""Helper functions for Granola MCP server."""

import json
import re
from datetime import datetime
from pathlib import Path


def get_auth_token() -> str:
    """
    Read WorkOS access token from Granola's local storage.

    Raises:
        FileNotFoundError: If Granola data directory doesn't exist
        ValueError: If token data is malformed
    """
    granola_dir = Path.home() / 'Library' / 'Application Support' / 'Granola'
    supabase_file = granola_dir / 'supabase.json'

    if not supabase_file.exists():
        raise FileNotFoundError(
            f'Granola auth file not found at {supabase_file}. '
            'Is Granola installed and authenticated?'
        )

    with open(supabase_file) as f:
        data = json.load(f)

    if 'workos_tokens' not in data:
        raise ValueError('No workos_tokens found in Granola auth file')

    tokens = json.loads(data['workos_tokens'])

    if 'access_token' not in tokens:
        raise ValueError('No access_token in workos_tokens')

    return tokens['access_token']


def get_auth_headers() -> dict[str, str]:
    """Get HTTP headers with authentication."""
    token = get_auth_token()
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
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
