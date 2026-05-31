# agent-senses

Sensory skills for LLM agents — give your agent the ability to see, listen, read, and more.

Agents without native multimodal support can delegate perception tasks to capable backends via these skills. Each skill is a thin CLI wrapper that returns structured JSON, making it easy to compose with any agent framework.

## Skills

| Skill | Description | Status |
|-------|-------------|--------|
| [`see`](see/) | Analyze images and PDFs — answer questions, compare, extract text | ✅ Ready |
| `listen` | Transcribe audio | 🔜 Planned |
| `read` | Extract and summarize documents | 🔜 Planned |
| `watch` | Analyze video | 🔜 Planned |

## Install

```bash
git clone https://github.com/HammerMei/agent-senses.git
cd agent-senses
./install.sh
```

Restart your Claude Code session after installing.

### Dependencies

- [`codex`](https://github.com/openai/codex) CLI — `npm install -g @openai/codex`

## Usage

### see

```bash
# Analyze an image
see -q "What is in this image?" -i photo.jpg

# Compare two images
see -q "Which looks better?" -i before.jpg -i after.jpg

# Analyze a PDF
see -q "Summarize this document" -i report.pdf
```

Returns structured JSON:
```json
{
  "error": null,
  "answer": "...",
  "confidence": 0.9,
  "per_file_findings": [{"file_id": "file1", "finding": "..."}],
  "comparison": null
}
```

Always check `error` first — `null` means success.

## Design

- **Backend-neutral** — defaults to Codex CLI (GPT-4o via your OpenAI subscription), swappable via env vars
- **No API billing** — uses your existing OpenAI subscription, not API tokens
- **Structured output** — JSON with a consistent error contract so agents can branch reliably
- **Agent-friendly** — short flags (`-q`, `-i`), no unnecessary options
