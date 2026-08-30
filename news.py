import requests
import yfinance as yf

import brain

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:14b"


def fetch_headlines(symbol, limit=4):
    try:
        items = yf.Ticker(brain.yf_symbol(symbol)).news or []
        titles = []
        for it in items[:limit]:
            t = (it.get("title") if isinstance(it, dict) else None) \
                or (it.get("content", {}) or {}).get("title")
            if t:
                titles.append(t)
        return titles
    except Exception:
        return []


def sentiment(symbol):
    titles = fetch_headlines(symbol)
    if not titles:
        return "NEUTRAL", "(no headlines found)"

    import socket
    try:
        s = socket.create_connection(("localhost", 11434), timeout=2)
        s.close()
    except (ConnectionRefusedError, OSError):
        return "NEUTRAL", "(AI sentiment offline)"

    joined = "\n".join(f"- {t}" for t in titles)
    prompt = (
        "You are a cautious trading risk analyst. Classify the overall market "
        "sentiment of these recent headlines about the asset.\n"
        f"{joined}\n\n"
        "Answer in EXACTLY this format and nothing else:\n"
        "VERDICT: BULLISH or BEARISH or NEUTRAL\n"
        "REASON: one short sentence.\n"
        "If headlines are mixed or unrelated to price direction, answer NEUTRAL."
    )
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1, "num_predict": 60},
        }, timeout=120)
        r.raise_for_status()
        text = r.json().get("response", "")
        verdict = "NEUTRAL"
        for word in ("BULLISH", "BEARISH", "NEUTRAL"):
            if word in text.upper():
                verdict = word
                break
        reason = ""
        if "REASON:" in text.upper():
            after = text.split(":", 1)[-1].strip() if text.count(":") >= 2 else ""
            reason = after[:120]
        return verdict, reason or joined[:100]
    except Exception as e:
        return "NEUTRAL", f"(news check unavailable: {str(e)[:60]})"


def record_ai_sentiments(verbose=True):
    """Run the local Ollama AI across every tracked market and persist the
    verdicts into the ai_news table (source='local'). The cloud cannot reach a
    localhost Ollama, so this is the night_runner's job. Returns count written.

    This never trades by itself - it only *informs* later decision runs.
    """
    try:
        import sqlite3
        conn = brain.init_db()
        ts = brain.datetime.now().isoformat(timespec="seconds")
        written = 0
        for symbol in brain.ALL:
            try:
                verdict, reason = sentiment(symbol)
            except Exception:
                verdict, reason = "NEUTRAL", "(error)"
            # skip "offline" placeholders so we don't store junk
            if "offline" in reason or "unavailable" in reason:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO ai_news(symbol, ts, verdict, reason, source) "
                "VALUES(?,?,?,?,?)",
                (symbol, ts, verdict, reason[:200], "local"))
            if verbose:
                print(f"  AI[{symbol}] {verdict} — {reason[:70]}")
            written += 1
        conn.commit()
        return written
    except Exception as e:
        print(f"  record_ai_sentiments error: {e}")
        return 0
