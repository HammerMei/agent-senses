#!/usr/bin/env python3
"""watch — YouTube understanding CLI for LLM agents.

Fetches metadata, a cleaned transcript, and evenly-spaced keyframe screenshots
from a YouTube video. Deliberately does NOT call any vision model itself —
frame *interpretation* is left to the calling agent:
  - Agents with native vision (Claude, GPT) can just look at the returned
    frame paths directly.
  - Agents without native vision (e.g. opencode-based) should pipe the frame
    paths into the `see` skill.

Design goals:
- Transcript-first: reading the transcript is enough to understand most
  informational videos, and it's cheap (no vision calls).
- Frames are extracted via ffmpeg seeking directly into the resolved stream
  URL — no full video download required.
- Return structured JSON for reliable downstream agent consumption.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 120
DEFAULT_NUM_FRAMES = 5
DEFAULT_FRAME_HEIGHT = 480


@dataclass
class WatchResponse:
    title: str = ""
    channel: str = ""
    upload_date: str = ""
    duration_seconds: float = 0.0
    description: str = ""
    chapters: list[dict[str, Any]] = field(default_factory=list)
    transcript: str = ""
    transcript_source: str = "none"  # "manual" | "auto" | "whisper-groq" | "whisper-openai" | "none"
    frames: list[dict[str, Any]] = field(default_factory=list)
    frames_deduplicated: int = 0
    video_url: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_url": self.video_url,
            "title": self.title,
            "channel": self.channel,
            "upload_date": self.upload_date,
            "duration_seconds": self.duration_seconds,
            "description": self.description,
            "chapters": self.chapters,
            "transcript": self.transcript,
            "transcript_source": self.transcript_source,
            "frames": self.frames,
            "frames_deduplicated": self.frames_deduplicated,
            "error": self.error,
        }


def _err(msg: str, err_type: str) -> dict[str, Any]:
    return {"type": err_type, "message": msg}


def _run(cmd: list[str], timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=input_text, text=True, capture_output=True, timeout=timeout, check=False
    )


def _run_bytes(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Like _run but for commands whose stdout is binary (e.g. raw pixel data)."""
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _check_binary(name: str) -> bool:
    try:
        subprocess.run([name, "--version"], capture_output=True, timeout=10, check=False)
        return True
    except FileNotFoundError:
        return False


# ─── Metadata ───────────────────────────────────────────────────────────────

def _fetch_metadata(url: str, timeout: int) -> tuple[dict[str, Any] | None, str | None]:
    proc = _run(["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url], timeout)
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"yt-dlp exited with code {proc.returncode}"
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"failed to parse yt-dlp metadata JSON: {exc}"


def _extract_chapters(meta: dict[str, Any]) -> list[dict[str, Any]]:
    chapters = meta.get("chapters") or []
    return [
        {"title": c.get("title", ""), "start_time": c.get("start_time", 0)}
        for c in chapters
        if isinstance(c, dict)
    ]


# ─── Transcript ─────────────────────────────────────────────────────────────

_VTT_TAG_RE = re.compile(r"<[^>]+>")
_VTT_TIMESTAMP_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")


def _clean_vtt(vtt_text: str) -> str:
    """Collapse auto-caption VTT (with rolling duplicate lines) into plain text."""
    lines = vtt_text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT":
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if _VTT_TIMESTAMP_LINE_RE.match(line):
            continue
        if line.isdigit():
            continue
        clean = _VTT_TAG_RE.sub("", line).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return " ".join(out)


def _fetch_transcript(url: str, work_dir: Path, timeout: int) -> tuple[str, str]:
    """Try manual subs first, then auto-generated. Returns (transcript, source)."""
    out_template = str(work_dir / "sub.%(ext)s")
    for flag, source in (("--write-sub", "manual"), ("--write-auto-sub", "auto")):
        proc = _run(
            [
                "yt-dlp", "--skip-download", flag, "--sub-lang", "en",
                "--sub-format", "vtt", "--no-warnings", "-o", out_template, url,
            ],
            timeout,
        )
        vtt_files = list(work_dir.glob("sub*.vtt"))
        if proc.returncode == 0 and vtt_files:
            text = _clean_vtt(vtt_files[0].read_text(errors="replace"))
            if text:
                return text, source
    return "", "none"


def _extract_audio(url: str, work_dir: Path, timeout: int) -> tuple[Path | None, str | None]:
    """Download just the audio track (small, mp3) for Whisper fallback."""
    out_template = str(work_dir / "audio.%(ext)s")
    proc = _run(
        [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "5",
            "--no-warnings", "-o", out_template, url,
        ],
        timeout,
    )
    audio_files = list(work_dir.glob("audio.*"))
    if proc.returncode != 0 or not audio_files:
        return None, proc.stderr.strip() or "yt-dlp produced no audio file"
    return audio_files[0], None


def _mw_transcribe(audio_path: Path, timeout: int) -> tuple[str, str]:
    """Local, on-device transcription via MacWhisper's `mw` CLI — audio never leaves the machine.
    Slower / heavier on CPU than a cloud call, but no API key and no privacy tradeoff."""
    if not _check_binary("mw"):
        return "", ""
    proc = _run(["mw", "transcribe", str(audio_path)], timeout)
    text = proc.stdout.strip()
    if proc.returncode == 0 and text:
        return text, "mw-local"
    return "", ""


def _cloud_whisper_transcribe(audio_path: Path, timeout: int) -> tuple[str, str]:
    """Try Groq Whisper first (cheaper/faster), then OpenAI Whisper. Returns (text, source)."""
    attempts = (
        ("GROQ_API_KEY", "https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", "whisper-groq"),
        ("OPENAI_API_KEY", "https://api.openai.com/v1/audio/transcriptions", "whisper-1", "whisper-openai"),
    )
    for env_var, endpoint, model, source in attempts:
        api_key = os.environ.get(env_var)
        if not api_key:
            continue
        proc = _run(
            [
                "curl", "-s", endpoint,
                "-H", f"Authorization: Bearer {api_key}",
                "-F", f"file=@{audio_path}",
                "-F", f"model={model}",
                "-F", "response_format=text",
            ],
            timeout,
        )
        text = proc.stdout.strip()
        if proc.returncode == 0 and text and "error" not in text[:20].lower():
            return text, source
    return "", ""


def _whisper_transcribe(audio_path: Path, timeout: int) -> tuple[str, str]:
    """Local MacWhisper (`mw`) first — privacy-preserving, audio stays on-device — then cloud
    Groq/OpenAI Whisper as fallback if `mw` isn't installed or fails. Returns (text, source)."""
    text, source = _mw_transcribe(audio_path, timeout)
    if text:
        return text, source
    return _cloud_whisper_transcribe(audio_path, timeout)


# ─── Frames ─────────────────────────────────────────────────────────────────

def _resolve_stream_url(url: str, timeout: int) -> tuple[str | None, str | None]:
    proc = _run(
        ["yt-dlp", "-f", f"best[height<={DEFAULT_FRAME_HEIGHT}]", "-g", "--no-warnings", url],
        timeout,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, proc.stderr.strip() or "could not resolve a direct stream URL"
    return proc.stdout.strip().splitlines()[0], None


def _extract_frames(
    stream_url: str, duration: float, num_frames: int, out_dir: Path, timeout: int
) -> list[dict[str, Any]]:
    if duration <= 0:
        timestamps = [0.0]
    else:
        # Evenly spaced, skipping the very first/last instant.
        step = duration / (num_frames + 1)
        timestamps = [round(step * (i + 1), 1) for i in range(num_frames)]

    frames: list[dict[str, Any]] = []
    for idx, ts in enumerate(timestamps):
        frame_path = out_dir / f"frame{idx + 1}.jpg"
        proc = _run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", stream_url, "-frames:v", "1", "-q:v", "2", str(frame_path)],
            timeout,
        )
        if proc.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0:
            frames.append({"id": f"frame{idx + 1}", "timestamp_seconds": ts, "path": str(frame_path)})
    return frames


def _frame_fingerprint(frame_path: Path, timeout: int) -> bytes | None:
    """8x8 grayscale raw pixels — cheap perceptual fingerprint, no extra deps beyond ffmpeg."""
    proc = _run_bytes(
        ["ffmpeg", "-y", "-i", str(frame_path), "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"],
        timeout,
    )
    if proc.returncode != 0 or len(proc.stdout) < 64:
        return None
    return proc.stdout[:64]


def _hamming_distance(a: bytes, b: bytes) -> int:
    """Average-hash style distance: threshold each fingerprint against its own mean, then compare bits."""
    def bits(data: bytes) -> list[int]:
        mean = sum(data) / len(data)
        return [1 if v > mean else 0 for v in data]
    return sum(1 for x, y in zip(bits(a), bits(b)) if x != y)


def _dedupe_frames(
    frames: list[dict[str, Any]], timeout: int, threshold: int = 5
) -> tuple[list[dict[str, Any]], int]:
    """Drop frames that are near-identical to the previously *kept* frame (e.g. static/paused footage)."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    prev_fp: bytes | None = None
    for f in frames:
        fp = _frame_fingerprint(Path(f["path"]), timeout)
        if fp is not None and prev_fp is not None and _hamming_distance(fp, prev_fp) <= threshold:
            dropped += 1
            continue
        kept.append(f)
        if fp is not None:
            prev_fp = fp
    return kept, dropped


# ─── Main ───────────────────────────────────────────────────────────────────

def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watch", description="Fetch transcript + keyframes from a YouTube video.")
    parser.add_argument("-u", "--url", required=True, help="YouTube video URL")
    parser.add_argument("-n", "--num-frames", type=int, default=DEFAULT_NUM_FRAMES, help="Number of keyframes to extract")
    parser.add_argument("-o", "--output-dir", default=None, help="Directory to save frames (default: temp dir)")
    parser.add_argument("--no-frames", action="store_true", help="Skip frame extraction (transcript + metadata only)")
    parser.add_argument(
        "--whisper-fallback", action="store_true",
        help="If no captions exist, fall back to Whisper transcription (GROQ_API_KEY or OPENAI_API_KEY required). Costs money/time — opt-in only.",
    )
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Keep near-identical frames (e.g. static footage) instead of dropping them. Dedup runs by default — it's local/free (ffmpeg only), unlike --whisper-fallback.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-step timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv or sys.argv[1:])
    response = WatchResponse(video_url=args.url)

    for binary, err_type in (("yt-dlp", "ytdlp_not_found"), ("ffmpeg", "ffmpeg_not_found")):
        if not _check_binary(binary):
            response.error = _err(f"required binary not found: {binary}", err_type)
            print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
            return 1

    try:
        meta, meta_err = _fetch_metadata(args.url, args.timeout)
        if meta is None:
            response.error = _err(meta_err or "metadata fetch failed", "metadata_fetch_failed")
            print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
            return 1

        response.title = meta.get("title", "")
        response.channel = meta.get("channel") or meta.get("uploader", "")
        response.upload_date = meta.get("upload_date", "")
        response.duration_seconds = float(meta.get("duration") or 0)
        response.description = meta.get("description", "")
        response.chapters = _extract_chapters(meta)

        out_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="watch_"))
        out_dir.mkdir(parents=True, exist_ok=True)

        response.transcript, response.transcript_source = _fetch_transcript(args.url, out_dir, args.timeout)

        if response.transcript_source == "none" and args.whisper_fallback:
            audio_path, audio_err = _extract_audio(args.url, out_dir, args.timeout)
            if audio_path is not None:
                whisper_text, whisper_source = _whisper_transcribe(audio_path, args.timeout)
                if whisper_text:
                    response.transcript, response.transcript_source = whisper_text, whisper_source
                else:
                    response.error = _err(
                        "mw not installed and no GROQ_API_KEY/OPENAI_API_KEY set, or all transcription attempts failed",
                        "whisper_unavailable",
                    )
            else:
                response.error = _err(audio_err or "audio extraction failed", "audio_extraction_failed")

        if not args.no_frames:
            stream_url, stream_err = _resolve_stream_url(args.url, args.timeout)
            if stream_url:
                response.frames = _extract_frames(
                    stream_url, response.duration_seconds, args.num_frames, out_dir, args.timeout
                )
                if not response.frames:
                    response.error = _err("frame extraction produced no output", "frame_extraction_failed")
                elif not args.no_dedup:
                    response.frames, response.frames_deduplicated = _dedupe_frames(response.frames, args.timeout)
            else:
                response.error = _err(stream_err or "could not resolve stream URL", "stream_resolve_failed")

        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except subprocess.TimeoutExpired:
        response.error = _err(f"a step exceeded the {args.timeout}s timeout", "timeout")
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure as structured JSON
        response.error = _err(str(exc), exc.__class__.__name__)
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
