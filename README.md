# Granola MCP Server

MCP server for accessing Granola meeting notes and data via Claude Code.

## API Research Findings

### Authentication
This server holds its **own** WorkOS refresh token rather than reading Granola's
local token store (Granola moved that store to an encrypted `supabase.json.enc`
keyed via the macOS Keychain, whose format is fragile and re-breaks on updates).
See [`granola_mcp/auth.py`](granola_mcp/auth.py).

- **Token store**: `~/.granola-mcp/auth.json` (this server's own file, written 0600)
- **Refresh**: access tokens are minted on demand via
  `POST https://api.granola.ai/v1/refresh-access-token` with body `{refresh_token}`
  plus Granola's Electron identity headers; cached until ~2 min before expiry, with
  rotated refresh tokens persisted automatically
- **Auth method**: Bearer token in the Authorization header
- **One-time setup**: seed a refresh token once with `python3 login.py` (reads the
  token from stdin, validates it against the refresh endpoint, then saves it). If
  `~/.granola-mcp/auth.json` is absent it bootstraps once from Granola's legacy
  plaintext `supabase.json`, when that file still holds a live token.

### Available API Endpoints

**Documents endpoint**: `POST https://api.granola.ai/v2/get-documents`
- Parameters: `limit` (int), `offset` (int), `include_last_viewed_panel` (bool)
- Returns: `{"docs": [...]}`
- Pagination: Use offset for additional results
- Note: `notes` and `notes_markdown` fields often empty for newer meetings

**Document panels endpoint**: `POST https://api.granola.ai/v1/get-document-panels`
- Parameters: `document_id` (str)
- Returns: Array of panels with AI-generated content
- Each panel: `id`, `title`, `content` (ProseMirror JSON), `template_slug`, timestamps
- Primary panel: `template_slug: "v2:meeting-summary-consolidated"`
- Contains the actual AI-generated meeting notes

**Transcript endpoint**: `POST https://api.granola.ai/v1/get-document-transcript`
- Parameters: `document_id` (str)
- Returns: Array of transcript segments with timestamps
- Each segment: `document_id`, `id`, `start_timestamp`, `end_timestamp`, `text`, `source`, `is_final`
- Source values: "microphone" (user) or "system" (other party)

**Headers required**:
```
Authorization: Bearer {access_token}
Content-Type: application/json
```

### Document Structure

Each document contains:
- `id` (str): Unique document identifier
- `title` (str | None): Meeting title
- `created_at` (str): ISO timestamp
- `updated_at` (str): ISO timestamp
- `notes` (dict): ProseMirror JSON format (AI-generated notes)
- `notes_markdown` (str | None): Markdown version of notes
- `people` (dict): Meeting participants
- `google_calendar_event` (dict | None): Calendar metadata
- `transcribe` (bool): Whether transcript was enabled
- `type` (str): Usually "meeting"

### ProseMirror Format

Notes are stored in ProseMirror JSON:
```json
{
  "type": "doc",
  "content": [
    {
      "type": "heading",
      "attrs": {"level": 3, "id": "..."},
      "content": [{"type": "text", "text": "..."}]
    },
    {
      "type": "bulletList",
      "attrs": {"tight": true},
      "content": [...]
    }
  ]
}
```

### What Works
✅ Search/list all meetings with pagination and optional filtering
✅ AI-generated meeting notes via panels endpoint (ProseMirror→Markdown)
✅ Meeting metadata (title, date, participants)
✅ Download notes with structural metadata (sections, bullets, word count)
✅ Raw meeting transcripts with timestamps and speaker attribution
✅ Download transcripts with metadata (duration, speaker breakdown)

### What Doesn't Work
❌ Individual document GET endpoints (404 errors)
❌ Separate chapters endpoints (404 errors)

**Note**: Granola's API is private and undocumented. All endpoints discovered through reverse engineering.

### Future Enhancements

The following features may be supported by the Granola API but are not yet implemented:
- **Advanced filtering**: Filter by date ranges, meeting status, or custom metadata
- **Sorting**: Specify sort order (e.g., by date, title, or last modified)
- **Type filtering**: Filter by document/meeting type
- **Tag filtering**: Filter meetings by attached tags or labels
- **Archived meetings**: Include or exclude archived meetings
- **User filtering**: Restrict results to specific user(s)

These parameters would be added to the `search_meetings` tool once the Granola API documentation becomes available or through further reverse engineering.

## Installation

```bash
# Add to Claude Code MCP config (user scope)
claude mcp add --scope user --transport stdio granola -- uv run --script ~/granola-mcp/granola-mcp.py
```

Then seed the auth token once (see [Authentication](#authentication)):

```bash
python3 login.py   # paste a Granola refresh token; it is validated and saved to ~/.granola-mcp/auth.json
```

## Available Tools

### Discovery & Organization

- **`search_meetings`**: Search/list meetings with optional query, list filter, and pagination. When query is omitted, returns all meetings. Supports filtering by title keyword (case-insensitive) and by list ID (server-side filtering). Parameters: `query`, `list_id`, `limit`, `offset`.
- **`get_meeting_lists`**: Get all meeting lists/collections with their document IDs. Returns lists with metadata including which meetings belong to each list.
- **`get_meetings`**: Fetch multiple specific meetings by document IDs (batch retrieval). Parameter: `document_ids` (list of strings). Useful for fetching all meetings in a list at once.

### Download Tools

- **`download_note`**: Download AI-generated meeting notes to a temporary Markdown file. Uses `/v1/get-document-panels` endpoint. Returns metadata (section count, bullet count, heading breakdown, word count, panel title). Files are auto-cleaned on server shutdown. (Produces markdown identical to Granola's official export)
- **`download_private_notes`**: Download user's private notes to a temporary Markdown file. Uses `/v2/get-documents` endpoint to fetch `notes_markdown` field. Returns metadata (word count, line count). Files are auto-cleaned on server shutdown. (Produces markdown identical to Granola's official export)
- **`download_transcript`**: Download meeting transcript to a temporary Markdown file. Returns metadata (segment count, duration, speaker breakdown). Combines consecutive segments from same speaker. Files are auto-cleaned on server shutdown. (Produces markdown identical to Granola's official export)

### Management Tools (Write Operations)

- **`list_deleted_meetings`**: List all deleted meeting document IDs. Returns IDs that can be used with `undelete_meeting()` to restore meetings. Uses `/v2/get-documents` endpoint to access the 'deleted' array.
- **`delete_meeting`**: Delete a meeting by setting its `deleted_at` timestamp. Deleted meetings don't appear in search results and appear in the 'deleted' array of API responses. Can be fully restored using `undelete_meeting()`. Uses `/v1/update-document` endpoint. Parameter: `document_id`.
- **`undelete_meeting`**: Restore a deleted meeting by clearing its `deleted_at` timestamp. Undeleted meetings are fully restored and appear in search results again with all original data intact. Uses `/v1/update-document` endpoint. Parameter: `document_id`.

## Implementation Notes

- Uses `httpx` for async HTTP requests
- Strict Pydantic validation (fail fast on API changes)
- Temp directory for downloads (auto-cleanup on shutdown)
- Follows browser-automation-mcp.py patterns
- Helper functions organized in `granola_mcp/helpers.py`
- Pydantic models in `granola_mcp/models.py`

## Development

**Formatting:**
```bash
uvx ruff format .
```
