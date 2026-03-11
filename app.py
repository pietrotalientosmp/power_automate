"""
Versione Flask dell'endpoint — usa questa se non vuoi Azure Functions.
Deploy su: Azure App Service, Railway, Render, o qualsiasi VPS.

Avvio locale:
  pip install flask
  python flask_app.py

Endpoint: POST http://localhost:5000/extract-articles
"""
from flask import Flask, request, jsonify
import json
import logging
import re
from datetime import datetime, timezone

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


# ──────────────────────────────────────────────
# LOGICA DI PARSING
# ──────────────────────────────────────────────

def normalize_articles(raw_items: list) -> list:
    """
    Accetta il formato flat di Power Automate (foreachItems):
    Lista di dict con chiavi tipo "0.title", "1.summary", ecc.
    """
    if raw_items and isinstance(raw_items[0], dict) and any("." in k for k in raw_items[0]):
        return _parse_flat_format(raw_items)
    return [_normalize_single(item) for item in raw_items]


def _parse_flat_format(flat_list: list) -> list:
    articles_map = {}

    for flat_dict in flat_list:
        for key, value in flat_dict.items():
            parts = key.split(".", 1)
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            field = parts[1]

            if idx not in articles_map:
                articles_map[idx] = {}

            if "." in field:
                sub_key = field.split(".", 1)[0]
                if sub_key not in articles_map[idx]:
                    articles_map[idx][sub_key] = []
                articles_map[idx][sub_key].append(value)
            else:
                articles_map[idx][field] = value

    return [_normalize_single(articles_map[i]) for i in sorted(articles_map.keys())]


def _normalize_single(item: dict) -> dict:
    title = str(item.get("title", "")).strip()
    url = str(
        item.get("primaryLink")
        or (item.get("links", [None])[0] if isinstance(item.get("links"), list) else None)
        or item.get("id", "")
    ).strip()
    summary = _clean_summary(str(item.get("summary", "")))
    publish_date = _parse_date(str(item.get("publishDate", "")))
    categories = item.get("categories", [])
    if isinstance(categories, list):
        categories = [c for c in categories if c and c.lower() not in ("app", "summary")]
    else:
        categories = []

    return {
        "title": title,
        "url": url,
        "summary": summary,
        "publishDate": publish_date,
        "categories": categories,
        "wordCount": len(summary.split()) if summary else 0,
    }


def _clean_summary(text: str) -> str:
    text = text.replace("&#8230;", "…").replace("&amp;", "&").replace("&nbsp;", " ")
    text = text.replace("&#160;", " ").replace("&#8217;", "'").replace("&quot;", '"')
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(date_str: str) -> str:
    if not date_str or date_str.startswith("0001"):
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str[:10] if len(date_str) >= 10 else date_str


def score_article(article: dict) -> float:
    score = 0.0
    score += min(article["wordCount"] / 10, 10)
    if article["publishDate"]:
        try:
            pub = datetime.strptime(article["publishDate"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - pub).days
            score += max(0, 10 - days_old * 0.5)
        except Exception:
            pass
    if "sponsored" in [c.lower() for c in article.get("categories", [])]:
        score -= 5
    return round(score, 2)


# ──────────────────────────────────────────────
# ENDPOINT
# ──────────────────────────────────────────────

@app.route("/extract-articles", methods=["POST"])
def extract_articles():
    """
    Input (body JSON da Power Automate):
    {
      "items": [
        {
          "0.title": "...",
          "0.primaryLink": "...",
          "0.summary": "...",
          "0.publishDate": "2026-03-10 13:47:26Z",
          "0.categories.0": "Artificial intelligence",
          "1.title": "...",
          ...
        }
      ]
    }

    Output:
    {
      "articles": [ { title, url, summary, publishDate, categories, wordCount, score } ],
      "count": N,
      "processedAt": "..."
    }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    raw_items = body.get("items", [])
    if not raw_items:
        return jsonify({"error": "Campo 'items' mancante o vuoto"}), 400

    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    articles = normalize_articles(raw_items)
    for a in articles:
        a["score"] = score_article(a)
    articles.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({
        "articles": articles,
        "count": len(articles),
        "processedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
