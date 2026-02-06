#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#   "aiocache",
#   "fastmcp>=2.12.5",
#   "httpx",
#   "markdownify",
#   "pydantic>=2.0",
#   "pydevd-pycharm~=241.18034.82",
# ]
# ///
#
# Granola MCP Server
#
# Provides access to Granola meeting notes and data via MCP.
# Architecture: Reads auth tokens from local Granola app data.

from __future__ import annotations

import argparse
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from aiocache import Cache, cached
import markdownify
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from src.helpers import (
    analyze_markdown_metadata,
    convert_utc_to_local,
    get_auth_headers,
    prosemirror_to_markdown,
)
from src.logging import DualLogger
from src.models import (
    AttendeeUpdate,
    BatchDocumentsResponse,
    CreateWorkspaceResult,
    DeleteMeetingResult,
    DeleteWorkspaceResult,
    DocumentPanel,
    DocumentSetResponse,
    ListWorkspacesResult,
    MeetingList,
    MeetingListItem,
    MeetingListsResult,
    NoteDownloadResult,
    PrivateNoteDownloadResult,
    ResolveUrlResult,
    TranscriptDownloadResult,
    TranscriptSegment,
    UpdateMeetingResult,
    WorkspaceInfo,
    WorkspacesResponse,
)

# =============================================================================
# Global State
# =============================================================================

_temp_dir: tempfile.TemporaryDirectory | None = None
_export_dir: Path | None = None
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(server):
    """Manage resources - cleanup on shutdown."""
    global _temp_dir, _export_dir, _http_client

    # Initialize temp directory for exports
    _temp_dir = tempfile.TemporaryDirectory()
    _export_dir = Path(_temp_dir.name)

    # Initialize HTTP client
    _http_client = httpx.AsyncClient(timeout=30.0)

    try:
        yield {}
    finally:
        # Cleanup
        if _http_client:
            await _http_client.aclose()
        if _temp_dir:
            _temp_dir.cleanup()


mcp = FastMCP('granola', lifespan=lifespan)

# =============================================================================
# Helper Functions
# =============================================================================

# Compiled regex for extracting document IDs from /d/{id} URLs
_DOCUMENT_ID_PATTERN = re.compile(r'/d/([a-f0-9-]+)')


def _extract_document_id(url: str) -> str | None:
    """Extract document ID from a URL containing /d/{id} path."""
    match = _DOCUMENT_ID_PATTERN.search(url)
    return match.group(1) if match else None


@cached(ttl=None, cache=Cache.MEMORY)
async def _get_document_set_cached() -> DocumentSetResponse:
    """
    Fetch the lightweight document index (owned + shared + deleted).

    Returns all document IDs with ownership flags and updated_at timestamps.
    Cached for the lifetime of the MCP server session (cleared on restart).
    """
    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/get-document-set'
    response = await _http_client.post(url, json={}, headers=headers)
    response.raise_for_status()
    return DocumentSetResponse.model_validate(response.json())


# Per-document cache for batch fetch results (session lifetime)
_document_cache: dict[str, object] = {}


async def _get_documents_by_ids(document_ids: list[str]) -> list:
    """
    Fetch documents by IDs, using per-document cache.

    Checks the cache first, only fetches uncached IDs from the API.
    Stores results back into the cache for future calls.

    Args:
        document_ids: List of document IDs to fetch

    Returns:
        List of GranolaDocument objects (order not guaranteed)
    """
    # Split into cached and uncached
    cached_docs = []
    uncached_ids = []
    for doc_id in document_ids:
        if doc_id in _document_cache:
            cached_docs.append(_document_cache[doc_id])
        else:
            uncached_ids.append(doc_id)

    if not uncached_ids:
        return cached_docs

    # Fetch uncached in chunks of 200
    fetched_docs = []
    for chunk_start in range(0, len(uncached_ids), 200):
        chunk_ids = uncached_ids[chunk_start : chunk_start + 200]
        headers = get_auth_headers()
        response = await _http_client.post(
            'https://api.granola.ai/v1/get-documents-batch',
            json={'document_ids': chunk_ids},
            headers=headers,
        )
        response.raise_for_status()
        data = BatchDocumentsResponse.model_validate(response.json())
        for doc in data.docs:
            _document_cache[doc.id] = doc
            fetched_docs.append(doc)

    return cached_docs + fetched_docs


async def _get_document_by_id(document_id: str):
    """
    Fetch a single document by ID. Works for both owned and shared documents.

    Args:
        document_id: Granola document ID

    Returns:
        GranolaDocument

    Raises:
        ValueError: If document not found
    """
    docs = await _get_documents_by_ids([document_id])
    if not docs:
        raise ValueError(f'Document {document_id} not found')
    return docs[0]


@cached(ttl=86400, cache=Cache.MEMORY)  # 24 hour TTL - token mappings are stable
async def _resolve_sharing_token(token: str) -> str:
    """
    Resolve a sharing token to document ID via HTTP redirect.

    The /t/{token} URLs redirect to /d/{document_id} URLs.
    Results are cached for 24 hours since mappings are stable.

    Args:
        token: Sharing token from /t/ URL

    Returns:
        Document ID

    Raises:
        ValueError: If token is invalid or redirect fails
    """
    url = f'https://notes.granola.ai/t/{token}'

    response = await _http_client.get(
        url,
        follow_redirects=False,
        timeout=10.0,
    )

    # Handle all 3xx redirects
    if 300 <= response.status_code < 400:
        location = response.headers.get('location', '')
        document_id = _extract_document_id(location)
        if document_id:
            return document_id
        raise ValueError(f'Unexpected redirect location: {location}')

    if response.status_code == 404:
        raise ValueError(f'Sharing token not found: {token}')

    raise ValueError(f'Unexpected response status: {response.status_code}')


# =============================================================================
# MCP Tools
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(title='List Meetings', readOnlyHint=True))
async def list_meetings(
    # Filter parameters
    title_contains: str | None = None,
    case_sensitive: bool = False,
    list_id: str | None = None,
    created_at_gte: str | None = None,
    created_at_lte: str | None = None,
    source: Literal['all', 'owned', 'shared'] = 'all',
    # Control parameters
    limit: int = 20,
    include_participants: bool = False,
) -> list[MeetingListItem]:
    """
    List Granola meetings with optional filtering.

    Uses the document set index to discover all meetings (owned + shared),
    then batch-fetches full details. Results are cached per session.

    Args:
        title_contains: Optional substring to filter by title
        case_sensitive: Whether title filtering should be case-sensitive (default: False)
        list_id: Optional list ID to filter meetings by list (only works with source='owned')
        created_at_gte: Filter meetings created on or after this date (ISO 8601: "YYYY-MM-DD")
        created_at_lte: Filter meetings created on or before this date (ISO 8601: "YYYY-MM-DD")
        source: Which meetings to include: 'all' (default), 'owned', or 'shared'
        limit: Maximum number of meetings to return. Use 0 to return all (default: 20)
        include_participants: Include full participant details (default: False for efficiency)

    Returns:
        List of meetings with id, title, date, and metadata
    """
    if list_id and source != 'owned':
        raise ValueError("list_id filter is only supported with source='owned'")

    # Get the lightweight document index
    doc_set = await _get_document_set_cached()

    # Filter by source and build sorted entry list
    entries = []
    for doc_id, entry in doc_set.documents.items():
        is_shared = bool(entry.shared)
        if source == 'owned' and is_shared:
            continue
        if source == 'shared' and not is_shared:
            continue
        entries.append((doc_id, entry.updated_at, is_shared))

    # Sort by updated_at descending (most recent first)
    entries.sort(key=lambda x: x[1], reverse=True)

    # If list_id is specified, filter to only docs in that list
    if list_id:
        headers = get_auth_headers()
        url = 'https://api.granola.ai/v1/get-document-lists-metadata'
        payload = {'include_document_ids': True, 'include_only_joined_lists': False}
        response = await _http_client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        lists_data = response.json().get('lists', {})
        list_doc_ids = set(lists_data.get(list_id, {}).get('document_ids', []))
        entries = [
            (doc_id, ts, shared)
            for doc_id, ts, shared in entries
            if doc_id in list_doc_ids
        ]

    # Batch-fetch full details in chunks, filtering as we go
    results = []
    batch_size = 40
    for chunk_start in range(0, len(entries), batch_size):
        chunk = entries[chunk_start : chunk_start + batch_size]
        chunk_ids = [doc_id for doc_id, _, _ in chunk]
        is_shared_map = {doc_id: is_shared for doc_id, _, is_shared in chunk}

        docs = await _get_documents_by_ids(chunk_ids)

        for doc in docs:
            # Skip deleted documents
            if doc.deleted_at:
                continue

            # Apply optional title filter
            if title_contains:
                title = doc.title or ''
                if case_sensitive:
                    if title_contains not in title:
                        continue
                else:
                    if title_contains.lower() not in title.lower():
                        continue

            # Apply optional date filters
            if created_at_gte or created_at_lte:
                from datetime import datetime

                created = datetime.fromisoformat(doc.created_at.replace('Z', '+00:00'))

                if created_at_gte:
                    filter_start = datetime.fromisoformat(
                        created_at_gte + 'T00:00:00+00:00'
                    )
                    if created < filter_start:
                        continue

                if created_at_lte:
                    filter_end = datetime.fromisoformat(
                        created_at_lte + 'T23:59:59+00:00'
                    )
                    if created > filter_end:
                        continue

            if doc.people:
                participant_count = len(doc.people.attendees)
            else:
                participant_count = 0

            # Extract participant details
            if include_participants and doc.people:
                participants = []
                for attendee in doc.people.attendees:
                    company_name = None
                    if attendee.details and attendee.details.company.name:
                        company_name = attendee.details.company.name

                    job_title = None
                    if attendee.details and attendee.details.person.jobTitle:
                        job_title = attendee.details.person.jobTitle

                    name = attendee.name
                    if not name and attendee.details and attendee.details.person:
                        name = attendee.details.person.name.fullName

                    participants.append(
                        {
                            'name': name,
                            'email': attendee.email,
                            'company_name': company_name,
                            'job_title': job_title,
                        }
                    )
            else:
                participants = None

            results.append(
                MeetingListItem(
                    id=doc.id,
                    title=doc.title or '(Untitled)',
                    created_at=convert_utc_to_local(doc.created_at),
                    type=doc.type,
                    has_notes=bool(doc.notes or doc.notes_markdown),
                    participant_count=participant_count,
                    is_shared=is_shared_map.get(doc.id, False),
                    participants=participants,
                )
            )

            if limit > 0 and len(results) >= limit:
                break

        if limit > 0 and len(results) >= limit:
            break

    return results


@mcp.tool(
    annotations=ToolAnnotations(
        title='Download Meeting Notes', readOnlyHint=False, idempotentHint=False
    )
)
async def download_note(
    document_id: str, filename: str, ctx: Context
) -> NoteDownloadResult:
    """
    Download meeting notes to a temporary Markdown file with metadata.

    Uses /v1/get-document-panels endpoint to fetch AI-generated notes.
    Returns structural and content metadata in the result.

    Files are saved to a temp directory that is cleaned up when the MCP server shuts down.

    Args:
        document_id: Granola document ID
        filename: Name for file (e.g., "prestel-notes.md")
        ctx: MCP context

    Returns:
        NoteDownloadResult with file path, size, and note metadata
    """
    logger = DualLogger(ctx)
    await logger.info(f'Downloading notes for document {document_id}')

    headers = get_auth_headers()

    # Get document metadata for title and date (works for owned + shared docs)
    document = await _get_document_by_id(document_id)

    # Get panels from API
    panels_url = 'https://api.granola.ai/v1/get-document-panels'
    panels_payload = {'document_id': document_id}

    panels_response = await _http_client.post(
        panels_url, json=panels_payload, headers=headers
    )
    panels_response.raise_for_status()

    # Validate panels
    panels_data = panels_response.json()
    if not panels_data:
        raise ValueError(f'No panels found for document {document_id}')

    panels = [DocumentPanel.model_validate(p) for p in panels_data]

    # Find the summary panel
    summary_panel = None
    for panel in panels:
        if panel.template_slug == 'v2:meeting-summary-consolidated':
            summary_panel = panel
            break

    if not summary_panel:
        # Fall back to first panel
        summary_panel = panels[0]

    # Convert content to Markdown (handle both ProseMirror JSON and HTML)
    if isinstance(summary_panel.content, str):
        # Content is HTML string - convert to Markdown matching Granola's format
        notes_markdown = markdownify.markdownify(
            summary_panel.content,
            heading_style='ATX',
            bullets='-',  # Use '-' for all bullets (Granola style)
            default_title=True,  # Use [URL](URL) format for links
        ).strip()
    else:
        # Content is ProseMirror JSON dict - use existing converter
        notes_markdown = prosemirror_to_markdown(summary_panel.content)

    # Format date from created_at
    from datetime import datetime

    # Convert UTC to local time before formatting
    created = datetime.fromisoformat(document.created_at.replace('Z', '+00:00'))
    created_local = created.astimezone()  # Convert to local timezone
    date_str = created_local.strftime('%a, %d %b %y')

    # Build full markdown with title and date header
    title = document.title or '(Untitled)'
    markdown = f'# {title}\n\n{date_str}\n\n{notes_markdown}'

    # Analyze markdown for metadata
    metadata = analyze_markdown_metadata(markdown)

    # Write to temp directory
    file_path = _export_dir / filename
    file_path.write_text(markdown, encoding='utf-8')

    await logger.info(f'Downloaded to {file_path}')

    return NoteDownloadResult(
        path=str(file_path),
        size_bytes=len(markdown.encode('utf-8')),
        section_count=metadata['section_count'],
        bullet_count=metadata['bullet_count'],
        heading_breakdown=metadata['heading_breakdown'],
        word_count=metadata['word_count'],
        panel_title=summary_panel.title,
        template_slug=summary_panel.template_slug,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title='Download Meeting Transcript', readOnlyHint=False, idempotentHint=False
    )
)
async def download_transcript(
    document_id: str, filename: str, ctx: Context
) -> TranscriptDownloadResult:
    """
    Download meeting transcript to a temporary Markdown file.

    Returns transcript metadata (segment count, duration, speaker breakdown)
    in the result object. The file contains timestamped conversation.

    Files are saved to a temp directory that is cleaned up when the MCP server shuts down.

    Args:
        document_id: Granola document ID
        filename: Name for file (e.g., "prestel-transcript.md")
        ctx: MCP context

    Returns:
        TranscriptDownloadResult with file path, size, and transcript metadata
    """
    logger = DualLogger(ctx)
    await logger.info(f'Downloading transcript for document {document_id}')

    headers = get_auth_headers()

    # Get document metadata for title and date (works for owned + shared docs)
    document = await _get_document_by_id(document_id)

    # Get the transcript
    url = 'https://api.granola.ai/v1/get-document-transcript'
    payload = {'document_id': document_id}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    # Validate with strict Pydantic
    segments = [TranscriptSegment.model_validate(seg) for seg in response.json()]

    if not segments:
        raise ValueError(f'No transcript available for document {document_id}')

    # Calculate metadata
    from datetime import datetime

    total_segments = len(segments)
    microphone_count = sum(1 for s in segments if s.source == 'microphone')
    system_count = sum(1 for s in segments if s.source == 'system')

    # Calculate duration
    start = datetime.fromisoformat(segments[0].start_timestamp.replace('Z', '+00:00'))
    end = datetime.fromisoformat(segments[-1].end_timestamp.replace('Z', '+00:00'))
    duration = end - start

    # Format date (abbreviated month + day)
    # Convert UTC to local time before formatting
    created = datetime.fromisoformat(document.created_at.replace('Z', '+00:00'))
    created_local = created.astimezone()  # Convert to local timezone
    date_str = created_local.strftime('%b %-d').lstrip(
        '0'
    )  # Remove leading zero from day if needed

    # Build markdown header
    title = document.title or '(Untitled)'
    lines = []
    lines.append(f'Meeting Title: {title}')
    lines.append(f'Date: {date_str}')
    lines.append('')
    lines.append('Transcript:')
    lines.append(' ')

    # Build transcript content (no timestamps, simple labels)
    # Combine consecutive segments from the same speaker
    combined_segments = []
    current_label = None
    current_texts = []

    for segment in segments:
        # Map source to Granola's labels
        label = 'Me' if segment.source == 'microphone' else 'Them'

        if label == current_label:
            # Same speaker - accumulate text
            current_texts.append(segment.text)
        else:
            # Different speaker - save previous and start new
            if current_label is not None:
                combined_text = ' '.join(current_texts)
                combined_segments.append(f'{current_label}: {combined_text}')
            current_label = label
            current_texts = [segment.text]

    # Don't forget the last segment
    if current_label is not None:
        combined_text = ' '.join(current_texts)
        combined_segments.append(f'{current_label}: {combined_text}')

    # Add two trailing spaces to all lines except the last
    for i in range(len(combined_segments) - 1):
        combined_segments[i] += '  '
    # Last line gets only one trailing space
    if combined_segments:
        combined_segments[-1] += ' '

    lines.extend(combined_segments)
    transcript_md = '\n'.join(lines)

    # Write to temp directory
    file_path = _export_dir / filename
    file_path.write_text(transcript_md, encoding='utf-8')

    await logger.info(f'Downloaded to {file_path}')

    return TranscriptDownloadResult(
        path=str(file_path),
        size_bytes=len(transcript_md.encode('utf-8')),
        segment_count=total_segments,
        duration_seconds=int(duration.total_seconds()),
        microphone_segments=microphone_count,
        system_segments=system_count,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title='Download Private Notes', readOnlyHint=False, idempotentHint=False
    )
)
async def download_private_notes(
    document_id: str, filename: str, ctx: Context
) -> PrivateNoteDownloadResult:
    """
    Download user's private notes to a temporary Markdown file.

    Returns private notes written by the user (not AI-generated notes).
    Uses get-documents-batch endpoint to fetch the notes_markdown field.

    Files are saved to a temp directory that is cleaned up when the MCP server shuts down.

    Args:
        document_id: Granola document ID
        filename: Name for file (e.g., "electrical-private-notes.md")
        ctx: MCP context

    Returns:
        PrivateNoteDownloadResult with file path and metadata
    """
    logger = DualLogger(ctx)
    await logger.info(f'Downloading private notes for document {document_id}')

    # Get document data (works for owned + shared docs)
    document = await _get_document_by_id(document_id)

    # Check if private notes exist
    if not document.notes_markdown:
        raise ValueError(f'No private notes available for document {document_id}')

    # Format date from created_at
    from datetime import datetime

    # Convert UTC to local time before formatting
    created = datetime.fromisoformat(document.created_at.replace('Z', '+00:00'))
    created_local = created.astimezone()  # Convert to local timezone
    date_str = created_local.strftime('%a, %d %b %y')

    # Build full markdown with title and date header
    title = document.title or '(Untitled)'
    markdown = f'# {title}\n\n{date_str}\n\n{document.notes_markdown}'

    # Calculate metadata
    lines = markdown.split('\n')
    line_count = len(lines)
    words = markdown.split()
    word_count = len([w for w in words if w.strip()])

    # Write to temp directory
    file_path = _export_dir / filename
    file_path.write_text(markdown, encoding='utf-8')

    await logger.info(f'Downloaded to {file_path}')

    return PrivateNoteDownloadResult(
        path=str(file_path),
        size_bytes=len(markdown.encode('utf-8')),
        word_count=word_count,
        line_count=line_count,
    )


@mcp.tool(annotations=ToolAnnotations(title='Get Meeting Lists', readOnlyHint=True))
async def get_meeting_lists(ctx: Context) -> MeetingListsResult:
    """
    Get all meeting lists/collections with their document IDs.

    Returns user's meeting lists (collections) with metadata including
    which meetings belong to each list.

    Args:
        ctx: MCP context

    Returns:
        MeetingListsResult with lists and total count
    """
    logger = DualLogger(ctx)
    await logger.info('Fetching meeting lists')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/get-document-lists-metadata'
    payload = {'include_document_ids': True, 'include_only_joined_lists': False}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    lists_data = data.get('lists', {})

    # Convert dictionary of lists to list of MeetingList objects
    meeting_lists = []
    for list_id, list_info in lists_data.items():
        doc_ids = list_info.get('document_ids', [])
        meeting_list = MeetingList(
            id=list_id,
            title=list_info.get('title', ''),
            description=list_info.get('description'),
            visibility=list_info.get('visibility', ''),
            document_ids=doc_ids,
            document_count=len(doc_ids),
            created_at=convert_utc_to_local(list_info.get('created_at', '')),
            updated_at=convert_utc_to_local(list_info.get('updated_at', '')),
        )
        meeting_lists.append(meeting_list)

    await logger.info(f'Found {len(meeting_lists)} meeting lists')

    return MeetingListsResult(lists=meeting_lists, total_count=len(meeting_lists))


@mcp.tool(annotations=ToolAnnotations(title='Get Meetings', readOnlyHint=True))
async def get_meetings(document_ids: list[str], ctx: Context) -> list[MeetingListItem]:
    """
    Fetch multiple meetings by their document IDs (batch retrieval).

    Use this to efficiently fetch multiple specific meetings at once,
    such as all meetings in a list or a set of related meetings.

    Note: Parameter is 'document_ids' (API terminology) even though
    the tool is named 'get_meetings' (user-friendly terminology).

    Args:
        document_ids: List of Granola document IDs to fetch
        ctx: MCP context

    Returns:
        List of meetings with metadata
    """
    logger = DualLogger(ctx)
    await logger.info(f'Fetching {len(document_ids)} meetings')

    # Use the document set index to determine shared status
    doc_set = await _get_document_set_cached()

    docs = await _get_documents_by_ids(document_ids)

    # Convert to list items
    meetings = []
    for doc in docs:
        # Determine shared status from the index
        entry = doc_set.documents.get(doc.id)
        is_shared = bool(entry and entry.shared) if entry else False

        # Extract participant information
        participant_count = 0
        participants = []
        if doc.people:
            participant_count = len(doc.people.attendees)
            for attendee in doc.people.attendees:
                company_name = None
                if attendee.details and attendee.details.company.name:
                    company_name = attendee.details.company.name

                job_title = None
                if attendee.details and attendee.details.person.jobTitle:
                    job_title = attendee.details.person.jobTitle

                name = attendee.name
                if not name and attendee.details and attendee.details.person:
                    name = attendee.details.person.name.fullName

                participants.append(
                    {
                        'name': name,
                        'email': attendee.email,
                        'company_name': company_name,
                        'job_title': job_title,
                    }
                )

        meetings.append(
            MeetingListItem(
                id=doc.id,
                title=doc.title or '(Untitled)',
                created_at=convert_utc_to_local(doc.created_at),
                type=doc.type,
                has_notes=bool(doc.notes or doc.notes_markdown),
                participant_count=participant_count,
                is_shared=is_shared,
                participants=participants,
            )
        )

    await logger.info(f'Fetched {len(meetings)} meetings')

    return meetings


@mcp.tool(
    annotations=ToolAnnotations(
        title='Delete Meeting', readOnlyHint=False, idempotentHint=True
    )
)
async def delete_meeting(document_id: str, ctx: Context) -> DeleteMeetingResult:
    """
    Delete a meeting by setting its deleted_at timestamp.

    Deleted meetings:
    - Don't appear in search results
    - Appear in the 'deleted' array of API responses
    - Can be fully restored using undelete_meeting()

    Args:
        document_id: Granola document ID to delete
        ctx: MCP context

    Returns:
        DeleteMeetingResult with success status and document ID
    """
    logger = DualLogger(ctx)
    await logger.info(f'Deleting meeting {document_id}')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/update-document'

    # Generate current ISO timestamp
    from datetime import datetime, timezone

    deleted_at = datetime.now(timezone.utc).isoformat()

    payload = {'id': document_id, 'deleted_at': deleted_at}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    await logger.info(f'Successfully deleted meeting {document_id}')

    return DeleteMeetingResult(success=True, document_id=document_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title='Undelete Meeting', readOnlyHint=False, idempotentHint=True
    )
)
async def undelete_meeting(document_id: str, ctx: Context) -> DeleteMeetingResult:
    """
    Restore a deleted meeting by clearing its deleted_at timestamp.

    Undeleted meetings are fully restored and will:
    - Appear in search results again
    - Be accessible through all normal endpoints
    - Retain all their original data (notes, transcripts, etc.)

    Args:
        document_id: Granola document ID to restore
        ctx: MCP context

    Returns:
        DeleteMeetingResult with success status and document ID
    """
    logger = DualLogger(ctx)
    await logger.info(f'Undeleting meeting {document_id}')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/update-document'

    payload = {'id': document_id, 'deleted_at': None}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    await logger.info(f'Successfully undeleted meeting {document_id}')

    return DeleteMeetingResult(success=True, document_id=document_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title='Update Meeting', readOnlyHint=False, idempotentHint=True
    )
)
async def update_meeting(
    ctx: Context,
    document_id: str,
    title: str | None = None,
    attendees: list[AttendeeUpdate] | None = None,
) -> UpdateMeetingResult:
    """
    Update meeting fields (title, attendees).

    Semantics: None = no change, [] = clear, [...] = replace

    Args:
        document_id: Meeting ID
        title: New title (optional)
        attendees: New attendee list (optional)

    Returns:
        UpdateMeetingResult with document ID
    """
    logger = DualLogger(ctx)
    await logger.info(f'Updating meeting {document_id}')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/update-document'

    # Build payload starting with document ID
    payload = {'id': document_id}

    # Handle title update (simple - just add to payload)
    if title is not None:
        payload['title'] = title
        await logger.info(f'  Updating title to: {title}')

    # Handle attendees update (complex - must preserve people object)
    if attendees is not None:
        await logger.info(f'  Updating attendees ({len(attendees)} attendees)')

        # Fetch current document to get existing people object
        current_doc = await _get_document_by_id(document_id)
        people = current_doc.people.model_dump() if current_doc.people else {}

        # Preserve existing fields: creator, title, created_at, sharing_link_visibility
        # Update: attendees, manual_attendee_edits

        # Build attendees array with proper nested structure
        people['attendees'] = [
            {
                'name': a.name,
                'email': a.email,
                'details': {
                    'person': {
                        'name': {'fullName': a.name},
                        **(
                            {'jobTitle': a.job_title} if a.job_title is not None else {}
                        ),
                    },
                    'company': {
                        **(
                            {'name': a.company_name}
                            if a.company_name is not None
                            else {}
                        )
                    },
                },
            }
            for a in attendees
        ]

        # Build manual_attendee_edits array
        people['manual_attendee_edits'] = [
            {'action': 'add', 'attendee_name': a.name, 'attendee_email': a.email}
            for a in attendees
        ]

        payload['people'] = people

    # Send update request
    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    result = UpdateMeetingResult.model_validate(response.json())
    await logger.info(f'Successfully updated meeting {result.id}')

    return result


@mcp.tool(annotations=ToolAnnotations(title='List Deleted Meetings', readOnlyHint=True))
async def list_deleted_meetings(ctx: Context) -> list[MeetingListItem]:
    """
    List deleted meetings.

    Fetches the document index and batch-fetches full details for documents
    with a non-null deleted_at field. These can be restored with undelete_meeting().

    Args:
        ctx: MCP context

    Returns:
        List of deleted meetings with metadata
    """
    logger = DualLogger(ctx)
    await logger.info('Fetching deleted meetings')

    # Get the full document index
    doc_set = await _get_document_set_cached()
    all_ids = list(doc_set.documents.keys())

    # Fetch all documents and filter to deleted ones
    docs = await _get_documents_by_ids(all_ids)
    deleted = [doc for doc in docs if doc.deleted_at]

    await logger.info(f'Found {len(deleted)} deleted meetings')

    return [
        MeetingListItem(
            id=doc.id,
            title=doc.title or '(Untitled)',
            created_at=convert_utc_to_local(doc.created_at),
            type=doc.type,
            has_notes=bool(doc.notes or doc.notes_markdown),
            participant_count=len(doc.people.attendees) if doc.people else 0,
            is_shared=False,
        )
        for doc in deleted
    ]


# =============================================================================
# Workspace Management Tools
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(title='List Workspaces', readOnlyHint=True))
async def list_workspaces(ctx: Context) -> ListWorkspacesResult:
    """
    List all workspaces for the user.

    Returns a list of workspaces with their basic information including ID,
    display name, role, plan type, and settings.

    Args:
        ctx: MCP context

    Returns:
        ListWorkspacesResult with workspaces list and total count
    """
    logger = DualLogger(ctx)
    await logger.info('Fetching workspaces')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/get-workspaces'

    payload = {}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = WorkspacesResponse.model_validate(response.json())

    # Convert to simplified workspace info
    workspaces = []
    for item in data.workspaces:
        workspace_info = WorkspaceInfo(
            workspace_id=item.workspace.workspace_id,
            display_name=item.workspace.display_name,
            slug=item.workspace.slug,
            is_locked=item.workspace.is_locked,
            logo_url=item.workspace.logo_url,
            created_at=item.workspace.created_at,
            role=item.role,
            plan_type=item.plan_type,
            privacy_mode_enabled=item.workspace.privacy_mode_enabled,
        )
        workspaces.append(workspace_info)

    await logger.info(f'Found {len(workspaces)} workspaces')

    return ListWorkspacesResult(workspaces=workspaces, total_count=len(workspaces))


@mcp.tool()
async def create_workspace(
    ctx: Context,
    display_name: str,
    is_locked: bool = False,
    logo_url: str | None = None,
    should_migrate_orphaned_entities: bool = False,
    migrate_subscription: bool = False,
) -> CreateWorkspaceResult:
    """
    Create a new workspace.

    Creates a new workspace with the specified settings. This is a write operation
    that creates persistent data in the user's account.

    Args:
        ctx: MCP context
        display_name: Name for the new workspace
        is_locked: Whether workspace is locked (default: False)
        logo_url: Optional URL for workspace logo
        should_migrate_orphaned_entities: Migrate orphaned entities (default: False)
        migrate_subscription: Migrate subscription (default: False)

    Returns:
        CreateWorkspaceResult with new workspace information
    """
    logger = DualLogger(ctx)
    await logger.info(f'Creating workspace: {display_name}')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v2/create-workspace'

    payload = {
        'display_name': display_name,
        'is_locked': is_locked,
        'logo_url': logo_url,
        'should_migrate_orphaned_entities': should_migrate_orphaned_entities,
        'migrate_subscription': migrate_subscription,
    }

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    result = CreateWorkspaceResult.model_validate(data)

    await logger.info(f'Created workspace: {result.workspace_id}')

    return result


@mcp.tool()
async def delete_workspace(ctx: Context, workspace_id: str) -> DeleteWorkspaceResult:
    """
    Delete a workspace.

    Deletes a workspace by setting its deleted_at timestamp. This is a soft delete
    operation - the workspace data is retained but marked as deleted.

    WARNING: This is a destructive operation. Use with caution.

    Args:
        ctx: MCP context
        workspace_id: ID of the workspace to delete

    Returns:
        DeleteWorkspaceResult with workspace_id and deletion timestamp
    """
    logger = DualLogger(ctx)
    await logger.info(f'Deleting workspace: {workspace_id}')

    headers = get_auth_headers()
    url = 'https://api.granola.ai/v1/delete-workspace'

    payload = {'workspace_id': workspace_id}

    response = await _http_client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    result = DeleteWorkspaceResult.model_validate(data)

    await logger.info(f'Deleted workspace at: {result.deleted_at}')

    return result


# =============================================================================
# URL Resolution Tools
# =============================================================================


@mcp.tool(annotations=ToolAnnotations(title='Resolve Granola URL', readOnlyHint=True))
async def resolve_url(url: str, ctx: Context) -> ResolveUrlResult:
    """
    Resolve a Granola URL to its document ID.

    Use this tool when you have a Granola sharing link (/t/ URL).
    Direct document links (/d/ URLs) are also supported.

    Examples:
    - https://notes.granola.ai/t/78b188bc-67af-46a1-949a-3148e537757c (sharing link)
    - https://notes.granola.ai/d/65b5ed31-c881-4664-912d-2e54bc8bb63a (direct link)

    Args:
        url: Granola URL (https://notes.granola.ai/t/... or /d/...)
        ctx: MCP context

    Returns:
        ResolveUrlResult with the document_id to use with other tools
    """
    logger = DualLogger(ctx)
    await logger.info(f'Resolving URL: {url}')

    # Validate URL format
    if 'notes.granola.ai' not in url:
        raise ValueError(
            'Invalid Granola URL. Expected format: https://notes.granola.ai/t/... or /d/...'
        )

    # Check for /d/ (direct document link) - use shared helper
    document_id = _extract_document_id(url)
    if document_id:
        await logger.info(f'Direct link - document ID: {document_id}')
        return ResolveUrlResult(
            document_id=document_id,
            url_type='direct',
            original_url=url,
            resolved_from_redirect=False,
        )

    # Check for /t/ (sharing token link)
    t_match = re.search(r'/t/([a-f0-9-]+)', url)
    if t_match:
        token = t_match.group(1)
        await logger.info(f'Sharing link - resolving token: {token}')

        # Resolve token to document ID via redirect
        document_id = await _resolve_sharing_token(token)
        await logger.info(f'Resolved to document ID: {document_id}')

        return ResolveUrlResult(
            document_id=document_id,
            url_type='sharing',
            original_url=url,
            resolved_from_redirect=True,
        )

    raise ValueError(
        f'Could not parse Granola URL. Expected /t/ or /d/ path in URL: {url}'
    )


# =============================================================================
# Main Entry Point
# =============================================================================


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Granola MCP Server')
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable PyCharm remote debugging',
    )
    parser.add_argument(
        '--debug-host',
        default=os.environ.get('DEBUG_HOST', 'host.docker.internal'),
        help='Debug host (default: host.docker.internal or $DEBUG_HOST)',
    )
    parser.add_argument(
        '--debug-port',
        type=int,
        default=int(os.environ.get('DEBUG_PORT', '5678')),
        help='Debug port (default: 5678 or $DEBUG_PORT)',
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point for the Granola MCP server."""
    args = parse_args()

    if args.debug:
        print(f'Enabling PyCharm remote debugging: {args.debug_host}:{args.debug_port}')
        import pydevd_pycharm

        pydevd_pycharm.settrace(
            args.debug_host,
            port=args.debug_port,
            stdoutToServer=True,
            stderrToServer=True,
            suspend=False,
        )
        print('Connected to PyCharm debugger')

    print('Starting Granola MCP server')
    mcp.run()


if __name__ == '__main__':
    main()
