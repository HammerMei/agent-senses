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
    transcript_source: str = "none"  # "manual" | "auto" | "none"
    frames: list[dict[str, Any]] = field(default_factory=list)
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
            "error": self.error,
        }


def _err(msg: str, err_type: str) -> dict[str, Any]:
    return {"type": err_type, "message": msg}


def _run(cmd: list[str], timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=input_text, text=True, capture_output=True, timeout=timeout, check=False
    )


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


# ─── Main ───────────────────────────────────────────────────────────────────

def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watch", description="Fetch transcript + keyframes from a YouTube video.")
    parser.add_argument("-u", "--url", required=True, help="YouTube video URL")
    parser.add_argument("-n", "--num-frames", type=int, default=DEFAULT_NUM_FRAMES, help="Number of keyframes to extract")
    parser.add_argument("-o", "--output-dir", default=None, help="Directory to save frames (default: temp dir)")
    parser.add_argument("--no-frames", action="store_true", help="Skip frame extraction (transcript + metadata only)")
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

        if not args.no_frames:
            stream_url, stream_err = _resolve_stream_url(args.url, args.timeout)
            if stream_url:
                response.frames = _extract_frames(
                    stream_url, response.duration_seconds, args.num_frames, out_dir, args.timeout
                )
                if not response.frames:
                    response.error = _err("frame extraction produced no output", "frame_extraction_failed")
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
