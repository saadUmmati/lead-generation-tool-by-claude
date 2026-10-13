"""
gemini_client.py — small, careful client for the Gemini API (free tier friendly).

Design points
  * One request per call, JSON out, strict pacing between requests.
  * No hard-coded free-tier numbers: Google changes them. We pace conservatively
    (GEMINI_MIN_INTERVAL_SECONDS, default 6s) and slow down further after a 429.
  * Understands the common error shapes: per-minute limit (wait and retry),
    per-day limit (stop cleanly so the job can be resumed tomorrow), model not
    available on the free tier (move on to the next model), bad key.
  * The model is auto-picked from the models your key can actually use, newest
    Flash first, unless GEMINI_MODEL is set.
  * The HTTP layer is injectable so everything here is testable without network.
"""

import asyncio
import json
import re
import time

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
_FLASH_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?$")


class GeminiError(Exception):
    """kind: auth | daily_limit | rate_limit | model_unavailable | blocked |
    bad_response | bad_request | server | network"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


async def httpx_transport(method: str, url: str, headers: dict, body):
    import httpx  # imported lazily so tests don't need it
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.request(method, url, headers=headers, json=body)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data, dict(resp.headers)


def rank_models(names: list) -> list:
    """Orders usable model names: newest plain Flash first, then newest Flash-Lite."""
    flash, lite = [], []
    for n in names:
        short = n.split("/")[-1]
        m = _FLASH_RE.match(short)
        if not m:
            continue
        ver = tuple(int(p) for p in m.group(1).split("."))
        (lite if m.group(2) else flash).append((ver, short))
    flash.sort(reverse=True)
    lite.sort(reverse=True)
    return [s for _, s in flash[:2]] + [s for _, s in lite[:2]]


def parse_json_loose(text: str):
    """Parses a JSON object from model text, tolerating code fences / chatter."""
    if text is None:
        raise ValueError("empty")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError("no JSON object found")


def parse_error(body) -> dict:
    """Extracts retry delay and quota hints from a Google API error body."""
    info = {"message": "", "retry_delay": None, "daily": False, "zero_limit": False}
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return info
    msg = str(err.get("message", ""))
    info["message"] = msg
    low = msg.lower()
    quota_text = ""
    for d in err.get("details", []) or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("@type", ""))
        if "RetryInfo" in t and d.get("retryDelay"):
            m = re.match(r"^\s*([\d.]+)\s*s", str(d["retryDelay"]))
            if m:
                info["retry_delay"] = float(m.group(1))
        if "QuotaFailure" in t:
            for v in d.get("violations", []) or []:
                quota_text += " " + json.dumps(v)
    qt = quota_text.lower()
    info["daily"] = ("perday" in qt.replace("_", "").replace("-", "")) or "per day" in low or "daily" in low
    info["zero_limit"] = "limit: 0" in low or "limit\": 0" in qt
    return info


class GeminiClient:
    def __init__(self, api_key: str, model: str | None = None, transport=None,
                 sleep=asyncio.sleep, min_interval: float = 6.0, max_retries: int = 4):
        self.api_key = api_key
        self.forced_model = model or None
        self.transport = transport or httpx_transport
        self._sleep = sleep
        self.min_interval = float(min_interval)
        self.max_retries = max_retries
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._candidates: list | None = None
        self._idx = 0

    # ------------------------------------------------------------ helpers --
    @property
    def headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    @property
    def model(self) -> str:
        if self._candidates:
            return self._candidates[min(self._idx, len(self._candidates) - 1)]
        return self.forced_model or ""

    async def _pace(self):
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await self._sleep(wait)
            self._last_call = time.monotonic()

    async def _discover_models(self) -> list:
        if self.forced_model:
            return [self.forced_model]
        try:
            status, body, _ = await self.transport("GET", f"{API_BASE}/models?pageSize=200", self.headers, None)
        except Exception:
            return list(FALLBACK_MODELS)
        if status in (400, 401, 403):
            raise GeminiError("auth", parse_error(body)["message"] or "The Gemini API key was rejected.")
        if status != 200 or not isinstance(body, dict):
            return list(FALLBACK_MODELS)
        usable = [m.get("name", "") for m in body.get("models", [])
                  if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        ranked = rank_models(usable)
        return ranked or list(FALLBACK_MODELS)

    # --------------------------------------------------------------- main --
    async def generate_json(self, system: str, prompt: str, temperature: float = 0.7):
        """Returns (parsed_dict, model_name). Raises GeminiError."""
        if self._candidates is None:
            self._candidates = await self._discover_models()
            self._idx = 0
        while self._idx < len(self._candidates):
            model = self._candidates[self._idx]
            try:
                return await self._call(model, system, prompt, temperature), model
            except GeminiError as e:
                if e.kind == "model_unavailable":
                    self._idx += 1
                    continue
                raise
        raise GeminiError("model_unavailable", "No Gemini model available to this API key on its current plan.")

    async def _call(self, model: str, system: str, prompt: str, temperature: float):
        url = f"{API_BASE}/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": temperature,
                "maxOutputTokens": 4096,
            },
        }
        parse_failures = 0
        for attempt in range(self.max_retries + 1):
            await self._pace()
            try:
                status, body, hdrs = await self.transport("POST", url, self.headers, payload)
            except Exception as exc:  # network trouble
                if attempt >= self.max_retries:
                    raise GeminiError("network", f"Could not reach the Gemini API: {exc}")
                await self._sleep(min(2 ** attempt * 3, 30))
                continue

            if status == 200:
                try:
                    return self._extract(body)
                except _Retry:
                    parse_failures += 1
                    if parse_failures > 1:
                        raise GeminiError("bad_response", "The model returned unusable output twice in a row.")
                    continue

            info = parse_error(body)
            if status in (401, 403) or (status == 400 and "api key" in info["message"].lower()):
                raise GeminiError("auth", info["message"] or "The Gemini API key was rejected.")
            if status == 404:
                raise GeminiError("model_unavailable", f"Model {model} is not available.")
            if status == 429:
                if info["zero_limit"]:
                    raise GeminiError("model_unavailable", f"Model {model} has no free-tier quota.")
                if info["daily"]:
                    raise GeminiError("daily_limit", "The daily free-tier limit has been reached (resets at midnight Pacific time).")
                if attempt >= self.max_retries:
                    raise GeminiError("rate_limit", "Still rate-limited after several waits; try again in a few minutes.")
                delay = info["retry_delay"] if info["retry_delay"] is not None else min(2 ** attempt * 15, 120)
                self.min_interval = min(self.min_interval * 1.5, 30.0)  # slow down for the rest of the run
                await self._sleep(min(delay + 1, 180))
                continue
            if status >= 500:
                if attempt >= self.max_retries:
                    raise GeminiError("server", f"Gemini API server error ({status}).")
                await self._sleep(min(2 ** attempt * 3, 45))
                continue
            raise GeminiError("bad_request", info["message"] or f"Gemini API returned HTTP {status}.")
        raise GeminiError("rate_limit", "Gave up after repeated retries.")

    def _extract(self, body):
        if not isinstance(body, dict):
            raise _Retry()
        cands = body.get("candidates") or []
        if not cands:
            reason = ((body.get("promptFeedback") or {}).get("blockReason")) or "no candidates"
            raise GeminiError("blocked", f"The model declined to answer ({reason}).")
        cand = cands[0]
        fin = str(cand.get("finishReason", ""))
        if fin in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise GeminiError("blocked", f"The model blocked this answer ({fin}).")
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        try:
            obj = parse_json_loose(text)
        except ValueError:
            raise _Retry()
        if not isinstance(obj, dict):
            raise _Retry()
        return obj


class _Retry(Exception):
    pass
