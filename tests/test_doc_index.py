"""Tests for document-index rules (granola_mcp/doc_index.py).

Pure-logic tests: no network.

Run:  python3 tests/test_doc_index.py
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from granola_mcp import doc_index  # noqa: E402


def entry(updated_at='2026-10-01T17:00:00Z', owner=None, shared=None):
    return NS(updated_at=updated_at, owner=owner, shared=shared)


def doc(notes=None, notes_markdown=None, panel_content=None):
    panel = NS(content=panel_content) if panel_content is not None else None
    return NS(notes=notes, notes_markdown=notes_markdown, last_viewed_panel=panel)


class Source(unittest.TestCase):
    def test_all_includes_owned_and_shared_but_not_workspace(self):
        self.assertTrue(doc_index.matches_source(entry(owner=True), 'all'))
        self.assertTrue(doc_index.matches_source(entry(shared=True), 'all'))
        self.assertFalse(doc_index.matches_source(entry(), 'all'))

    def test_owned_excludes_workspace_only_documents(self):
        self.assertTrue(doc_index.matches_source(entry(owner=True), 'owned'))
        self.assertFalse(doc_index.matches_source(entry(), 'owned'))
        self.assertFalse(doc_index.matches_source(entry(shared=True), 'owned'))

    def test_workspace_selects_entries_with_neither_flag(self):
        self.assertTrue(doc_index.matches_source(entry(), 'workspace'))
        self.assertFalse(doc_index.matches_source(entry(owner=True), 'workspace'))


class Freshness(unittest.TestCase):
    def test_fresh_when_index_timestamp_unchanged(self):
        self.assertTrue(doc_index.is_cached_doc_fresh('t1', entry(updated_at='t1')))

    def test_stale_when_index_reports_newer_update(self):
        self.assertFalse(doc_index.is_cached_doc_fresh('t1', entry(updated_at='t2')))

    def test_kept_when_document_absent_from_index(self):
        self.assertTrue(doc_index.is_cached_doc_fresh('t1', None))


class HasNotes(unittest.TestCase):
    summary = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Decided X'}]}]}
    empty = {'type': 'doc', 'content': [{'type': 'paragraph'}]}

    def test_ai_summary_panel_counts_as_notes(self):
        self.assertTrue(doc_index.doc_has_notes(doc(panel_content=self.summary)))

    def test_html_panel_counts_as_notes(self):
        self.assertTrue(doc_index.doc_has_notes(doc(panel_content='<p>Decided X</p>')))

    def test_empty_panel_and_notes_is_false(self):
        self.assertFalse(doc_index.doc_has_notes(doc(notes=self.empty, panel_content=self.empty)))

    def test_markdown_notes_count(self):
        self.assertTrue(doc_index.doc_has_notes(doc(notes_markdown='- item')))

    def test_whitespace_markdown_is_false(self):
        self.assertFalse(doc_index.doc_has_notes(doc(notes_markdown='  \n')))


class CreatedSincePrefilter(unittest.TestCase):
    def test_skips_entries_last_updated_before_cutoff(self):
        self.assertFalse(doc_index.may_be_created_since(entry(updated_at='2026-09-24T17:00:00Z'), '2026-10-01'))

    def test_keeps_entries_updated_on_or_after_cutoff(self):
        self.assertTrue(doc_index.may_be_created_since(entry(updated_at='2026-10-01T00:05:00Z'), '2026-10-01'))

    def test_keeps_everything_without_cutoff(self):
        self.assertTrue(doc_index.may_be_created_since(entry(updated_at='2020-01-01T00:00:00Z'), None))


if __name__ == '__main__':
    unittest.main()
