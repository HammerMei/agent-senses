---
name: see
description: Delegate visual understanding to a vision model. Accepts images and PDFs as attachments, answers questions based on visible evidence, and returns structured JSON. Backend-neutral by design; defaults to Codex CLI (GPT-4o).
model_tier: strong
---

# see

Use `scripts/see.py` to let an agent without native vision analyze images.

## What this skill does

- Accepts **one or more images** in a single request
- Answers questions based strictly on visible evidence
- Compares multiple images
- Returns structured JSON for reliable agent consumption
- Keeps the backend **pluggable** so Codex CLI can be swapped for another SDK later

## Operating rules

- Never invent details that are not visible in the images
- If something is blurry, occluded, cropped, or ambiguous, say so explicitly
- With multiple images, refer to each by its auto-assigned ID (`img1`, `img2`, …)
- Return structured JSON output

## CLI

```bash
# Images
python3 scripts/see.py -q "Which looks better?" -i a.jpg -i b.jpg

# PDF
python3 scripts/see.py -q "Summarise this document" -i report.pdf

# Mixed
python3 scripts/see.py -q "Does the screenshot match the spec?" -i spec.pdf -i screenshot.png
```

The backend defaults to Codex CLI. Override via env vars if needed:
- `SEE_TIMEOUT` — backend timeout in seconds (default: 120)
- `SEE_BACKEND` — backend name (default: `codex-cli`)
- `SEE_CODEX_COMMAND` — codex executable path (default: `codex`)

## Output contract

Always returns JSON. Check `error` first.

| Field | Type | Description |
|-------|------|-------------|
| `error` | object \| null | `null` on success. Set on failure — check `error.type`. |
| `answer` | string | Vision answer, or raw backend text if JSON parsing failed. |
| `confidence` | float | Model-reported confidence (0–1). |
| `per_file_findings` | array | Per-attachment structured findings. |
| `comparison` | object \| null | Cross-image comparison result. |
| `stderr` | string | Backend stderr (preserved for debugging). |
| `exit_code` | int | Backend process exit code. |
| `raw_text` | string | Raw backend stdout (preserved for debugging). |

### `error.type` values

| `error.type` | Meaning | Agent action |
|---|---|---|
| `backend_not_found` | `codex` binary missing | Run `npm install -g @openai/codex`, then retry |
| `timeout` | Backend timed out | Retry with fewer images or increase `SEE_TIMEOUT` |
| `no_output` | Backend produced no stdout | Check `stderr` for clues; escalate |
| `non_json_output` | Backend returned plain text; `answer` has raw fallback | Use `answer` with caution; consider retry |

## Notes for agents

- Use this skill when the base model does not have native vision
- Prefer one multi-image request over repeated round-trips
