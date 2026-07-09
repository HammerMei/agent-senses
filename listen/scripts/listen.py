#!/usr/bin/env python3
"""listen — audio/video → transcript CLI for LLM agents.

Reuses the audio-extraction and Whisper-transcription logic from the `watch`
skill directly via Python import — not a nested skill invocation. `watch`
(video) is a superset of `listen` (audio): frames are extra, transcription
is the same underlying call either way, so there is exactly one Whisper
implementation to maintain, living in watch.py.

Accepts:
- A remote URL that yt-dlp understands (downloads just the audio track)
- A local audio or video file path (handed straight to the Whisper API —
  no extraction step; Groq/OpenAI transcription endpoints accept common
  containers like mp4/m4a/wav/mp3 directly). NOT YET VERIFIED end-to-end
  with a real API key — flag this to the calling agent if it matters.

Deliberately out of scope for now (ask before adding):
- Microphone / live capture
- Music or song identification — a different problem (recognition, not
  transcription), not designed yet
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Reuse watch's audio/whisper logic directly — no extra skill-call hop.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "watch" / "scripts"))
from watch import _check_binary, _extract_audio, _whisper_transcribe  # noqa: E402

DEFAULT_TIMEOUT = 120


def _err(msg: str, err_type: str) -> dict[str, Any]:
    return {"type": err_type, "message": msg}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="listen", description="Transcribe audio from a URL or local file via Whisper (Groq/OpenAI)."
    )
    parser.add_argument("-i", "--input", required=True, help="Remote URL (yt-dlp-supported site) or local audio/video file path")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-step timeout in seconds")
    args = parser.parse_args(argv or sys.argv[1:])

    response: dict[str, Any] = {
        "input": args.input,
        "transcript": "",
        "transcript_source": "none",
        "error": None,
    }

    if not _check_binary("ffmpeg"):
        response["error"] = _err("required binary not found: ffmpeg", "ffmpeg_not_found")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 1

    local_path = Path(args.input)
    if local_path.exists():
        audio_path = local_path
    else:
        if not _check_binary("yt-dlp"):
            response["error"] = _err("required binary not found: yt-dlp", "ytdlp_not_found")
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 1
        work_dir = Path(tempfile.mkdtemp(prefix="listen_"))
        audio_path, audio_err = _extract_audio(args.input, work_dir, args.timeout)
        if audio_path is None:
            response["error"] = _err(audio_err or "audio extraction failed", "audio_extraction_failed")
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 1

    text, source = _whisper_transcribe(audio_path, args.timeout)
    if text:
        response["transcript"], response["transcript_source"] = text, source
    else:
        response["error"] = _err(
            "mw not installed and no GROQ_API_KEY/OPENAI_API_KEY set, or all transcription attempts failed",
            "whisper_unavailable",
        )
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
