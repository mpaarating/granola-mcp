# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an MCP (Model Context Protocol) server that provides access to Granola meeting notes and transcripts via Claude Code. The server uses Granola's private, undocumented API (discovered through reverse engineering) to enable AI assistants to list/filter meetings, download notes, access transcripts, and manage meeting data.

**Important**: Granola's API is private and subject to change. The server uses strict Pydantic validation to fail fast when API changes occur.

**Note on Search**: The Granola API does not support server-side search. The `list_meetings` tool performs client-side title filtering with automatic pagination and caching for performance.

## Architecture

### Core Components

**Main server** (`granola-mcp.py`):
- FastMCP server with async/await architecture
- Manages HTTP client (`httpx.AsyncClient`) and temp directory lifecycle via `lifespan` context manager
- All MCP tools defined as `@mcp.tool` decorated async functions
- Global state: `_http_client`, `_temp_dir`, `_export_dir`
- Session-based caching with `aiocache` for API responses (cleared on server restart)

**Data models** (`granola_mcp/models.py`):
- Pydantic models with `extra='ignore'` and `strict=True` - ignore unknown fields (Granola adds them often) while still type-checking the fields we consume
- Hierarchy: `DocumentsResponse` → `GranolaDocument` → nested models for People, GoogleCalendarEvent, etc.
- Simplified response models: `MeetingListItem`, `NoteDownloadResult`, `TranscriptDownloadResult`

**Helper utilities** (`granola_mcp/helpers.py`):
- `get_auth_token()`: Delegates to the self-refreshing auth manager in `granola_mcp/auth.py` (mints a WorkOS access token from our own refresh token in `~/.granola-mcp/auth.json`)
- `get_auth_headers()`: Bearer token plus Granola's Electron identity headers (`X-Client-Version`, `X-Granola-Platform`, device/OS) required to pass the "Unsupported client" gate
- `prosemirror_to_markdown()`: Recursive converter for ProseMirror JSON → Markdown (handles nested lists, headings, links, formatting)
- `analyze_markdown_metadata()`: Extracts structural metrics (sections, bullets, word count)

**Logging utilities** (`granola_mcp/logging.py`):
- `DualLogger`: Logs to both stdout and MCP client context for debugging

### API Endpoints Used

The server interacts with these Granola API endpoints:
- `POST https://api.granola.ai/v2/get-documents` - List/search meetings with pagination
- `POST https://api.granola.ai/v1/get-document-panels` - Get AI-generated notes (ProseMirror JSON)
- `POST https://api.granola.ai/v1/get-document-transcript` - Get meeting transcript segments
- `POST https://api.granola.ai/v1/get-documents-batch` - Batch fetch meetings by IDs
- `POST https://api.granola.ai/v1/get-document-lists-metadata` - Get meeting lists/collections
- `POST https://api.granola.ai/v1/update-document` - Update document fields (used for delete/undelete)

All requests require `Authorization: Bearer {access_token}` header.

### Key Design Patterns

**Authentication**: Self-refreshing — the server holds its own WorkOS refresh token in `~/.granola-mcp/auth.json` and mints access tokens via `/v1/refresh-access-token`, rather than reading Granola's (now-encrypted) local token store. Seed once with `python3 login.py`. See `granola_mcp/auth.py`.

**Temp file management**: Downloads saved to `TemporaryDirectory` that auto-cleans on server shutdown

**Caching with aiocache**: API responses cached in-memory using `@cached` decorator with session-based TTL (cleared on server restart). Cache keys automatically generated from function parameters `(limit, offset, list_id)`.

**Async generator pattern**: `list_meetings` uses internal async generator to stream documents in batches of 40, enabling efficient filtering without loading all data into memory at once.

**ProseMirror conversion**: Recursive tree traversal with depth tracking for nested list indentation

**Strict validation**: Pydantic models catch API changes immediately rather than failing silently

**Dual logging**: All operations logged to both stdout (for debugging) and MCP context (for user visibility)

## Development Commands

### Formatting
```bash
uvx ruff format .
```

### Installation
```bash
# Add to Claude Code MCP config (user scope)
claude mcp add --scope user --transport stdio granola -- uv run --script ~/granola-mcp/granola-mcp.py
```

### Debugging
```bash
# Run with PyCharm remote debugging
uv run --script granola-mcp.py --debug --debug-host localhost --debug-port 5678
```

**Testing**: After code changes, user must reconnect MCP server: `/mcp reconnect granola`

## Development Conventions

### Python Execution

Use `uv run` with heredoc for interactive Python execution:

```bash
uv run --no-project --with colorama python - <<'PY'
from colorama import Fore
print(Fore.GREEN + "Analysis result" + Fore.RESET)
PY

## Key Implementation Details

### Document Caching with aiocache

The `_get_documents_cached()` private helper function uses `aiocache` to cache API responses:

```python
@cached(ttl=None, cache=Cache.MEMORY)
async def _get_documents_cached(limit: int, offset: int, list_id: str | None = None) -> list:
```

- **Cache key**: Automatically generated from `(limit, offset, list_id)` parameters
- **TTL**: `None` (session-based - cache persists until MCP server restart)
- **Storage**: In-memory (`Cache.MEMORY`)
- **Batch size**: Tools fetch in batches of 40 to balance API efficiency and cache granularity

### ProseMirror to Markdown Conversion

The `prosemirror_to_markdown()` function handles recursive conversion with these node types:
- `doc`: Root node - joins children with double newlines
- `heading`: Uses `level` attr to determine `#` count
- `paragraph`, `bulletList`, `orderedList`: Handles nesting via `depth` parameter
- `listItem`: Processed by `process_list_item()` which handles nested lists with proper indentation
- Text marks: `bold` → `**text**`, `italic` → `*text*`, `link` → `[text](href)`, `code` → `` `text` ``

### Download Tools Architecture

All download tools follow this pattern:
1. Fetch document metadata from `/v2/get-documents` for title/date
2. Fetch specific content (panels for notes, transcript segments, etc.)
3. Convert to Markdown format matching Granola's official export format
4. Add title and date header
5. Calculate metadata (word count, sections, duration, etc.)
6. Write to temp file and return result with metadata

### Meeting Listing vs. Batch Retrieval

**Important**: The Granola API does not support server-side search. The `list_meetings` tool performs client-side filtering.

- `list_meetings()`: Lists meetings with optional client-side title filtering. Fetches documents in batches of 40 (cached) and filters by title substring (case-insensitive by default). Supports:
  - `title_contains`: Optional substring filter
  - `case_sensitive`: Toggle for case-sensitive matching
  - `list_id`: Server-side list filter
  - `limit`: Max results to return (0 = all)
  - No offset parameter - always starts from beginning and accumulates results

- `get_meetings()`: Takes list of IDs and fetches in batch - for fetching specific meetings by ID after discovery

- Pattern: Use `get_meeting_lists()` → `get_meetings(document_ids)` to fetch all meetings in a collection

### Delete/Undelete Implementation

Deletion is soft delete via timestamp:
- `delete_meeting()`: Sets `deleted_at` to current UTC timestamp
- `undelete_meeting()`: Sets `deleted_at` to `null`
- Deleted meetings appear in `deleted` array of API responses but not in regular searches
- `list_deleted_meetings()` returns IDs from the `deleted` array

## Code Style

- Single quotes for strings (enforced by Ruff)
- Async/await throughout
- Type hints on all function signatures
- Comprehensive docstrings with Args/Returns sections
- Pydantic models for all API responses
- Fail fast validation (no silent failures)

## Fixing Validation Errors

With `extra='ignore'`, unknown fields Granola adds no longer raise. Validation errors now come from the fields we *do* model: a type mismatch (`strict=True` won't coerce) or a field that came back `null`/absent but isn't declared `Optional`. The error gives you a starting point (field name, value, type), but don't just pattern-match to a fix.

**Before changing models, re-read `granola_mcp/models.py`** to understand existing patterns. Don't rely on memory.

**Inspect the API** to understand the field: Is it always present? Can it be null? What does it represent? Use `granola_mcp/helpers.py` for auth (`get_auth_token()`, `get_auth_headers()`) - auth is self-refreshing via `granola_mcp/auth.py` (token store: `~/.granola-mcp/auth.json`).

**Reason about the type**: Consider nullability, semantic meaning, whether to model nested structures. For sequences, prefer `Sequence[T]` (immutable interface) over `list[T]`. The goal is understanding, not just silencing the error.

## Common Modifications

**Adding a new tool**: Follow the pattern in existing tools - add `@mcp.tool` decorator, use `DualLogger` for logging, validate responses with Pydantic models, return structured result models.

**Updating models**: When API changes, update Pydantic models in `granola_mcp/models.py`. Strict validation will immediately catch mismatches. See "Fixing Validation Errors" above.

**Adding API endpoint**: Add to helpers or main file, use `_http_client` for requests, add `get_auth_headers()` for authentication, validate response with new Pydantic model.
