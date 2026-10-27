"""Lord Stylo AI: Ollama backed terminal agent with real tools."""

import atexit
import io
import json
import os
import random
import re
import shlex
import sys
import time

# pipes/redirects default to cp1252 on Windows -> force utf-8
for _s in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import agent as agent_mod
from ollama_client import (
    FREE_MODELS,
    OllamaClient,
    OllamaError,
    load_config,
    save_config,
    validate_key,
)

# ---------- theme ----------
CORAL = "bold #d97757"
DIM = "grey50"
USER_C = "bold #7dd3fc"
OK_C = "#a3e635"
ERR_C = "#f87171"
TOOL_C = "#c4b5fd"

console = Console(legacy_windows=False)
FAST = not console.is_terminal  # piped/headless: skip the animation delays

kb = KeyBindings()
_session = None
HAVE_TTY = sys.stdin.isatty() and sys.stdout.isatty()


def nap(lo, hi=None):
    """Animation delay (instant when output is piped)."""
    if FAST:
        return
    time.sleep(random.uniform(lo, hi if hi is not None else lo))


def _ansi(text, width=None) -> str:
    """Render a rich Text/Group to a raw ANSI string (for prompt_toolkit)."""
    buf = io.StringIO()
    c = Console(
        file=buf,
        force_terminal=True,
        width=width or max(24, console.width),
        legacy_windows=False,
        color_system="truecolor",
    )
    c.print(text, end="")
    return buf.getvalue()


# ---- resize aware layout -------------------------------------------------
# Every printed item is kept here. Width dependent items are stored as
# callables so they can be rebuilt at the terminal's *current* width.
HISTORY = []
_CUR_W = console.width


def remember(renderable):
    HISTORY.append(renderable)


def emit(renderable, show=True):
    """Print a renderable and keep it for later re-layout."""
    remember(renderable)
    if show:
        console.print(renderable() if callable(renderable) else renderable)


def relayout():
    """Redraw everything at the terminal's current width (zoom in/out)."""
    global _CUR_W, _BOX_W
    _BOX_W = max(24, console.width)
    _CUR_W = console.width
    if console.is_terminal:
        console.clear()
    for it in HISTORY:
        console.print(it() if callable(it) else it)


def check_resize():
    """Re-layout the whole UI when the terminal was zoomed/resized."""
    if console.width != _CUR_W:
        relayout()
        return True
    return False


def get_session():
    """prompt_toolkit needs a real console; fall back when piped/headless."""
    global _session
    if _session is None:
        try:
            _session = PromptSession(key_bindings=kb)
        except Exception:
            _session = False
    return _session


@kb.add("enter")
def _(event):
    event.current_buffer.validate_and_handle()


@kb.add("c-d")
def _(event):
    event.current_buffer.text = "/exit"
    event.current_buffer.validate_and_handle()


MODEL = "gpt-oss:120b-cloud"
VER = "cli v16"  # bump on any UI/behaviour change so stale sessions are obvious
CWD = os.path.basename(os.getcwd()) or os.getcwd()
CTX = 12
THEMES = ["default"]

CLIENT = None      # OllamaClient (lazy)
CONVO = None       # agent message history (system + turns)
_COMPACT_MARK = 0  # len(CONVO) at the last compaction

# ---------- session persistence (auto save / auto restore) ----------
SESSION_PATH = os.path.join(
    getattr(agent_mod, "HERE", os.getcwd()), ".session.json"
)
SESSION_KEEP = 80          # newest messages written to disk
_TOOL_SNIP = 1500          # tool results stored truncated


def _trim_for_save(msgs):
    """Keep the newest block, always starting at a user message."""
    msgs = list(msgs)
    while len(msgs) > SESSION_KEEP:
        msgs.pop(0)
        while msgs and msgs[0].get("role") != "user":
            msgs.pop(0)
    return msgs


def _save_session():
    """Write the conversation to .session.json (auto: after every turn)."""
    try:
        msgs = list(CONVO[1:]) if CONVO else []     # system prompt rebuilt fresh
        if not msgs:
            try:
                os.remove(SESSION_PATH)
            except OSError:
                pass
            return False
        msgs = _trim_for_save(msgs)
        for m in msgs:
            if m.get("role") == "tool":
                body = m.get("content") or ""
                if len(body) > _TOOL_SNIP:
                    m["content"] = body[:_TOOL_SNIP] + "\n...(session snip)"
        data = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cwd": os.getcwd(),
            "ctx": CTX,
            "messages": msgs,
        }
        tmp = SESSION_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, SESSION_PATH)
        return True
    except Exception:
        return False


def _load_session():
    """Restore the last session. -> (count, saved_at) or (0, '')."""
    global CONVO, CTX, _COMPACT_MARK
    try:
        with open(SESSION_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return 0, ""
    msgs = data.get("messages") or []
    if not msgs:
        return 0, ""
    CONVO = agent_mod.new_history()      # fresh system + pinned memory
    CONVO.extend(msgs)
    try:
        CTX = max(1, min(99, int(data.get("ctx") or 12)))
    except Exception:
        CTX = 12
    _COMPACT_MARK = len(CONVO)           # don't re-compact instantly
    return len(msgs), str(data.get("saved_at") or "")


def header():
    emit(
        Group(
            Text(""),
            Panel(
                Align.center(
                    Group(
                        Text.from_markup(
                            f"  [bold #d97757]◆ Lord Stylo AI[/]   "
                            f"[{DIM}]{MODEL}[/]"
                        ),
                        Text.from_markup(
                            f"  [{DIM}]cwd: {CWD}  ·  {VER}  ·  "
                            f"type /help[/]"
                        ),
                    )
                ),
                border_style="#d97757",
                padding=(0, 1),
            ),
            Text(""),
        )
    )


def rule(label=""):
    console.print(Rule(Text(label, style=DIM), style="#4a4a4a"))


# ---------- rendering ----------
def user_msg(text):
    console.print()
    console.print(Text("❯ ", style=CORAL + " dim"), end="")
    console.print(Text(text, style=USER_C))


def assistant_start():
    return Text("✻ ", style=CORAL)


def thinking():
    frames = ["   ", ".  ", ".. ", "..."]
    return Group(
        Text.from_markup(f"[{CORAL}]✻[/] [{DIM}]Thinking…[/]"),
    )


class Spin:
    def __init__(self, label="Thinking"):
        self.label = label
        self.i = 0
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __rich__(self):
        self.i = (self.i + 1) % len(self.frames)
        return Text.from_markup(
            f"[{CORAL}]{self.frames[self.i]}[/] [{DIM}]{self.label}…[/]"
        )


def tool_box(name, detail, body=""):
    t = Table.grid(padding=(0, 1))
    t.add_column(style=TOOL_C, no_wrap=True, width=2)
    t.add_column()
    t.add_row("⏺ ", Text(name, style="bold white"))
    for line in body.splitlines() or []:
        t.add_row("", Text(line, style=DIM))
    if detail:
        t.add_row("", Text(detail, style=DIM))
    emit(Panel(t, border_style="#3a3a3a", padding=(0, 1), expand=False))


def _reply_title():
    t = Text()
    t.append("✳ ", style=CORAL)
    t.append("Lord Stylo AI", style="bold white")
    t.append("  ", style="")
    t.append(MODEL, style="dim")
    return t


def _reply_panel(content):
    return Panel(
        content,
        title=_reply_title(),
        title_align="left",
        border_style=CORAL.replace("bold ", ""),
        style="on #14100e",
        padding=(1, 2),
        expand=True,
    )


# ---------- real agent turn (streaming + tools) ----------
def get_client():
    global CLIENT, MODEL
    if CLIENT is None:
        CLIENT = OllamaClient()
        MODEL = CLIENT.model
    return CLIENT


def _tool_detail(name, args):
    a = args or {}
    if name in ("run_command", "powershell", "run_background"):
        return str(a.get("command", ""))[:140]
    if name == "open_in_browser":
        return str(a.get("target", ""))[:140]
    if name == "stop_process":
        return f"pid {a.get('pid', '?')}"
    if name == "read_file":
        return f"{a.get('path', '')}  (from line {a.get('offset', 1)})"
    if name == "count_files":
        return f"{a.get('pattern', '*')}   in {a.get('path', '.')}"
    if name in ("write_file", "list_dir", "delete_path", "file_info",
                "disk_usage", "tree"):
        return str(a.get("path", ""))
    if name == "zip_list":
        return str(a.get("zip_path", ""))
    if name == "zip_extract":
        return f"{a.get('zip_path', '')}  →  {a.get('dest', 'same folder')}"
    if name == "zip_create":
        return f"{a.get('source', '')}  →  {a.get('out', '')}"
    if name == "download":
        return f"{a.get('url', '')}  →  {a.get('dest', '')}"
    if name == "process_list":
        return f"filter: {a.get('filter') or 'all'}"
    if name == "sys_info":
        return "cpu · ram · disk · os"
    if name == "cli_run":
        tail = "  [bg]" if a.get("background") else ""
        return (f"{a.get('tool', '')} {a.get('args', '')}".strip()[:130] + tail)
    if name == "deploy":
        p = a.get("platform", "")
        mode = "prod" if a.get("prod", True) else "preview"
        return f"{p} → {a.get('dir', '.')}  ({mode})"
    if name == "python_exec":
        return (str(a.get("code", ""))[:60] + " …").replace("\n", " ")
    if name == "pip_install":
        return str(a.get("packages", ""))[:140]
    if name == "edit_file":
        return f"{a.get('path', '')}  ·  {repr(str(a.get('old_str', ''))[:50])}"
    if name == "glob":
        return f"{a.get('pattern', '')}   in {a.get('path', '.')}"
    if name == "grep":
        g = f"  {a.get('glob')}" if a.get("glob") else ""
        return f"/{a.get('pattern', '')}/{g}   in {a.get('path', '.')}"
    return "  ".join(f"{k}={str(v)[:40]}" for k, v in list(a.items())[:4])


def _tool_result_lines(out):
    lines = str(out or "").splitlines() or ["(empty)"]
    ok = not lines[0].startswith("error:")
    if len(lines) > 16:
        cut = len(lines) - 16
        lines = lines[:16] + [f"... (+{cut} more lines)"]
    return lines, ok


_TABLE_SEP = re.compile(r"^\|?[\s\-:|]+\|?$")


def _cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _demote_tables(text):
    """Wide markdown tables break the terminal box -> short bullet lines."""
    lines = text.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if (
            ln.lstrip().startswith("|")
            and i + 1 < n
            and _TABLE_SEP.match(lines[i + 1].strip())
        ):
            head = _cells(ln)
            i += 2
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            for r in rows:
                r = r + [""] * max(0, len(head) - len(r))
                pairs = [
                    f"{h}: {v}"
                    for h, v in zip(head, r)
                    if v and not h.startswith("---")
                ]
                if pairs:
                    out.append("- " + ", ".join(pairs))
            out.append("")
            continue
        out.append(ln)
        i += 1
    return "\n".join(out).rstrip()


class TurnUI:
    """Glues the agent callbacks onto the rich UI (spinner / panel / boxes)."""

    def __init__(self):
        self.spin = None
        self.live = None
        self.buf = ""
        self._last = 0.0

    # --- spinner ---
    def status(self, label):
        if FAST:
            emit(Text(f"✻ {label}…", style=CORAL + " " + DIM))
            return
        self._stop_spin()
        self.spin = Live(
            Spin(label), console=console, refresh_per_second=18,
            transient=True,
        )
        self.spin.start()

    def _stop_spin(self):
        if self.spin is not None:
            try:
                self.spin.stop()
            except Exception:
                pass
            self.spin = None

    # --- streaming answer ---
    def text(self, delta):
        if not delta:
            return
        self.buf += delta
        if FAST:
            return
        # ONE transient line only: a tall live panel repeats itself on
        # terminals that hard-wrap wide rows, so we show progress instead
        # and paint the real answer exactly once in flush().
        now = time.time()
        if now - self._last > 0.12:
            self._last = now
            label = f"streaming · {len(self.buf)} chars"
            if self.spin is None:
                self.spin = Live(
                    Spin(label), console=console,
                    refresh_per_second=10, transient=True,
                )
                self.spin.start()
            else:
                try:
                    self.spin.update(Spin(label), refresh=True)
                except Exception:
                    self._stop_spin()

    def flush(self):
        """Stop the progress line, then print the full answer exactly once."""
        if not self.buf:
            return
        self._stop_spin()
        text = _demote_tables(self.buf)
        emit(_reply_panel(Markdown(text, code_theme="monokai")))
        self.buf = ""

    # --- tools ---
    def tool(self, name, args):
        self._stop_spin()
        self.flush()
        if name == "edit_file":
            return          # its result renders as a diff box (emit_diff)
        tool_box(name, _tool_detail(name, args))

    def result(self, name, out):
        diff, out = _split_diff(out)
        if diff:
            emit_diff(diff)
        lines, ok = _tool_result_lines(out)
        if lines != ["(empty)"] or not ok:
            tool_result(name, lines, ok)

    def finish(self):
        self._stop_spin()
        self.flush()

    def die(self):
        self._stop_spin()
        if self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass
            self.live = None
        self.buf = ""


def assistant_run(user_text):
    """One full agent turn: think -> tools -> streamed answer."""
    global CTX
    client = get_client()
    ui = TurnUI()
    mark = len(CONVO)
    ui.status("Thinking")
    hooks = {
        "on_status": ui.status,
        "on_text": ui.text,
        "on_flush": ui.flush,
        "on_tool": ui.tool,
        "on_result": ui.result,
    }
    try:
        agent_mod.run_turn(CONVO, user_text, client, hooks)
    except OllamaError as exc:
        ui.die()
        del CONVO[mark:]
        emit(
            Panel(
                Text(str(exc), style=ERR_C),
                title="api error",
                border_style="#f87171",
                padding=(0, 1),
            )
        )
        return
    except KeyboardInterrupt:
        ui.die()
        del CONVO[mark:]
        emit(Text("stopped", style=DIM))
        return
    except Exception as exc:
        ui.die()
        del CONVO[mark:]
        emit(
            Panel(
                Text(f"{type(exc).__name__}: {exc}", style=ERR_C),
                title="crash",
                border_style="#f87171",
                padding=(0, 1),
            )
        )
        return
    ui.finish()

    pt = (client.stats or {}).get("prompt_eval_count")
    if pt:
        CTX = min(99, max(1, round(pt / 32000 * 100)))
    _maybe_compact(client, ui)


def _maybe_compact(client, ui=None, force=False):
    """Auto/manual compaction: summarize old turns, keep memory on disk."""
    global CTX, _COMPACT_MARK
    if len(CONVO) < (2 if force else agent_mod.COMPACT_MIN):
        if force:
            emit(Text("nothing to compact", style=DIM))
        return
    if not force:
        if CTX < agent_mod.COMPACT_AT and len(CONVO) < 80:
            return
        if len(CONVO) - _COMPACT_MARK < 10:   # already compacted recently
            return
    on_status = ui.status if ui is not None else (lambda s: None)
    on_status("Compacting")
    info = agent_mod.compact(CONVO, client, on_status=on_status, force=force)
    if ui is not None:
        ui._stop_spin()
    if not info:
        if force:
            emit(Text("compaction failed (api) — history kept", style=ERR_C))
        return
    _COMPACT_MARK = len(CONVO)
    CTX = max(5, round(CTX * 0.4))
    mem = len(agent_mod.load_memory())
    pin = info.get("pinned") or 0
    emit(
        Text(
            f"✻ compacted {info['before']} → {info['after']} msgs · "
            f"memory saved .memory.md ({max(1, mem // 1024)} KB)"
            + (f" · pinned +{pin}" if pin else "")
            + f" · ctx≈{CTX}%",
            style=DIM,
        )
    )


def tool_result(name, lines, ok=True):
    color = OK_C if ok else ERR_C
    t = Text()
    for i, ln in enumerate(lines):
        if i:
            t.append("\n")
        t.append("  ⎿ " if i == 0 else "    ", style=DIM)
        t.append(ln, style=color)
    emit(t)


# ---------- diff editor view (edit_file / write_file) ----------
_DIFF_RE = re.compile(r"\[DIFF\]\n?(.*?)\n?\[/DIFF\]", re.S)
_DIFF_MAX = 140


def _split_diff(out):
    """out with a [DIFF]..[/DIFF] block -> (diff_text or None, rest)."""
    m = _DIFF_RE.search(str(out or ""))
    if not m:
        return None, out
    rest = (str(out or "")[: m.start()] + str(out or "")[m.end() :]).strip()
    return m.group(1), rest


def emit_diff(diff):
    """Line-numbered red/- green/+ diff box (opencode style)."""
    base = ""
    rows, old_no, new_no, shown, extra = [], 0, 0, 0, 0
    for raw in diff.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("---"):
            continue
        if line.startswith("+++"):
            base = line[4:].strip()
            continue
        if line.startswith("@@"):
            m = re.search(r"@@ -(\d+)", line)
            old_no = int(m.group(1)) if m else 0
            m = re.search(r" \+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
            if shown < _DIFF_MAX:
                rows.append(Text(f"      {line}", style="italic grey42"))
                shown += 1
            else:
                extra += 1
            continue
        if shown >= _DIFF_MAX:
            extra += 1
            continue
        sign = line[0] if line[:1] else " "
        body = line[1:].rstrip()
        if sign == "-":
            rows.append(
                Text(f"{old_no:<4} {sign} {body}", style="#fecaca on #4a1d1d")
            )
            old_no += 1
        elif sign == "+":
            rows.append(
                Text(f"{new_no:<4} {sign} {body}", style="#bbf7d0 on #14432a")
            )
            new_no += 1
        else:
            rows.append(Text(f"{old_no:<4}   {body}", style="grey45"))
            old_no += 1
            new_no += 1
        shown += 1
    if not rows:
        return
    if extra:
        rows.append(Text(f"      ... (+{extra} lines)", style="grey42"))
    name = os.path.basename(base) or base or "file"
    emit(Text(f"  ← Edit {name}", style=DIM))
    emit(
        Panel(
            Group(*rows),
            border_style="#3a3a3a",
            padding=(0, 1),
            expand=True,
        )
    )


# ---------- commands ----------
HELP = """### Commands

| cmd | action |
|-----|--------|
| `/help` | show this help |
| `/clear` | clear conversation + screen (memory kept) |
| `/compact` | summarize old turns now (auto at 60% ctx) |
| `/pin <fact>` | pin a fact forever (survives compaction) |
| `/image <path> [q]` | vision: gemini reads image → main model replies |
| `/model` | switch free Ollama model |
| `/models` | list free models |
| `/status` | model, key, tokens, session, pinned |
| `/key add <key>` | add an API key (rotation) |
| `/setup` | re-run the key setup popup |
| `/theme` | cycle accent colour |
| `/exit` | quit |

**Session:** auto-saved to `.session.json` after every turn and restored on
start (`.memory.md` keeps pinned facts + summaries across sessions).

**Tools:** `cli_run` `deploy` `python_exec` `check_python` `read_image`
`pip_install` · `run_command` `powershell` `run_background` `wait_for_port`
`stop_process` `open_in_browser` `sys_info` `process_list` · `glob`
`count_files` `grep` `list_dir` `read_file` `write_file` `edit_file`
`delete_path` · `file_info` `disk_usage` `tree` · `zip_list` `zip_extract`
`zip_create` `download`

**Keys:** `Enter` send · `Shift+Enter` newline · `Ctrl+D` exit
"""


def do_command(c):
    global MODEL, CTX, _CUR_W, CORAL, CONVO, _COMPACT_MARK
    if c in ("/exit", "/quit", "/q"):
        emit(Text("bye 👋", style=DIM))
        sys.exit(0)
    if c == "/clear":
        CONVO = agent_mod.new_history()
        CTX = 12
        _COMPACT_MARK = 0
        HISTORY.clear()
        _CUR_W = console.width
        if console.is_terminal:
            console.clear()
        try:
            os.remove(SESSION_PATH)      # fresh start = forget saved session
        except OSError:
            pass
        header()
        mem = len(agent_mod.load_memory())
        emit(
            Text(
                f"conversation cleared · memory kept "
                f"({max(1, mem // 1024)} KB, .memory.md)",
                style=DIM,
            )
        )
        return
    if c.startswith("/pin"):
        fact = c[4:].strip()
        if not fact:
            emit(
                Text(
                    "usage: /pin <fact that must survive forever "
                    "(path, port, decision)>",
                    style=ERR_C,
                )
            )
            return
        added = agent_mod.add_pinned([fact])
        total = agent_mod.pinned_count()
        if added:
            emit(Text(f"pinned · {total} facts in .memory.md", style=OK_C))
        else:
            emit(Text("already pinned", style=DIM))
        return
    if c in ("/compact", "/summarize"):
        _maybe_compact(get_client(), force=True)
        return
    if c.startswith("/setup"):
        first_run_setup(force=True)
        return
    if c.startswith("/image"):
        try:
            bits = shlex.split(c)
        except ValueError:
            bits = c.split()
        args = bits[1:]
        if not args:
            emit(
                Text(
                    'usage: /image "C:/path/pic.png" [your question]',
                    style=ERR_C,
                )
            )
            return
        path = args[0]
        question = " ".join(args[1:])
        prompt = f'Use the read_image tool on "{path}"'
        if question:
            prompt += f" and answer: {question}"
        else:
            prompt += " and tell me what is in it (short bullets)"
        assistant_run(prompt)
        return
    if c == "/help":
        emit(Markdown(HELP))
        return
    if c in ("/model", "/models"):
        _show_models()
        return
    if c.startswith("/model "):
        name = c.split(None, 1)[1].strip()
        client = get_client()
        client.model = name
        MODEL = name
        save_config({"keys": client.keys, "model": name})
        emit(Text(f"model → {name}", style=OK_C))
        return
    if c.startswith("/key"):
        client = get_client()
        parts = c.split()
        if len(parts) >= 3 and parts[1] == "add":
            n = client.add_key(parts[2])
            emit(Text(f"{n} keys loaded (rotation active)", style=OK_C))
        else:
            emit(
                Text(
                    f"{len(client.keys)} keys · active {client.key_label} · "
                    f"usage /key add <key>",
                    style=DIM,
                )
            )
        return
    if c == "/status":
        client = get_client()
        st = client.stats or {}
        lines = [
            f"model      {client.model}",
            f"vision     {getattr(client, 'gemini_vision_model', '?')} "
            f"(primary) -> {client.vision_model} (fallback)",
            f"key        {client.key_label}  (rotations: {client.rotations})",
            f"tokens     prompt={st.get('prompt_eval_count', '-')} "
            f"out={st.get('eval_count', '-')}  ctx≈{CTX}%",
            f"history    {max(0, len(CONVO) - 1)} messages "
            f"(auto-compact at {agent_mod.COMPACT_AT}% ctx)",
            f"session    .session.json auto save/restore ON",
            f"owner      {'ON - owner key active' if agent_mod.OWNER_MODE else 'OFF'}",
            f"memory     {len(agent_mod.load_memory())} chars in .memory.md "
            f"({agent_mod.pinned_count()} pinned)",
            f"tools      {len(agent_mod._PAR_STATS)} parallel batches run",
            f"build      {VER}",
            f"cwd        {os.getcwd()}",
        ]
        emit(Text("\n".join(lines), style=DIM))
        return
    if c == "/theme":
        cols = ["#d97757", "#60a5fa", "#a3e635", "#f472b6", "#facc15"]
        cur = CORAL.replace("bold ", "")
        CORAL = "bold " + cols[(cols.index(cur) + 1) % len(cols)]
        emit(Text("theme cycled", style=OK_C))
        return
    emit(Text(f"unknown command: {c}", style=ERR_C))


def _show_models():
    client = get_client()
    for m in FREE_MODELS:
        mark = "▸ " if m == client.model else "  "
        emit(Text(f"{mark}{m}", style=OK_C if m == client.model else DIM))


def status_bar(width=None):
    """Status bar that drops segments when the terminal gets narrow (zoom)."""
    width = width or console.width
    segs = [
        (" ● ", OK_C),
        (f"{MODEL} ", "bold white"),
        (f"│ {CTX}% ctx ", DIM),
        (f"│ {CWD} ", DIM),
        ("│ ⏎ send ", DIM),
    ]
    t = Text()
    used = 0
    for i, (txt, style) in enumerate(segs):
        if i >= 2 and used + len(txt) > width:
            break
        t.append(txt, style=style)
        used += len(txt)
    return t


_BOX_W = 80  # width captured for the current input round


def _input_top(w, color=DIM, right=" ⏎ send "):
    """╭─ you ───────── ⏎ send ╮  — exactly `w` columns wide."""
    label = " you "
    if w < 16 + len(right):  # too narrow for the hint
        right = ""
    dashes = max(1, w - 3 - len(label) - len(right))
    t = Text()
    t.append("╭", style=color)
    t.append("─", style=color)
    t.append(label, style="bold #7dd3fc")
    t.append("─" * dashes, style=color)
    if right:
        t.append(right, style=DIM)
    t.append("╮", style=color)
    return t


def _input_bottom(w=None, color=DIM):
    w = w or _BOX_W
    return Text("╰" + "─" * max(1, w - 2) + "╯", style=color)


_PULSE = ["#d97757", "#ef9a76", "#ffd2b8", "#ef9a76"]


def show_input_top():
    """Animated input frame; rebuilt from live width on every re-layout."""
    global _BOX_W
    _BOX_W = max(24, console.width)
    if not FAST:
        for i in range(6):
            console.print(
                _input_top(_BOX_W, _PULSE[i % len(_PULSE)]), end="\r"
            )
            time.sleep(0.055)
    # stored as a callable -> re-built with the current width on resize
    emit(lambda: _input_top(max(24, console.width)))


def prompt_text():
    t = Text()
    t.append("│ ", style=DIM)
    t.append("❯ ", style=CORAL)
    return t


def prompt_line(text):
    t = prompt_text()
    t.append(text or "", style=USER_C)
    return t


def prompt_ansi():
    """Coloured '│ ❯ ' prompt as a raw ANSI string."""
    return _ansi(prompt_text())


def read_line():
    """Return one user input line; prompt_toolkit when TTY, input() otherwise."""
    sess = get_session() if HAVE_TTY else False
    if sess:
        try:
            with patch_stdout():
                return sess.prompt(
                    ANSI(prompt_ansi()),
                    multiline=True,
                    prompt_continuation=lambda width, height, size: "│   ",
                )
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            pass  # no usable console -> plain fallback below
    return console.input(prompt_text())


# ---------- first-run setup (popup key entry -> save -> auto restart) -------
def _ask_line(label, allow_empty=False):
    """Read one line: works in a real terminal AND when piped (tests/CI)."""
    try:
        raw = console.input(f"  {label}  › ")
    except (EOFError, KeyboardInterrupt):
        return "" if allow_empty else None
    return raw.strip()


def first_run_setup(force=False):
    """No API key yet -> show a popup-style panel, take keys, save keys.json.
    Returns True if a fresh setup ran (main() then starts normally)."""
    try:
        have = bool(load_config().get("keys"))
    except Exception:
        have = False
    if have and not force:
        return False

    panel = Panel(
        Text.from_markup(
            "[bold]Welcome to Lord Stylo AI![/]\n\n"
            "No API key found — let's add your free key first.\n\n"
            "  1.  open  [bold cyan]https://ollama.com/settings/keys[/]\n"
            "  2.  press [bold]Create Key[/] and copy it\n"
            "  3.  paste it below and hit Enter\n\n"
            "Optional: [dim]Gemini key for fast image reading — press Enter to"
            " skip[/]"
        ),
        title=" setup  ·  first run ",
        title_align="center",
        border_style=CORAL.replace("bold ", ""),
        padding=(1, 2),
    )
    emit(panel)

    try:
        cfg = load_config()
        keys = list(cfg.get("keys") or [])
        while True:
            raw = _ask_line("[bold]Ollama API key[/]")
            if raw is None:
                emit(Text("setup cancelled — /setup kabhi bhi chalao", style=DIM))
                return False
            if not raw:
                emit(Text("  ✗ key chahiye — paste karo ya Enter dabao", style=ERR_C))
                continue
            ok, why = validate_key(raw)
            if not ok:
                emit(Text(f"  ✗ {why}", style=ERR_C))
                continue
            emit(Text(f"  ✓ {why}", style=OK_C))
            key = raw                  # verified key -> becomes owner key
            if key not in keys:
                keys.append(key)
            break

        gem = _ask_line("[bold]Gemini key (optional)[/]", allow_empty=True) or ""
        if gem:
            ok, why = validate_key(gem)
            if not ok:
                emit(Text(f"  ✗ gemini skipped: {why}", style=ERR_C))
                gem = ""
            else:
                emit(Text(f"  ✓ gemini {why}", style=OK_C))
    except Exception as exc:            # never crash the CLI over setup
        emit(Text(f"setup failed: {exc}", style=ERR_C))
        return False

    save_config(
        {
            "keys": keys,
            "owner_keys": [key],          # the pasted key = owner key
            **({"gemini_key": gem} if gem else {}),
        }
    )

    # "restart": drop the old client and boot the UI fresh with new keys
    global CLIENT, MODEL
    CLIENT = None
    MODEL = ""
    get_client()
    if console.is_terminal:
        console.clear()
    emit(Text(f"  ✓ {len(keys)} key(s) saved → keys.json · starting…", style=OK_C))
    return True


def apply_owner():
    """owner_keys (from wizard / keys.json) -> agent owner mode."""
    cfg = load_config()
    keys = set(cfg.get("keys") or [])
    owner = [k for k in (cfg.get("owner_keys") or []) if k in keys]
    agent_mod.set_owner(bool(owner))
    return bool(owner)


def main():
    global CTX, CONVO
    if os.name == "nt":
        os.system("")
    first_run_setup()               # no key yet? popup -> key -> auto start
    apply_owner()                   # pasted key -> owner mode
    CONVO = agent_mod.new_history()
    get_client()
    atexit.register(_save_session)     # auto-save on any exit
    header()
    n, stamp = _load_session()
    if n:
        emit(
            Text(
                f"✻ session restored: {n} msgs from {stamp} · "
                f"/clear = fresh start",
                style=DIM,
            )
        )
    emit(
        Text.from_markup(
            f"  [{DIM}]ask me anything — /help for commands[/]"
        )
    )

    while True:
        # zoom/resize while idle -> rebuild the whole screen at new width
        check_resize()

        emit(Text(""))
        emit(status_bar())
        show_input_top()
        try:
            raw = read_line()
        except (KeyboardInterrupt, EOFError):
            emit(lambda: _input_bottom())
            emit(Text("bye 👋", style=DIM))
            return

        # remember the typed line, then rebuild if the terminal was zoomed
        remember(prompt_line(raw.replace("\n", " ")))
        check_resize()
        emit(lambda: _input_bottom())

        text = raw.strip()
        if not text:
            continue
        if text.startswith("/"):
            do_command(text)
            _save_session()
            continue

        # real agent turn: stream + tool calls
        assistant_run(text)
        _save_session()


if __name__ == "__main__":
    main()
