---
name: watch
description: Fetch a YouTube video's transcript, metadata, and evenly-spaced keyframe screenshots. Transcript-first (cheap, works for most informational videos); frames are provided for the calling agent to inspect directly (native vision) or hand to the `see` skill.
model_tier: light
---

# watch

Use `scripts/watch.py` to understand a YouTube video without guessing from its title.

## What this skill does

- Fetches video metadata: title, channel, upload date, duration, description, chapters
- Fetches the transcript (prefers manual/official captions, falls back to auto-generated), cleaned into plain text
- Extracts N evenly-spaced keyframe screenshots via `ffmpeg`, seeking directly into the resolved stream — **no full video download**
- Returns structured JSON for reliable agent consumption

## What this skill deliberately does NOT do

`watch` never calls a vision model itself. Frame *interpretation* is left to the calling agent:

- If your model has native vision (Claude, GPT), just look at the returned frame paths directly.
- If your model does not have native vision (e.g. opencode-based agents), pipe the frame paths into the `see` skill.

This keeps `watch` cheap and fast for the common case (transcript alone is usually enough to know what a video covers), and defers to a purpose-built skill when frames actually need visual analysis.

## Operating rules

- Read the transcript before speculating about video content — do not guess from the title/thumbnail alone
- If `transcript_source` is `"none"`, say so explicitly; do not fabricate a summary from metadata alone
- If `error` is set, check `error.type` before treating any other field as reliable

## CLI

```bash
# Transcript + metadata + 5 keyframes (default)
python3 scripts/watch.py -u "https://www.youtube.com/watch?v=VIDEO_ID"

# Transcript + metadata only, skip frames
python3 scripts/watch.py -u "https://www.youtube.com/watch?v=VIDEO_ID" --no-frames

# More frames, custom output directory
python3 scripts/watch.py -u "https://www.youtube.com/watch?v=VIDEO_ID" -n 10 -o /tmp/my_frames
```

## Output contract

Always returns JSON. Check `error` first.

| Field | Type | Description |
|-------|------|-------------|
| `error` | object \| null | `null` on success. Set on failure or partial failure — check `error.type`. |
| `title` | string | Video title |
| `channel` | string | Channel/uploader name |
| `upload_date` | string | `YYYYMMDD` |
| `duration_seconds` | float | Video length in seconds |
| `description` | string | Full video description |
| `chapters` | array | `[{title, start_time}]` if the video has chapters, else `[]` |
| `transcript` | string | Cleaned transcript text |
| `transcript_source` | string | `"manual"`, `"auto"`, or `"none"` |
| `frames` | array | `[{id, timestamp_seconds, path}]` — empty if `--no-frames` or extraction failed |

### `error.type` values

| `error.type` | Meaning | Agent action |
|---|---|---|
| `ytdlp_not_found` | `yt-dlp` binary missing | Install: `pipx install yt-dlp`, then retry |
| `ffmpeg_not_found` | `ffmpeg` binary missing | Install: `brew install ffmpeg` (or your OS equivalent), then retry |
| `metadata_fetch_failed` | yt-dlp could not extract video info | Check the URL is valid and the video is public |
| `stream_resolve_failed` | Could not resolve a direct stream URL for frame extraction | Transcript/metadata may still be usable; frames unavailable |
| `frame_extraction_failed` | Stream resolved but `ffmpeg` produced no frames | Retry with `-n` fewer frames or check network access |
| `timeout` | A step exceeded the configured timeout | Retry with `--timeout` increased |

Note: `error` may be set even when `transcript`/`metadata` fields are populated — this skill degrades gracefully (e.g. frame extraction can fail while transcript still succeeds). Always check which fields actually got data.

## Notes for agents

- Prefer `--no-frames` when you only need to know "what does this video say" — cheaper and faster
- With chapters available, `frames` timestamps are independent of chapter boundaries (evenly spaced); pass explicit timestamps via a future `-t` flag if you need frames at specific chapter marks (not yet implemented)
- Dependencies: [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (`pipx install yt-dlp`) and `ffmpeg` (`brew install ffmpeg`)
