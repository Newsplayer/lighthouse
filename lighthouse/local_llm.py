"""A free, open model on this machine, for the builder's gap fill (llm_fill.py).

Server: starts llama.cpp's llama-server with a GGUF model file, on 127.0.0.1 only, and stops it again.
Remote: an OpenAI-compatible server that's already running on this machine (Ollama, LM Studio, llama.cpp).
Both answer chat(messages, schema, max_tokens) -> (dict or None, usage) with the answer held to the JSON
schema by the server's grammar, at temperature 0, with the model's thinking turned off.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests

log = logging.getLogger("lighthouse.local_llm")


class Remote:
    def __init__(self, url: str, name: str = "local", parallel: int = 1):
        self.url = url.rstrip("/")
        if not self.url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            raise ValueError("the gap fill only talks to a model on this machine (127.0.0.1)")
        self.name = name
        self.parallel = max(1, parallel)
        self.http = requests.Session()

    def chat(self, messages: list[dict], schema: dict, max_tokens: int = 200) -> tuple[dict | None, dict]:
        payload = {"model": self.name, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
                   "response_format": {"type": "json_schema", "json_schema": {"name": "facts", "strict": True,
                                                                              "schema": schema}},
                   "chat_template_kwargs": {"enable_thinking": False}}
        r = self.http.post(f"{self.url}/v1/chat/completions", json=payload, timeout=180)
        r.raise_for_status()
        res = r.json()
        text = (res.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            ans = json.loads(text)
        except ValueError:
            return None, res.get("usage") or {}
        return (ans if isinstance(ans, dict) else None), res.get("usage") or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server(Remote):
    """llama-server from llama.cpp, started for the duration of a `with` block."""

    def __init__(self, exe: str | Path, model: str | Path, parallel: int = 2, context: int = 4096,
                 threads: int | None = None, start_seconds: float = 300):
        self.exe, self.model = Path(exe), Path(model)
        self.port = _free_port()
        super().__init__(f"http://127.0.0.1:{self.port}", self.model.stem, parallel)
        self.context, self.threads, self.start_seconds = context, threads or os.cpu_count() or 4, start_seconds
        self.proc = None

    def __enter__(self):
        env = {**os.environ, "LD_LIBRARY_PATH": f"{self.exe.parent}:{os.environ.get('LD_LIBRARY_PATH', '')}"}
        # Each parallel slot gets context/parallel tokens; the prompts are ~600 tokens, so 2048 each is plenty.
        cmd = [str(self.exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(self.context * self.parallel), "-np", str(self.parallel), "-t", str(self.threads),
               "--jinja"]
        # Its log goes to a file: a pipe nobody reads fills up and stalls the server.
        self.log = tempfile.NamedTemporaryFile("w+", prefix="llama-server-", suffix=".log", delete=False)
        self.proc = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.start_seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server stopped: {self.log_tail()}")
            try:
                if self.http.get(f"{self.url}/health", timeout=5).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(1)
        self.__exit__(None, None, None)
        raise RuntimeError("llama-server didn't start in time")

    def log_tail(self, chars: int = 600) -> str:
        try:
            return Path(self.log.name).read_text(errors="replace")[-chars:]
        except OSError:
            return ""

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        super().__exit__(*exc)
        if getattr(self, "log", None):
            self.log.close()
            Path(self.log.name).unlink(missing_ok=True)
