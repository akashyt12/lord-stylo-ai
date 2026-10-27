"""Ollama cloud client: streaming chat + automatic API key rotation."""

import json
import os
import re
import socket
import threading
import time
import requests

# --- speed: prefer IPv4 - on many Windows boxes a dangling IPv6 route
# stalls the TLS handshake for tens of seconds before falling back ---
try:
    import urllib3.util.connection as _uc

    _uc.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "keys.json")
API = "https://ollama.com/api/chat"

# models available on the Free plan (verified live)
FREE_MODELS = [
    "gpt-oss:120b-cloud",
    "gpt-oss:20b-cloud",
    "gemma4:31b-cloud",
    "nemotron-3-super",
]
DEFAULT_MODEL = FREE_MODELS[0]
# vision helper model: reads images, its text goes to the main model
VISION_MODEL = "gemma4:31b-cloud"

# PRIMARY vision = free Gemini API (fast, top OCR) -> falls back to Ollama
# gemma4 whenever the Gemini call fails (403/429/503/timeout/...)
GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
GEMINI_VISION_MODEL = "gemini-3.5-flash"

_ROTATE_STATUS = (401, 402, 429, 500, 502, 503, 504)
# no baked-in keys: fresh installs get theirs from the first-run wizard
# (main.py /setup) which writes them into keys.json
_FALLBACK_KEYS = []


def validate_key(key, timeout=30):
    """(ok, reason) — one tiny chat call checks the key is live.
    offline (network error) -> ok=True (Termux/airplane mode friendly)."""
    key = (key or "").strip()
    if not key:
        return False, "key khali hai"
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "options": {"num_predict": 1},
    }
    try:
        resp = requests.post(
            API,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=(8, timeout),
        )
    except Exception as exc:
        return True, f"network issue — key skip check ({exc.__class__.__name__})"
    if resp.status_code in (401, 402, 403):
        return False, f"invalid key (http {resp.status_code})"
    if resp.status_code >= 400 and resp.status_code < 500:
        tail = (resp.text or "").strip()[:120]
        return False, f"http {resp.status_code}: {tail}"
    if resp.status_code >= 500:
        return True, f"server busy ({resp.status_code}) — key accepted"
    return True, "verified"


class OllamaError(Exception):
    """Fatal API error (no keys left / network dead / bad request)."""


class _Rotate(Exception):
    """Quota/key problem -> try the next key."""

    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def load_config():
    try:
        with open(CONFIG, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    keys = cfg.get("keys") or list(_FALLBACK_KEYS)
    owner = cfg.get("owner_keys")
    if not owner:
        owner = [keys[0]] if keys else []     # first key = owner key
    return {
        "keys": keys,
        "owner_keys": list(owner),
        "model": cfg.get("model") or DEFAULT_MODEL,
        "vision_model": cfg.get("vision_model") or VISION_MODEL,
        "gemini_key": cfg.get("gemini_key") or "",
        "gemini_vision_model": cfg.get("gemini_vision_model")
        or GEMINI_VISION_MODEL,
    }


def save_config(cfg):
    """Merge into the existing keys.json so unknown fields survive."""
    try:
        old = {}
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as fh:
                old = json.load(fh)
        except Exception:
            old = {}
        old.update(cfg)
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(old, fh, indent=2)
    except Exception:
        pass


class OllamaClient:
    def __init__(self, model=None):
        cfg = load_config()
        self.keys = list(cfg["keys"])
        self.model = model or cfg["model"]
        self.vision_model = cfg["vision_model"]        # ollama fallback
        self.gemini_key = cfg.get("gemini_key", "")      # free Gemini vision
        self.gemini_vision_model = cfg.get(
            "gemini_vision_model", GEMINI_VISION_MODEL
        )
        self.idx = 0          # active key index
        self.rotations = 0    # how many times we had to switch keys
        self.stats = {}       # last request token counts
        try:
            threading.Thread(target=self._warm, daemon=True).start()
        except Exception:
            pass

    # ---- pooled keep-alive connection (no DNS+TLS handshake per call) ----
    def _session(self):
        s = getattr(self, "_sess", None)
        if s is None:
            s = requests.Session()
            ad = requests.adapters.HTTPAdapter(
                pool_connections=6, pool_maxsize=10
            )
            s.mount("https://", ad)
            s.mount("http://", ad)
            self._sess = s
        return s

    def _warm(self):
        """Background: resolve DNS + open TLS once so the FIRST real call
        (a vision read, a chat) skips the handshake instead of paying it."""
        for url in (
            "https://generativelanguage.googleapis.com/",
            "https://ollama.com/",
        ):
            try:
                self._session().get(url, timeout=(8, 8))
            except Exception:
                pass

    # ---- keys ----
    @property
    def key(self):
        if not self.keys:
            raise OllamaError("keys.json me koi API key nahi hai")
        return self.keys[self.idx % len(self.keys)]

    @property
    def key_label(self):
        return f"key#{(self.idx % len(self.keys)) + 1}/{len(self.keys)}"

    def rotate(self):
        if not self.keys:
            raise OllamaError("sari API keys khatam / koi key nahi")
        self.idx = (self.idx + 1) % len(self.keys)
        self.rotations += 1

    def add_key(self, key):
        key = (key or "").strip()
        if key and key not in self.keys:
            self.keys.append(key)
            save_config({"keys": self.keys, "model": self.model})
        return len(self.keys)

    # ---- chat ----
    def chat(
        self, messages, tools=None, temperature=0.6, timeout=180, model=None
    ):
        """Stream a completion. `model` overrides the default (vision calls).

        Yields events:
          {"type": "text", "text": str}
          {"type": "tool_calls", "calls": [...]}
          {"type": "done", "stats": {...}}
        """
        if not self.keys:
            raise OllamaError("keys.json me koi API key nahi hai")
        attempts = 0
        server_tries = 0
        limit = max(4, len(self.keys) * 3)
        while True:
            try:
                yield from self._once(
                    messages, tools, temperature, timeout, model
                )
                return
            except _Rotate as exc:
                attempts += 1
                status = getattr(exc, "status", 0)
                # 5xx = server side hiccup -> retry the SAME key first
                if status in (500, 502, 503) and server_tries < 2:
                    server_tries += 1
                    time.sleep(1.5 * server_tries)
                    continue
                if attempts >= limit:
                    raise OllamaError(
                        f"API problem (http {status or '?'}) — sab keys "
                        f"try ho gayi. Thoda der baad retry karo, ya "
                        f"/key add <key> se nayi key daalo"
                    )
                self.rotate()

    # ---- gemini vision: PRIMARY image reader (free tier, top OCR) ----
    def gemini_vision(self, b64, mime, prompt, timeout=60, retries=1,
                      budget=30):
        """Gemini call with a HARD time budget + keep-alive connection.

        `budget` caps the WHOLE gemini phase (attempt 1 + retries) so a
        hung network can never stall the turn for minutes; the caller then
        falls back to gemma immediately.
        """
        if not self.gemini_key:
            raise OllamaError("keys.json me 'gemini_key' nahi hai")
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64}},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1200,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.gemini_key,
        }
        url = GEMINI_API.format(model=self.gemini_vision_model)
        deadline = time.time() + max(6.0, float(budget or 30))
        retries = max(0, int(retries or 0))
        sess = self._session()
        last = "gemini failed"
        for attempt in range(retries + 1):
            remain = deadline - time.time()
            if remain < 4.0:
                last = last if last != "gemini failed" else (
                    f"gemini: time budget {int(budget)}s used"
                )
                break
            ct = min(8.0, remain)                 # connect: fail reasonably fast
            rt = min(float(timeout or 60), remain - 0.5)
            try:
                resp = sess.post(url, headers=headers, json=body,
                                 timeout=(ct, rt))
            except requests.RequestException as exc:
                last = f"gemini network: {exc}"
                if attempt < retries:
                    time.sleep(min(1.0, max(0.2, remain / 8)))
                    continue
                raise OllamaError(last) from exc
            if resp.status_code != 200:
                try:
                    msg = resp.json()["error"]["message"][:160]
                except Exception:
                    msg = resp.text[:160].replace("\n", " ")
                last = f"gemini http {resp.status_code}: {msg}"
                retryable = resp.status_code in (408, 429, 500, 502, 503, 504)
                if retryable and attempt < retries and (
                    deadline - time.time() > 6
                ):
                    time.sleep(min(1.5, max(0.3, (deadline - time.time()) / 8)))
                    continue
                raise OllamaError(last)
            try:
                parts = resp.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except Exception as exc:
                raise OllamaError(f"gemini bad reply: {exc}") from exc
            if not text:
                last = "gemini empty reply"
                if attempt < retries and deadline - time.time() > 6:
                    continue
                raise OllamaError(last)
            return text
        raise OllamaError(last)

    def _once(self, messages, tools, temperature, timeout, model=None):
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._session().post(
                API,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(10, timeout),
            )
        except requests.RequestException as exc:
            raise OllamaError(f"network error: {exc}")

        if resp.status_code != 200:
            body = ""
            try:
                body = resp.text[:400]
            except Exception:
                pass
            resp.close()
            if resp.status_code in _ROTATE_STATUS:
                raise _Rotate(
                    f"http {resp.status_code}: {body[:120]}", resp.status_code
                )
            if resp.status_code == 400 and re.search(
                r"length|too long|limit|context", body, re.I
            ):
                raise OllamaError(
                    "context too long — /clear karke nayi shuruat karo "
                    f"({body[:160]})"
                )
            raise OllamaError(f"http {resp.status_code}: {body[:300]}")

        pending = {}   # index -> tool call
        stats = {}
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                msg = data.get("message") or {}
                content = msg.get("content") or ""
                if content:
                    yield {"type": "text", "text": content}
                for n, tc in enumerate(msg.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    key = tc.get("id") or fn.get("name") or str(n)
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"_raw": args}
                    pending[key] = {
                        "id": tc.get("id") or f"call_{key}",
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": args,
                        },
                    }
                if data.get("done"):
                    for k in (
                        "prompt_eval_count",
                        "eval_count",
                        "total_duration",
                        "done_reason",
                    ):
                        if k in data:
                            stats[k] = data[k]
        except requests.RequestException as exc:
            raise OllamaError(f"stream break: {exc}")
        finally:
            resp.close()

        self.stats = stats
        if pending:
            yield {"type": "tool_calls", "calls": list(pending.values())}
        yield {"type": "done", "stats": stats}
