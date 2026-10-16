import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_client import GeminiClient, GeminiError
from llm_client import GroqClient, FallbackClient, build_client, configured_providers
from offer_profile import load_offer_profile, render_offer_file
from pitch_runner import run

sys.path.insert(0, os.path.dirname(__file__))
from test_phase_b import GOOD, SENDER, OFFER, ROW, ok_body, Fake


def arun(c):
    return asyncio.run(c)


async def nosleep(_):
    return None


def groq_ok(obj):
    return 200, {"choices": [{"message": {"content": json.dumps(obj)}}]}, {}


def groq(posts, **kw):
    return GroqClient("k", transport=Fake(posts), sleep=nosleep, min_interval=0, **kw)


def gem(posts, **kw):
    return GeminiClient("k", model="gemini-3.8-flash", transport=Fake(posts), sleep=nosleep, min_interval=0, **kw)


def test_groq_success_and_request_shape():
    f = Fake([groq_ok(GOOD)])
    c = GroqClient("k", transport=f, sleep=nosleep, min_interval=0)
    obj, model = arun(c.generate_json("sys", "prompt"))
    assert obj["angle"] == GOOD["angle"] and model == "openai/gpt-oss-120b"


def test_groq_429_retry_after_then_ok():
    c = groq([(429, {"error": {"message": "Rate limit reached for tokens per minute"}}, {"retry-after": "2"}), groq_ok(GOOD)])
    assert arun(c.generate_json("s", "p"))[0]["angle"]


def test_groq_daily_limit():
    c = groq([(429, {"error": {"message": "Rate limit reached ... tokens per day (TPD)"}}, {"retry-after": "5000"})])
    try:
        arun(c.generate_json("s", "p")); assert False
    except GeminiError as e:
        assert e.kind == "daily_limit"


def test_groq_model_fallthrough_and_auth():
    c = groq([(404, {"error": {"message": "model not found"}}, {}), groq_ok(GOOD)])
    assert arun(c.generate_json("s", "p"))[1] == "llama-3.3-70b-versatile"
    try:
        arun(groq([(401, {"error": {"message": "Invalid API Key"}}, {})]).generate_json("s", "p")); assert False
    except GeminiError as e:
        assert e.kind == "auth"


def test_groq_503_gives_server_error_after_retries():
    c = groq([(503, None, {})] * 5, max_retries=2)
    try:
        arun(c.generate_json("s", "p")); assert False
    except GeminiError as e:
        assert e.kind == "server" and "503" in e.message


def test_fallback_gemini_503_switches_to_groq():
    g = gem([(503, None, {})] * 4, max_retries=1)
    q = groq([groq_ok(GOOD)])
    fb = FallbackClient([("gemini", g), ("groq", q)])
    obj, model = arun(fb.generate_json("s", "p"))
    assert obj["angle"] and model.startswith("groq:")
    assert "gemini" not in fb.dead  # a 503 is transient: Gemini is tried again next time


def test_fallback_daily_limit_skips_provider_for_the_run():
    g = gem([(429, {"error": {"message": "Quota exceeded per day", "details": []}}, {})])
    q = groq([groq_ok(GOOD), groq_ok(GOOD)])
    fb = FallbackClient([("gemini", g), ("groq", q)])
    arun(fb.generate_json("s", "p")); arun(fb.generate_json("s", "p"))
    assert fb.dead == {"gemini": "daily_limit"} and len(g.transport.calls) <= 2


def test_fallback_all_fail_reports_last_error():
    fb = FallbackClient([("gemini", gem([(429, {"error": {"message": "per day"}}, {})])),
                         ("groq", groq([(429, {"error": {"message": "tokens per day"}}, {"retry-after": "9999"})]))])
    try:
        arun(fb.generate_json("s", "p")); assert False
    except GeminiError as e:
        assert e.kind == "daily_limit" and "All providers failed" in e.message


def test_build_client_from_env():
    assert configured_providers({}) == [] and build_client({}) is None
    assert configured_providers({"GROQ_API_KEY": "x"}) == ["groq"]
    assert configured_providers({"GEMINI_API_KEY": "a", "GROQ_API_KEY": "b"}) == ["gemini", "groq"]
    assert configured_providers({"GEMINI_API_KEY": "a", "GROQ_API_KEY": "b", "LLM_PROVIDER": "groq"}) == ["groq"]
    assert configured_providers({"GEMINI_API_KEY": "a", "LLM_PROVIDER": "groq"}) == []
    c = build_client({"GEMINI_API_KEY": "a", "GROQ_API_KEY": "b", "GROQ_MODEL": "m1, m2"})
    assert c.labels == ["gemini", "groq"] and c.providers[1][1].models == ["m1", "m2"]


def test_runner_survives_gemini_503_with_groq_and_saves_each_pitch(tmp_path):
    prof = type("P", (), {"sender": SENDER, "offers": [OFFER], "ok": True})()
    rows = [dict(ROW, title=f"Hotel {i}") for i in range(3)]
    g = gem([(503, None, {})] * 20, max_retries=0)
    q = groq([groq_ok(GOOD)] * 3)
    fb = FallbackClient([("gemini", g), ("groq", q)])
    saved = []
    state = {}
    arun(run(rows, prof, fb, state, 0, lambda: saved.append(sum(1 for r in rows if r.get("pitch_status") == "written")), lambda m: None))
    assert state["phase"] == "done" and state["done"] == 3 and saved[-1] == 3 and max(saved) >= 1
    assert rows[0]["pitch_model"].startswith("groq:")


def test_runner_pauses_when_every_provider_is_out_and_keeps_written(tmp_path):
    prof = type("P", (), {"sender": SENDER, "offers": [OFFER], "ok": True})()
    rows = [dict(ROW, title=f"Hotel {i}") for i in range(3)]
    q = groq([groq_ok(GOOD), (429, {"error": {"message": "tokens per day"}}, {"retry-after": "9999"})])
    fb = FallbackClient([("groq", q)])
    state = {}
    arun(run(rows, prof, fb, state, 0, lambda: None, lambda m: None))
    assert state["phase"] == "paused" and state["done"] == 1
    assert sum(1 for r in rows if r.get("pitch_status") == "written") == 1


def test_render_offer_file_roundtrip(tmp_path):
    sender = {"YOUR_NAME": "Sam", "COMPANY_NAME": "Acme", "OPT_OUT_LINE": "Reply no\nthanks"}
    offer = {"OFFER_NAME": "Hotels", "WHAT_I_SELL": "Car hire", "MATCH_CATEGORIES": "hotel, hostel", "PROOF_POINTS": ""}
    p = tmp_path / "o.txt"
    p.write_text(render_offer_file(sender, [offer]), encoding="utf-8")
    prof = load_offer_profile(str(p))
    assert prof.ok and prof.sender["YOUR_NAME"] == "Sam" and prof.sender["OPT_OUT_LINE"] == "Reply no thanks"
    assert prof.offers[0]["MATCH_CATEGORIES"] == ["hotel", "hostel"] and "PROOF_POINTS" not in prof.offers[0]
