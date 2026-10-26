"""Transports for model-backed players: the Anthropic Messages API over urllib (the project is stdlib-only), a
fake for tests, and a sample cache that makes a model player deterministic in (snapshot, seed) the way spec §12
asks: the first call for a key goes to the model, every later call for the same key returns the stored reply."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
RETRYABLE = {408, 409, 429, 500, 529}


class TransportError(Exception):
    def __init__(self, status: int | None, kind: str, message: str):
        super().__init__(f"{kind} ({status}): {message}")
        self.status, self.kind, self.message = status, kind, message


class AnthropicTransport:
    """POST /v1/messages. Retries 429/5xx with the server's retry-after or exponential backoff."""
    name = "anthropic"

    def __init__(self, api_key_env: str = "ANTHROPIC_API_KEY", url: str = API_URL, timeout_s: int = 600,
                 retries: int = 4, betas: tuple[str, ...] = ()):
        self.api_key_env, self.url, self.timeout_s, self.retries, self.betas = api_key_env, url, timeout_s, retries, betas

    def complete(self, body: dict) -> dict:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise TransportError(None, "no_api_key", f"environment variable {self.api_key_env} is not set")
        betas = list(self.betas)
        if body.get("fallbacks") == "default":
            betas.append("server-side-fallback-2026-07-01")
        elif isinstance(body.get("fallbacks"), list):
            betas.append("server-side-fallback-2026-06-01")
        headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": API_VERSION}
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        data = json.dumps(body).encode()
        delay = 2.0
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                try:
                    err = json.loads(text)["error"]
                    kind, msg = err.get("type", "http_error"), err.get("message", text)
                except (ValueError, KeyError, TypeError):
                    kind, msg = "http_error", text[:500]
                if e.code not in RETRYABLE or attempt == self.retries:
                    raise TransportError(e.code, kind, msg) from None
                wait = e.headers.get("retry-after")
                time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else delay)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == self.retries:
                    raise TransportError(None, "connection_error", str(e)) from None
                time.sleep(delay)
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")


class FakeTransport:
    """Tests install a responder: fn(body) -> response dict (or raises TransportError). Without one, every call
    answers a single log entry and reports a nominal token usage, so a model-player batch can be rehearsed end to
    end (worker processes, sample caches, budget guard) without an API key."""
    name = "fake"
    responder = None
    DEFAULT_USAGE = {"input_tokens": 1000, "output_tokens": 50}

    def complete(self, body: dict) -> dict:
        if FakeTransport.responder is None:
            return text_reply(json.dumps({"orders": [{"type": "log", "text": "fake reply"}]}), model=body.get("model", "fake"),
                              **FakeTransport.DEFAULT_USAGE)
        return FakeTransport.responder(body)


TRANSPORTS = {"anthropic": AnthropicTransport, "fake": FakeTransport}


def text_reply(text: str, model: str = "fake", stop_reason: str = "end_turn", **usage) -> dict:
    """Shape a Messages API response (for fakes and docs)."""
    return {"model": model, "stop_reason": stop_reason, "stop_details": None,
            "content": [{"type": "text", "text": text}] if text is not None else [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, **usage}}


def response_text(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def usage_of(resp: dict) -> dict:
    u = resp.get("usage") or {}
    return {"input": u.get("input_tokens", 0) or 0, "output": u.get("output_tokens", 0) or 0,
            "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            "cache_create": u.get("cache_creation_input_tokens", 0) or 0}


class SampleCache:
    """runs/<run>/samples/<key>.json: the recorded reply for (player config, snapshot, seed)."""

    def __init__(self, directory: Path | None):
        self.dir = Path(directory) if directory else None

    def path(self, key: str) -> Path | None:
        return self.dir / f"{key}.json" if self.dir else None

    def get(self, key: str) -> dict | None:
        p = self.path(key)
        return json.loads(p.read_text()) if p and p.exists() else None

    def put(self, key: str, sample: dict) -> None:
        p = self.path(key)
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(sample, indent=1, sort_keys=True))
