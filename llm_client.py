"""
llm_client.py — pick which free LLM writes the pitches, and fall back between them.

Providers
  * Gemini (Google AI Studio free tier)      -> gemini_client.GeminiClient
  * Groq   (OpenAI-compatible free tier)     -> GroqClient below

Configured from .env:
  LLM_PROVIDER = auto | gemini | groq     (default auto: use every provider that has a key)
  GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MIN_INTERVAL_SECONDS
  GROQ_API_KEY,   GROQ_MODEL,   GROQ_MIN_INTERVAL_SECONDS

Fallback rule (FallbackClient): if a provider answers with a server error (e.g. the 503
you saw), a rate/daily limit, a missing model or a bad key, the SAME pitch is retried on the
next provider instead of failing. A provider that hit its daily limit or has a bad key is
skipped for the rest of the run. Nothing here bypasses any limit: it just uses the free
allowance you have on each service.

All clients share one interface: `await generate_json(system, prompt, temperature)` ->
(dict, model_name), raising GeminiError (kinds: auth | daily_limit | rate_limit |
model_unavailable | blocked | bad_response | bad_request | server | network).
"""

import asyncio
import os
import time

from gemini_client import GeminiClient, GeminiError, httpx_transport, parse_json_loose

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq changes its free models from time to time, so these are only defaults;
# set GROQ_MODEL in .env (comma-separated list allowed) to override.
GROQ_DEFAULT_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


class GroqClient:
    name = "groq"

    def __init__(self, api_key: str, model: str | None = None, transport=None,
                 sleep=asyncio.sleep, min_interval: float = 8.0, max_retries: int = 3):
        self.api_key = api_key
        models = [m.strip() for m in (model or "").split(",") if m.strip()]
        self.models = models or list(GROQ_DEFAULT_MODELS)
        self.transport = transport or httpx_transport
        self._sleep = sleep
        self.min_interval = float(min_interval)
        self.max_retries = max_retries
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._idx = 0

    @property
    def model(self) -> str:
        return self.models[min(self._idx, len(self.models) - 1)]

    async def _pace(self):
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await self._sleep(wait)
            self._last_call = time.monotonic()

    async def generate_json(self, system: str, prompt: str, temperature: float = 0.7):
        while self._idx < len(self.models):
            model = self.models[self._idx]
            try:
                return await self._call(model, system, prompt, temperature), model
            except GeminiError as e:
                if e.kind == "model_unavailable":
                    self._idx += 1
                    continue
                raise
        raise GeminiError("model_unavailable", "No Groq model available to this API key.")

    @staticmethod
    def _err(body) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        return str(err.get("message", "")) if isinstance(err, dict) else ""

    async def _call(self, model, system, prompt, temperature):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                # JSON mode on Groq requires the word "JSON" to appear in the messages.
                {"role": "user", "content": prompt + "\n\nReply with a single valid JSON object only."},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_completion_tokens": 3000,
        }
        parse_failures = 0
        for attempt in range(self.max_retries + 1):
            await self._pace()
            try:
                status, body, hdrs = await self.transport("POST", GROQ_URL, headers, payload)
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise GeminiError("network", f"Could not reach the Groq API: {exc}")
                await self._sleep(min(2 ** attempt * 3, 30))
                continue

            if status == 200:
                try:
                    text = body["choices"][0]["message"]["content"]
                    obj = parse_json_loose(text)
                    if not isinstance(obj, dict):
                        raise ValueError("not an object")
                    return obj
                except (KeyError, IndexError, TypeError, ValueError):
                    parse_failures += 1
                    if parse_failures > 1:
                        raise GeminiError("bad_response", "Groq returned unusable output twice in a row.")
                    continue

            msg = self._err(body)
            low = msg.lower()
            if status in (401, 403):
                raise GeminiError("auth", msg or "The Groq API key was rejected.")
            if status == 404 or (status == 400 and ("model" in low and ("not found" in low or "decommission" in low or "does not exist" in low))):
                raise GeminiError("model_unavailable", f"Groq model {model} is not available.")
            if status == 429:
                hdr = {k.lower(): v for k, v in (hdrs or {}).items()}
                try:
                    delay = float(hdr.get("retry-after", ""))
                except ValueError:
                    delay = None
                if "per day" in low or "tokens per day" in low or "requests per day" in low or (delay is not None and delay > 600):
                    raise GeminiError("daily_limit", "Groq's daily free limit has been reached (it resets within 24 hours).")
                if attempt >= self.max_retries:
                    raise GeminiError("rate_limit", "Groq is still rate-limiting after several waits.")
                self.min_interval = min(self.min_interval * 1.5, 30.0)
                await self._sleep(min((delay if delay is not None else 2 ** attempt * 10) + 1, 120))
                continue
            if status >= 500:
                if attempt >= self.max_retries:
                    raise GeminiError("server", f"Groq API server error ({status}).")
                await self._sleep(min(2 ** attempt * 3, 30))
                continue
            raise GeminiError("bad_request", msg or f"Groq API returned HTTP {status}.")
        raise GeminiError("rate_limit", "Groq: gave up after repeated retries.")


class FallbackClient:
    """Tries providers in order; moves to the next on a failure that another
    provider could plausibly avoid. Same interface as the single clients."""

    SWITCH_KINDS = ("server", "network", "rate_limit", "daily_limit", "model_unavailable", "auth", "bad_response")
    SKIP_FOR_RUN = ("daily_limit", "auth", "model_unavailable")

    def __init__(self, providers: list):
        """providers: list of (label, client)."""
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.dead: dict = {}  # label -> reason it is skipped
        self.last_used = ""

    @property
    def labels(self) -> list:
        return [l for l, _ in self.providers]

    async def generate_json(self, system: str, prompt: str, temperature: float = 0.7):
        last_err = None
        tried = 0
        for label, client in self.providers:
            if label in self.dead:
                continue
            tried += 1
            try:
                obj, model = await client.generate_json(system, prompt, temperature)
                self.last_used = label
                return obj, f"{label}:{model}"
            except GeminiError as e:
                last_err = GeminiError(e.kind, f"{label}: {e.message}")
                if e.kind in self.SKIP_FOR_RUN:
                    self.dead[label] = e.kind
                if e.kind not in self.SWITCH_KINDS:
                    raise last_err
        if last_err is None:
            reasons = ", ".join(f"{l} ({k})" for l, k in self.dead.items())
            raise GeminiError("daily_limit" if "daily_limit" in self.dead.values() else "auth",
                              f"No AI provider is usable right now: {reasons}.")
        if len(self.providers) > 1 and tried > 1:
            last_err = GeminiError(last_err.kind, f"All providers failed. Last: {last_err.message}")
        raise last_err


def configured_providers(env=None) -> list:
    """Labels of providers that have an API key, honouring LLM_PROVIDER."""
    env = os.environ if env is None else env
    choice = (env.get("LLM_PROVIDER") or "auto").strip().lower()
    has = {"gemini": bool((env.get("GEMINI_API_KEY") or "").strip()),
           "groq": bool((env.get("GROQ_API_KEY") or "").strip())}
    if choice in ("gemini", "groq"):
        return [choice] if has[choice] else []
    return [p for p in ("gemini", "groq") if has[p]]


def build_client(env=None, transports=None):
    """Builds the client for the current .env. Returns None when no key is set.
    transports: optional {'gemini': fn, 'groq': fn} for tests."""
    env = os.environ if env is None else env
    transports = transports or {}
    labels = configured_providers(env)
    if not labels:
        return None
    multi = len(labels) > 1
    providers = []
    for label in labels:
        if label == "gemini":
            interval = float(env.get("GEMINI_MIN_INTERVAL_SECONDS") or 6)
            c = GeminiClient(env["GEMINI_API_KEY"].strip(), model=env.get("GEMINI_MODEL") or None,
                             min_interval=interval, max_retries=2 if multi else 4,
                             transport=transports.get("gemini"))
        else:
            interval = float(env.get("GROQ_MIN_INTERVAL_SECONDS") or 8)
            c = GroqClient(env["GROQ_API_KEY"].strip(), model=env.get("GROQ_MODEL") or None,
                           min_interval=interval, max_retries=2 if multi else 3,
                           transport=transports.get("groq"))
        providers.append((label, c))
    return FallbackClient(providers)
