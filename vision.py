import base64

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests


MODELS = ["llava:7b", "llama3.2-vision"]
OLLAMA_URL = "http://localhost:11434/api/generate"


def model_available():
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        return any(m in " ".join(models) for m in MODELS)
    except Exception:
        return False


def make_chart_png(df, symbol):
    tail = df.tail(120)
    close = tail["Close"]
    sma50 = df["Close"].rolling(50).mean().tail(120)
    sma200 = df["Close"].rolling(200).mean().tail(120) if len(df) >= 200 else None

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0f172a")
    for a in (ax, axv):
        a.set_facecolor("#0f172a")
        a.tick_params(colors="#94a3b8")
        for s in a.spines.values():
            s.set_color("#334155")

    ax.plot(close.index, close.values, color="#38bdf8", linewidth=1.8, label="close")
    ax.plot(sma50.index, sma50.values, color="#f59e0b", linewidth=1.2, label="SMA50")
    if sma200 is not None:
        ax.plot(sma200.index, sma200.values, color="#a78bfa", linewidth=1.2, label="SMA200")
    ax.legend(facecolor="#1e293b", labelcolor="#e2e8f0", edgecolor="#334155")
    ax.set_title(f"{symbol} - last {len(tail)} days", color="#e2e8f0")
    ax.grid(alpha=0.15)

    up = tail["Close"] >= tail["Open"]
    axv.bar(tail.index[up], tail["Volume"][up], color="#22c55e", width=1.0)
    axv.bar(tail.index[~up], tail["Volume"][~up], color="#ef4444", width=1.0)
    axv.grid(alpha=0.15)

    buf = pd.io.common.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def analyze_chart(symbol, df):
    png = make_chart_png(df, symbol)
    b64 = base64.b64encode(png).decode()
    prompt = (
        "You are a cautious quantitative trading assistant looking at a daily price "
        "chart with SMA50/SMA200 and volume for the asset below. "
        "In at most 80 words answer exactly in this format:\n"
        "TREND: (up/down/sideways + one clause)\n"
        "PATTERN: (one notable observation)\n"
        "RISK: (low/medium/high + why)\n"
        "Do not give price targets or advice beyond this."
    )
    last_err = None
    for model in MODELS:
        try:
            r = requests.post(OLLAMA_URL, json={
                "model": model, "prompt": prompt, "images": [b64], "stream": False,
                "options": {"temperature": 0.2, "num_predict": 150},
            }, timeout=240)
            r.raise_for_status()
            text = r.json().get("response", "").strip()
            if text:
                return f"[{model}] {text}"
        except Exception as e:
            last_err = e
    return f"(eyes unavailable: {str(last_err)[:80]})"
