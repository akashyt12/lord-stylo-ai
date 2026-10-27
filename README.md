# Lord Stylo AI

An AI agent that runs in your terminal — streaming replies, real tool
execution (shell, files, background servers, deploys), auto memory
compaction, session restore, image reading and **free model APIs**.

Works on **Windows, Termux (Android), Linux** — anything with Python 3.9+.

```
┌──────────────────────────────────────────────┐
│  ◆ Lord Stylo AI   gpt-oss:120b-cloud        │
│  cwd: my-project  ·  type /help              │
├──────────────────────────────────────────────┤
│  > make a folder and put hello.txt in it     │
│                                              │
│  ● run_command  mkdir demo && echo hi > ...  │
│  ● write_file   demo/hello.txt               │
│  │  done — folder banaya aur hello.txt daali │
└──────────────────────────────────────────────┘
```

## Quick start

### Windows

```powershell
git clone https://github.com/akashyt12/lord-stylo-ai.git
cd lord-stylo-ai
pip install -r requirements.txt
py main.py
```

### Termux (Android)

```bash
pkg update && pkg install python git
git clone https://github.com/akashyt12/lord-stylo-ai.git
cd lord-stylo-ai
pip install -r requirements.txt
python main.py
```

### Linux / macOS

```bash
git clone https://github.com/akashyt12/lord-stylo-ai.git
cd lord-stylo-ai
python3 -m pip install -r requirements.txt
python3 main.py
```

## First run = key setup (popup)

On the very first start a setup panel opens automatically:

1. open <https://ollama.com/settings/keys> → **Create Key** → copy
2. paste it in the prompt (it is verified live before saving)
3. optionally paste a Gemini key for image reading — or press Enter to skip
4. keys are saved to `keys.json` and the CLI starts immediately

Done once — next starts go straight to the chat. Re-run it anytime with
`/setup`. No keys are shipped with this repo; you bring your own (free plan
works).

## Commands

| cmd | action |
|-----|--------|
| `/help` | show help |
| `/clear` | clear conversation + screen (memory kept) |
| `/compact` | summarize old turns now (auto at 60% ctx) |
| `/pin <fact>` | pin a fact forever (survives compaction) |
| `/image <path> [q]` | vision: gemini reads image → main model replies |
| `/model` `/models` | switch / list free models |
| `/status` | model, key, tokens, session, pinned |
| `/key add <key>` | add an API key (rotation) |
| `/setup` | re-run the key setup popup |
| `/theme` | cycle accent colour |
| `/exit` | quit |

**Keys:** `Enter` send · `Shift+Enter` newline · `Ctrl+D` exit

## Features

- **Streaming UI** — replies render token-by-token, tool calls in panels
- **Real tools** — shell (PowerShell/bash), background servers + `wait_for_port`,
  file read/write/edit, glob/grep, zip, download, pip install, python exec with
  syntax verdict, process control, deploy helpers
- **Parallel tool calls** — multiple tools in one turn run concurrently
- **Free models** — `gpt-oss:120b-cloud`, `gpt-oss:20b-cloud`,
  `gemma4:31b-cloud`, `nemotron-3-super` (Ollama free plan) + key rotation
- **Image reading** — free Gemini (`gemini-3.5-flash`) first, Ollama `gemma4`
  fallback, auto-downscaled via Pillow
- **Memory** — auto-compact at 60% context, `.memory.md` summaries,
  `/pin` facts that never get compacted away
- **Session restore** — conversation auto-saved to `.session.json`,
  continues after restart (`/clear` = fresh start)
- **Python-first** — code it writes is compiled/checked before it claims done

## Config files (created locally, never committed)

| file | what |
|------|------|
| `keys.json` | your API keys (from `keys.example.json` template) |
| `.memory.md` | pinned facts + compaction summaries |
| `.session.json` | saved conversation for restore |

## License

MIT — use it, modify it, ship it.
