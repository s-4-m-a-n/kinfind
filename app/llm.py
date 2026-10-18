"""Where the AI calls go: Gemini on Google Cloud Vertex AI, or OpenRouter.

- VERTEX_PROJECT set: Gemini on Vertex AI in that Google Cloud project. It's billed to the
  project and signs in as the server's Google identity (on Cloud Run, its service account),
  so there is no API key to keep. VERTEX_LOCATION defaults to 'global'.
- Otherwise OPENROUTER_API_KEY, if set.
- Neither: AI features are off and Kinfind works without them.

Both speak the same OpenAI-style chat format, and Vertex AI uses the same 'google/<model>'
names, so ai_match.py doesn't need to know which one it is using. Image output uses Vertex
AI's own generateContent call, since its OpenAI-style endpoint returns text only.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.request

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
_token_lock = threading.Lock()
_credentials = None


def provider() -> str | None:
    if os.environ.get("VERTEX_PROJECT"):
        return "vertex"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    return None


def enabled() -> bool:
    return provider() is not None


def _vertex_base() -> str:
    location = os.environ.get("VERTEX_LOCATION", "global")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{os.environ['VERTEX_PROJECT']}/locations/{location}"


def _vertex_token() -> str:
    """An access token for the server's Google identity, refreshed when it expires."""
    global _credentials
    import google.auth
    from google.auth.transport.requests import Request

    with _token_lock:
        if _credentials is None:
            _credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _credentials.valid:
            _credentials.refresh(Request())
        return _credentials.token


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def chat(body: dict, timeout: float = 120) -> dict:
    """One OpenAI-style chat completion; returns the response JSON."""
    if provider() == "vertex":
        return _post(f"{_vertex_base()}/endpoints/openapi/chat/completions", body,
                     {"Authorization": f"Bearer {_vertex_token()}"}, timeout)
    return _post(OPENROUTER, body, {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                                    "X-Title": "Kinfind"}, timeout)


def generate_image(model: str, prompt: str, jpeg: bytes, timeout: float = 180) -> bytes:
    """Ask an image model to redraw a photo; returns the new image's bytes."""
    if provider() == "vertex":
        name = model.removeprefix("google/")
        data = _post(f"{_vertex_base()}/publishers/google/models/{name}:generateContent", {
            "contents": [{"role": "user", "parts": [
                {"text": prompt}, {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }, {"Authorization": f"Bearer {_vertex_token()}"}, timeout)
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        image = next((p["inlineData"]["data"] for p in parts if p.get("inlineData")), None)
        if not image:
            text = " ".join(p.get("text", "") for p in parts)
            raise RuntimeError(f"The image model returned no image: {text[:200]}")
        return base64.b64decode(image)

    msg = chat({"model": model, "modalities": ["image", "text"], "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
    ]}]}, timeout)["choices"][0]["message"]
    if not msg.get("images"):
        raise RuntimeError(f"The image model returned no image: {str(msg.get('content'))[:200]}")
    return base64.b64decode(msg["images"][0]["image_url"]["url"].split(",", 1)[1])
