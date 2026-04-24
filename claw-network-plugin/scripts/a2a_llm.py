"""LLM client for A2A — reads OpenClaw's configured provider + key, makes
a direct HTTP call. No OpenClaw agent in the loop.

OpenClaw stores model providers under `models.providers` in either
`~/.openclaw/openclaw.json` or `~/.openclaw/clawdbot.json`. Each provider
declares a `baseUrl`, `apiKey`, `api` ('anthropic-messages' or
'openai-chat-completions'), and a `models` list with the model id to use.

For now we only support `anthropic-messages` (which is what every
deployment we've seen uses — minimax-portal, minimax, anthropic itself,
zai, etc. all expose this format). Add OpenAI-style if a user reports it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def _openclaw_home() -> Path:
    return Path(os.environ.get("OPENCLAW_HOME") or (Path.home() / ".openclaw"))


def _load_config() -> dict:
    """Try to find OpenClaw's config dict. Looks in known locations."""
    home = _openclaw_home()
    for name in ("openclaw.json", "clawdbot.json"):
        p = home / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("models"), dict):
                    return data
            except Exception:
                pass
    # Last resort: empty
    return {}


def _load_auth_profiles() -> dict:
    """OpenClaw stores live OAuth tokens (the real bearer for providers
    like minimax-portal) here, separately from the static models config."""
    home = _openclaw_home()
    candidates = [
        home / "agents" / "main" / "agent" / "auth-profiles.json",
        home / "auth-profiles.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                    return data["profiles"]
            except Exception:
                pass
    return {}


def _resolve_bearer(provider_id: str, provider_cfg: dict) -> str | None:
    """Find the actual bearer token for this provider.

    Order of preference:
      1. If auth-profiles.json has '<provider_id>:default.access' → use that
         (this is the OAuth access token, refreshed by OpenClaw)
      2. If provider_cfg.apiKey looks like a real key (not 'oauth' /
         'placeholder' / etc.) → use that
    """
    profiles = _load_auth_profiles()
    for key, prof in profiles.items():
        if not isinstance(prof, dict):
            continue
        if prof.get("provider") == provider_id and prof.get("access"):
            return str(prof["access"])
    api_key = str(provider_cfg.get("apiKey") or "")
    # Heuristic: if it doesn't look like an actual secret, skip.
    if not api_key or api_key.lower() in ("oauth", "placeholder", "minimax-oauth", "anthropic-oauth", "claude-oauth"):
        return None
    return api_key


def _pick_provider(prefer_id: str | None = None) -> tuple[str | None, dict]:
    """Return (provider_id, provider_config) for the first usable provider.

    Usable = has apiKey AND has models AND `api` is recognized.
    If `prefer_id` is given (e.g. 'minimax-portal') and exists, prefer it.
    """
    cfg = _load_config()
    providers = (cfg.get("models") or {}).get("providers") or {}
    if not isinstance(providers, dict):
        return None, {}
    # Order: prefer requested id, then any with api=anthropic-messages.
    candidate_order = []
    if prefer_id and prefer_id in providers:
        candidate_order.append(prefer_id)
    for pid in providers:
        if pid not in candidate_order:
            candidate_order.append(pid)
    for pid in candidate_order:
        pcfg = providers[pid]
        if not isinstance(pcfg, dict):
            continue
        if not pcfg.get("apiKey") or not pcfg.get("baseUrl"):
            continue
        api = (pcfg.get("api") or "").strip()
        if api not in ("anthropic-messages", "openai-chat-completions", "openai-completions"):
            continue
        models = pcfg.get("models") or []
        if not models:
            continue
        return pid, pcfg
    return None, {}


def _model_id(provider_cfg: dict) -> str:
    """Pick the first model id from the provider's model list."""
    for m in provider_cfg.get("models") or []:
        if isinstance(m, dict) and m.get("id"):
            return str(m["id"])
        if isinstance(m, str) and m:
            return m
    return ""


def call_llm(system: str, user: str, *, max_tokens: int = 1024, timeout: int = 30, retries: int = 1) -> str:
    """Call the user's configured LLM with a system + user prompt.

    Returns the assistant's text reply (empty string on failure).
    """
    pid, pcfg = _pick_provider()
    if not pcfg:
        raise RuntimeError("no usable LLM provider configured in OpenClaw")
    api = (pcfg.get("api") or "").strip()
    base = str(pcfg.get("baseUrl") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    bearer = _resolve_bearer(pid, pcfg)
    model = _model_id(pcfg)
    if not (base and bearer and model):
        raise RuntimeError(f"LLM provider {pid!r} missing baseUrl/auth-token/model")

    # OAuth-based providers (minimax-portal, anthropic-portal, etc.) use
    # Authorization: Bearer. Static-key Anthropic providers also accept
    # Bearer as of recent versions, so we standardize on Bearer + leave
    # x-api-key as fallback. authHeader=true in config also asks for Bearer.
    use_bearer = pcfg.get("authHeader") or pcfg.get("auth") == "bearer" or _looks_like_oauth_token(bearer, pcfg)

    if api == "anthropic-messages":
        url = base + "/v1/messages"
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if use_bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        else:
            headers["x-api-key"] = bearer
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        parse = _parse_anthropic_response
    elif api in ("openai-chat-completions", "openai-completions"):
        url = base + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {bearer}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        parse = _parse_openai_response
    else:
        raise RuntimeError(f"unsupported LLM api: {api!r}")

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            text = parse(data)
            if text:
                return text.strip()
            last_err = RuntimeError(f"LLM returned empty content: {raw[:300]}")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")[:300]
            except Exception:
                err_body = ""
            last_err = RuntimeError(f"LLM HTTP {e.code}: {err_body}")
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(1)
    raise last_err or RuntimeError("LLM call failed")


def _looks_like_oauth_token(token: str, provider_cfg: dict) -> bool:
    """OAuth tokens (minimax-portal etc.) tend to look different from
    static API keys. We default to Bearer when unsure — failing API
    requests are easier to debug than silently using the wrong header."""
    if not token:
        return False
    # Known oauth providers
    pname = (provider_cfg.get("api") or "").lower()
    if "portal" in pname:
        return True
    # Token shape heuristic: long base64-ish, no 'sk-ant-' prefix
    if not token.startswith("sk-ant-") and len(token) > 60:
        return True
    return False


def _parse_anthropic_response(data: dict) -> str:
    """Anthropic /v1/messages response.

    Standard shape: {content: [{type:'text', text:'...'}, ...]}
    Reasoning models (MiniMax M2.x, Claude with extended thinking): may
    only emit `{type:'thinking', thinking:'...'}` blocks if max_tokens
    runs out before the model finishes reasoning. We prefer text blocks
    but fall back to thinking content so callers (e.g. judge prompt)
    can still extract a JSON answer from the chain-of-thought.
    """
    text_parts = []
    thinking_parts = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            thinking_parts.append(str(block.get("thinking") or ""))
    text = "\n".join(p for p in text_parts if p).strip()
    if text:
        return text
    return "\n".join(p for p in thinking_parts if p).strip()


def _parse_openai_response(data: dict) -> str:
    """OpenAI /v1/chat/completions response: {choices:[{message:{content:'...'}}]}"""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "")
