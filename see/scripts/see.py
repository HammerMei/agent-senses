#!/usr/bin/env python3
"""see — backend-neutral vision CLI for LLM agents.

Lets agents without native vision delegate file understanding to an external
vision-capable model (Codex CLI / GPT-4o by default). Supports images and PDFs —
any format the backend accepts can be passed as an attachment.

Design goals:
- Accept one or more attachments (images, PDFs) in a single request.
- Keep the backend pluggable so Codex CLI can be swapped for another SDK later.
- Return structured JSON for reliable downstream agent consumption.
- Gracefully handle non-JSON backend output with a safe fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_OUTPUTS = {"json", "text"}
DEFAULT_TIMEOUT = int(os.getenv("SEE_TIMEOUT", "120"))
DEFAULT_BACKEND = os.getenv("SEE_BACKEND", "codex-cli")
DEFAULT_CODEX_COMMAND = os.getenv("SEE_CODEX_COMMAND", "codex")
DEFAULT_CODEX_ARGS = os.getenv("SEE_CODEX_ARGS", "")


@dataclass
class Attachment:
    id: str
    path: str


@dataclass
class VisionRequest:
    question: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class VisionResponse:
    answer: str
    confidence: float = 0.0
    per_file_findings: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None
    backend: str = ""
    raw_text: str = ""
    stderr: str = ""
    exit_code: int = 0
    # Structured error for agent consumption.
    # null  → full success (JSON parsed, answer is usable).
    # set   → failure or degraded mode; agents should check `error.type`:
    #   "backend_not_found" — CLI binary missing (exit 127)
    #   "timeout"           — backend exceeded the configured timeout
    #   "no_output"         — backend exited but produced no stdout
    #   "non_json_output"   — backend returned text; `answer` contains raw text as fallback
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "confidence": self.confidence,
            "per_file_findings": self.per_file_findings,
            "comparison": self.comparison,
            "backend": self.backend,
            "raw_text": self.raw_text,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "error": self.error,
        }


# ─── Prompt builder ─────────────────────────────────────────────────────────

def _build_prompt(request: VisionRequest) -> str:
    attach_lines = "\n".join(f"- {att.id}" for att in request.attachments) or "- (none)"
    return (
        "You are a vision analysis assistant. Answer strictly from visible evidence.\n\n"
        "Rules:\n"
        "1. Never invent details. If something is unclear, occluded, or ambiguous, say so explicitly.\n"
        "2. With multiple attachments, always refer to findings by file_id.\n"
        "3. Output valid JSON only — no markdown fences.\n"
        "\n"
        f"Question: {request.question}\n\n"
        f"Attachments:\n{attach_lines}\n\n"
        "Return JSON:\n"
        '{\n'
        '  "answer": "...",\n'
        '  "confidence": 0.85,\n'
        '  "per_file_findings": [{"file_id": "...", "finding": "..."}],\n'
        '  "comparison": null\n'
        '}\n'
    )


# ─── Parsing helpers ────────────────────────────────────────────────────────

def _extract_json_candidate(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return text

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            body = "\n".join(lines[1:])
            if body.endswith("```"):
                body = body[: body.rfind("```")].strip()
            return body.strip()

    # Try the first top-level JSON object/array if the model wrapped text around it.
    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return text[first_obj : last_obj + 1].strip()

    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return text[first_arr : last_arr + 1].strip()

    return text


def _safe_json_loads(raw_text: str) -> tuple[dict[str, Any] | None, str | None]:
    candidate = _extract_json_candidate(raw_text)
    if not candidate:
        return None, "empty output"
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed, None
        return None, "top-level JSON must be an object"
    except json.JSONDecodeError as exc:
        return None, str(exc)


# ─── Backends ───────────────────────────────────────────────────────────────

class VisionBackend(ABC):
    name: str = "unknown"

    @abstractmethod
    def run(self, request: VisionRequest) -> VisionResponse: ...


class CodexCLIBackend(VisionBackend):
    """Codex CLI backend via subprocess.

    Sends the prompt over stdin and passes each attachment path via -i flags.
    Any format Codex CLI supports (images, PDFs) is accepted transparently.
    """

    name = "codex-cli"

    def __init__(self, command: str | None = None, extra_args: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.command = command or DEFAULT_CODEX_COMMAND
        self.extra_args = extra_args if extra_args is not None else DEFAULT_CODEX_ARGS
        self.timeout = timeout

    def run(self, request: VisionRequest) -> VisionResponse:
        prompt = _build_prompt(request)
        cmd = [self.command, "exec", "--skip-git-repo-check"]
        if self.extra_args.strip():
            cmd.extend(shlex.split(self.extra_args))
        for att in request.attachments:
            cmd.extend(["-i", att.path])

        try:
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            msg = (
                f"Codex CLI not found (command: {self.command!r}). "
                "Install it with: npm install -g @openai/codex"
            )
            return VisionResponse(
                answer="",
                backend=self.name,
                stderr=msg,
                exit_code=127,
                error={"type": "backend_not_found", "message": msg},
            )
        except subprocess.TimeoutExpired as exc:
            stderr = ""
            if exc.stderr:
                stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", errors="replace")
            msg = f"backend timed out after {self.timeout}s"
            return VisionResponse(
                answer="",
                backend=self.name,
                stderr=stderr or msg,
                exit_code=124,
                error={"type": "timeout", "message": msg},
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        raw = stdout  # stderr is preserved in the response for debugging but never treated as answer

        # Hard failure: backend produced no stdout.
        if not raw:
            msg = stderr or f"backend exited with code {completed.returncode} and produced no output"
            return VisionResponse(
                answer="",
                backend=self.name,
                raw_text="",
                stderr=stderr,
                exit_code=completed.returncode,
                error={"type": "no_output", "message": msg},
            )

        parsed, parse_error = _safe_json_loads(raw)
        if parsed:
            answer = str(parsed.get("answer", "")).strip() or raw
            return VisionResponse(
                answer=answer,
                confidence=_coerce_float(parsed.get("confidence", 0.0)),
                per_file_findings=_coerce_list(parsed.get("per_file_findings", [])),
                comparison=parsed.get("comparison"),
                backend=self.name,
                raw_text=raw,
                stderr=stderr,
                exit_code=completed.returncode,
                error=None,
            )

        # Degraded: backend returned non-JSON text.  Surface raw output as answer so the
        # agent can still use it, but set error.type so it knows to treat the result with
        # lower confidence and can decide whether to retry or escalate.
        return VisionResponse(
            answer=raw,
            backend=self.name,
            raw_text=raw,
            stderr=stderr,
            exit_code=completed.returncode,
            error={"type": "non_json_output", "message": parse_error or "backend returned non-JSON output"},
        )


# ─── Normalization helpers ───────────────────────────────────────────────────

def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _load_attachments(paths: list[str]) -> list[Attachment]:
    attachments: list[Attachment] = []
    for idx, path in enumerate(paths):
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"file not found: {path}")
        if not p.is_file():
            raise ValueError(f"path is not a file: {path}")
        attachments.append(Attachment(id=f"file{idx + 1}", path=str(p.resolve())))
    return attachments


# ─── CLI helpers ────────────────────────────────────────────────────────────

def _print_json(doc: dict[str, Any]) -> None:
    print(json.dumps(doc, ensure_ascii=False, indent=2))


def _build_backend(args: argparse.Namespace) -> VisionBackend:
    backend = args.backend or DEFAULT_BACKEND
    if backend == "codex-cli":
        return CodexCLIBackend(timeout=args.timeout)
    raise ValueError(f"unsupported backend: {backend}")


def _build_request(args: argparse.Namespace) -> VisionRequest:
    attachments = _load_attachments(args.attachment or [])
    if not attachments:
        raise ValueError("at least one attachment is required")
    if not args.question.strip():
        raise ValueError("question is required")
    return VisionRequest(
        question=args.question.strip(),
        attachments=attachments,
    )


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="see",
        description="Delegate image understanding to a vision model (Codex CLI by default).",
    )
    parser.add_argument("-q", "--question", required=True, help="User question")
    parser.add_argument("-i", "--attachment", action="append", default=[], help="Path to an image or PDF (repeatable)")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="Backend name (default: codex-cli)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Backend timeout in seconds")
    parser.add_argument("--output", default="json", choices=sorted(SUPPORTED_OUTPUTS), help="Output format")
    parser.add_argument("--dry-run", action="store_true", help="Print the request and resolved backend command without executing it")
    return parser


def _parse_args(argv: list[str]) -> argparse.Namespace:
    return _make_parser().parse_args(argv)


def _render_dry_run(request: VisionRequest, backend: VisionBackend, args: argparse.Namespace) -> dict[str, Any]:
    cmd_preview = None
    if isinstance(backend, CodexCLIBackend):
        cmd_preview = [backend.command, "exec"]
        if backend.extra_args.strip():
            cmd_preview.extend(shlex.split(backend.extra_args))
        for att in request.attachments:
            cmd_preview.extend(["-i", att.path])
    return {
        "backend": backend.name,
        "command_preview": cmd_preview,
        "request": {
            "question": request.question,
            "attachments": [{"id": att.id, "path": att.path} for att in request.attachments],
        },
        "timeout": args.timeout,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        request = _build_request(args)
        backend = _build_backend(args)
        if args.dry_run:
            _print_json(_render_dry_run(request, backend, args))
            return 0

        response = backend.run(request)
        if args.output == "text":
            print(response.answer)
        else:
            _print_json(response.to_dict())
        return 0 if response.exit_code == 0 else response.exit_code
    except Exception as exc:
        error_doc = {
            "error": {
                "message": str(exc),
                "type": exc.__class__.__name__,
            }
        }
        if args and getattr(args, "output", "json") == "text":
            print(f"ERROR: {exc}", file=sys.stderr)
        else:
            _print_json(error_doc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
