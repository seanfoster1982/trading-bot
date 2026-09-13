"""Local FinBERT sentiment — no trading authority. Lazy-imports transformers."""
from __future__ import annotations

import argparse
from typing import Any

MODEL_ID = "ProsusAI/finbert"


def analyze(texts: list[str]) -> list[dict[str, Any]]:
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline
    except ImportError as e:
        raise RuntimeError(
            "FinBERT deps missing. Install with: .venv\\Scripts\\python.exe -m pip install transformers torch"
        ) from e
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
    rev = getattr(getattr(model, "config", None), "_name_or_path", MODEL_ID)
    clf = pipeline(
        "text-classification",
        model=model,
        tokenizer=tokenizer,
        top_k=None,
        framework="pt",
        device=-1,
        trust_remote_code=False,
    )
    out = []
    for t in texts:
        scores = clf(t)[0]
        best = max(scores, key=lambda x: x["score"])
        label = str(best["label"]).lower()
        if "pos" in label:
            sent = "positive"
        elif "neg" in label:
            sent = "negative"
        else:
            sent = "neutral"
        out.append({"text": t, "sentiment": sent, "score": float(best["score"]), "model": rev})
    return out


def smoke_test() -> int:
    samples = [
        "The company reported record profits and raised guidance.",
        "Shares plunged after the firm missed earnings and cut dividends.",
        "The Federal Reserve left interest rates unchanged today.",
    ]
    try:
        rows = analyze(samples)
    except RuntimeError as e:
        print(f"FINBERT: INSTALL_NEEDED {e}")
        return 2
    except Exception as e:
        print(f"FINBERT: FAILED {type(e).__name__}: {e}")
        return 1
    for r in rows:
        print(f"  {r['sentiment']:8} {r['score']:.3f} :: {r['text'][:60]}")
    print("FINBERT: CONNECTED (local) — not a trading signal")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke-test", action="store_true")
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test())
    p.print_help()


if __name__ == "__main__":
    main()
