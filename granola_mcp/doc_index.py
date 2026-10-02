"""Pure helpers over the get-document-set index and fetched documents.

Kept free of I/O so the freshness, ownership, and has-notes rules are testable
without the network. Entries and documents are duck-typed (pydantic models in
production, simple namespaces in tests).
"""

from __future__ import annotations

from typing import Literal

# The index is cached for this long. Long enough to batch a burst of tool calls,
# short enough that a meeting recorded mid-session shows up.
DOCUMENT_SET_TTL_SECONDS = 300

Source = Literal['all', 'owned', 'shared', 'workspace']


def entry_kind(entry) -> Literal['owned', 'shared', 'workspace']:
    """Classify an index entry.

    Granola sets `owner` on documents you recorded and `shared` on documents
    shared with you. Entries with neither are other people's notes that are only
    visible through your workspace; they are not meetings you attended.
    """
    if entry.owner:
        return 'owned'
    if entry.shared:
        return 'shared'
    return 'workspace'


def matches_source(entry, source: Source) -> bool:
    kind = entry_kind(entry)
    if source == 'all':
        return kind in ('owned', 'shared')
    return kind == source


def is_cached_doc_fresh(cached_index_updated_at: str | None, entry) -> bool:
    """A cached document is fresh while the index still reports the same updated_at.

    Documents missing from the index (e.g. resolved from a sharing link) have no
    signal to compare against, so the cached copy is kept.
    """
    if entry is None:
        return True
    return cached_index_updated_at == entry.updated_at


def _prosemirror_has_text(node) -> bool:
    if node is None:
        return False
    if isinstance(node, str):
        return bool(node.strip())
    text = getattr(node, 'text', None)
    if text is None and isinstance(node, dict):
        text = node.get('text')
    if text and text.strip():
        return True
    children = getattr(node, 'content', None)
    if children is None and isinstance(node, dict):
        children = node.get('content')
    return any(_prosemirror_has_text(child) for child in children or ())


def doc_has_notes(doc) -> bool:
    """True when the document has manual notes or a non-empty AI summary panel.

    Newer meetings leave notes/notes_markdown empty and keep the AI summary in
    the last-viewed panel, so checking only the notes fields reports False for
    almost every meeting.
    """
    if doc.notes_markdown and doc.notes_markdown.strip():
        return True
    if _prosemirror_has_text(doc.notes):
        return True
    panel = doc.last_viewed_panel
    return bool(panel) and _prosemirror_has_text(panel.content)


def may_be_created_since(entry, created_at_gte: str | None) -> bool:
    """Cheap pre-filter for a created_at >= YYYY-MM-DD (UTC) filter, using only the index.

    A document is never updated before it was created, so an index updated_at
    earlier than the cutoff rules it out without a batch fetch.
    """
    if not created_at_gte:
        return True
    return entry.updated_at[:10] >= created_at_gte
