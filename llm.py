"""Thin wrapper around Ollama's chat API. Swap this file to point at another
self-hosted runtime (vLLM, llama.cpp server); the agent only needs
`chat(messages, tools)` to return the assistant message."""
import re

import httpx

import settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def chat(messages: list[dict], tools: list[dict] | None = None,
         think: bool | None = None) -> dict:
    payload = {
        "model": settings.MODEL,
        "messages": messages,
        "stream": False,
        "think": settings.THINK if think is None else think,
        # temperature 0: the same question should give the same query
        "options": {"temperature": 0, "num_ctx": settings.NUM_CTX},
    }
    if tools:
        payload["tools"] = tools

    resp = httpx.post(settings.OLLAMA_URL, json=payload, timeout=600)
    if resp.status_code == 400 and "think" in resp.text:  # model has no thinking mode
        payload.pop("think")
        resp = httpx.post(settings.OLLAMA_URL, json=payload, timeout=600)
    resp.raise_for_status()

    message = resp.json()["message"]
    message["content"] = _THINK_RE.sub("", message.get("content") or "").strip()
    message.pop("thinking", None)
    return message


def ready() -> str | None:
    """None if the Ollama server is up and the configured model is installed, else the
    reason. Only lists installed models; it does not load or run anything."""
    base = settings.OLLAMA_URL.rsplit("/api/", 1)[0]
    try:
        tags = httpx.get(base + "/api/tags", timeout=5).json()
    except Exception as e:  # noqa: BLE001
        return f"Ollama is not reachable at {base}: {e}"
    names = {m["name"] for m in tags.get("models", [])}
    if settings.MODEL not in names and f"{settings.MODEL}:latest" not in names:
        return f"model {settings.MODEL} is not installed (installed: {', '.join(sorted(names)) or 'none'})"
    return None
