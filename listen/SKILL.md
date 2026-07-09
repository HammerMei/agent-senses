---
name: listen
description: Transcribe an audio/video URL or local file. Tries local on-device MacWhisper (`mw`) first — privacy-preserving, no cloud — then falls back to Groq/OpenAI Whisper. Unlike `watch`'s caption-first, transcription-as-fallback design, this skill's whole job is transcription, so it always runs the full attempt chain.
model_tier: light
---

# listen

Use `scripts/listen.py` to get a transcript from something that isn't a captioned YouTube video — a podcast, a voice memo, a meeting recording, a local audio/video file.

## Relationship to `watch`

`watch` is a superset of `listen`: a video is audio + frames. Rather than duplicate the transcription-calling logic, `listen.py` imports `_extract_audio`/`_whisper_transcribe`/`_check_binary` directly from `watch/scripts/watch.py` (a Python import, not a nested skill invocation — no extra skill-call hop). `watch.py` stays self-contained and does not depend on `listen` in the other direction. Since `_whisper_transcribe` itself tries `mw` before cloud Whisper, `listen` inherits that same local-first priority automatically.

If you're already looking at a YouTube-style video with captions, use `watch` — it's cheaper (no paid API call). Reach for `listen` when there's no caption track to fall back on, or the source is audio-only to begin with.

## What this skill does

- Accepts a remote URL (anything `yt-dlp` understands) **or** a local audio/video file path
- For a URL: downloads just the audio track (not a full video download)
- For a local file: hands it straight to the transcriber — no extraction step. MacWhisper and Groq/OpenAI's transcription endpoints all accept common containers (mp4, m4a, wav, mp3, webm) directly. Cloud-API path **not yet verified end-to-end with a real API key** — say so if it matters for the task at hand, don't assume it silently works. The `mw` local path **has been verified end-to-end** (2026-07-09).
- Tries each transcriber in order until one succeeds:
  1. **`mw` (MacWhisper CLI)** — local, on-device, audio never leaves the machine. No API key, no cost, slower/heavier on CPU than cloud. Requires the MacWhisper app installed and running. `transcript_source: "mw-local"`.
  2. **Groq Whisper** (`GROQ_API_KEY`) — cloud. `transcript_source: "whisper-groq"`.
  3. **OpenAI Whisper** (`OPENAI_API_KEY`) — cloud fallback. `transcript_source: "whisper-openai"`.

## What this skill deliberately does NOT do (yet)

- **Microphone / live capture** — skipped as out of scope, add only if explicitly asked
- **Music/song identification** — a different problem (recognition vs. transcription), not designed yet. If this gets built, `watch` inherits it too since it's a superset — don't build it speculatively before the design is settled.

## Operating rules

- If privacy matters for the content being transcribed, check `transcript_source` came back `"mw-local"` before assuming nothing left the machine — it silently falls through to cloud APIs if `mw` isn't installed/running.
- The cloud fallback paths cost money and time — unlike `watch`'s transcription fallback (opt-in behind a flag), `listen`'s whole job is transcription so it always runs the attempt chain. Don't call `listen` on something `watch` could already answer from captions.
- If `error` is set, check `error.type` before treating `transcript` as reliable.

## CLI

```bash
# Remote URL (podcast page, video site, etc.)
python3 scripts/listen.py -i "https://example.com/episode-123"

# Local file
python3 scripts/listen.py -i "/path/to/recording.m4a"
```

## Output contract

```json
{
  "input": "...",
  "transcript": "...",
  "transcript_source": "mw-local | whisper-groq | whisper-openai | none",
  "error": null
}
```

### `error.type` values

| `error.type` | Meaning | Agent action |
|---|---|---|
| `ffmpeg_not_found` | `ffmpeg` binary missing | Install: `brew install ffmpeg` (or your OS equivalent), then retry |
| `ytdlp_not_found` | Input was a URL but `yt-dlp` binary missing | Install: `pipx install yt-dlp`, then retry |
| `audio_extraction_failed` | Input was a URL but `yt-dlp` couldn't extract audio | Check the URL is valid and reachable |
| `whisper_unavailable` | `mw` not installed and no `GROQ_API_KEY`/`OPENAI_API_KEY` set, or all attempts failed | Install MacWhisper, or set an API key, and retry |

## Dependencies

[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (`pipx install yt-dlp`, only needed for URL input) and `ffmpeg` (`brew install ffmpeg`) — same as `watch`. Plus at least one transcriber: [MacWhisper](https://goodsnooze.gumroad.com/l/macwhisper) (`mw` CLI, macOS only, local/private) and/or a `GROQ_API_KEY`/`OPENAI_API_KEY` env var (cloud fallback).
