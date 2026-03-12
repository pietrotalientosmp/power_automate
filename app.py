"""
Flask endpoint — estrae, normalizza e filtra articoli RSS.
Supporta sia il formato flat Power Automate (chiavi "0.title", "1.summary" …)
sia il formato diretto (oggetti con campi title, summary, primaryLink, …).

Avvio locale:
  pip install flask
  python app.py

Endpoint: POST http://localhost:5000/extract-articles
"""
from flask import Flask, request, jsonify
import re
import logging
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


# ──────────────────────────────────────────────
# COSTANTI
# ──────────────────────────────────────────────

WEEK_DAYS = 7          # finestra temporale per il filtro
MIN_SCORE = 0.0        # abbassa a 0 per non perdere articoli validi


# ──────────────────────────────────────────────
# PULIZIA TESTO
# ──────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Rimuove tag HTML, entities e spazi multipli."""
    # Rimuove <img …> e tutti gli altri tag
    text = re.sub(r"<img[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # HTML entities comuni
    entities = {
        "&#8230;": "…", "&amp;": "&", "&nbsp;": " ", "&#160;": " ",
        "&#8217;": "'", "&quot;": '"', "&lt;": "<", "&gt;": ">",
        "&#8216;": "'", "&#8220;": "\u201c", "&#8221;": "\u201d",
    }
    for ent, char in entities.items():
        text = text.replace(ent, char)
    # Spazi multipli / newline
    text = re.sub(r"\s+", " ", text).strip()
    # Rimuove il trailing "…" residuo se è tutto il summary
    if text == "…":
        text = ""
    return text


# ──────────────────────────────────────────────
# PARSING DATE
# ──────────────────────────────────────────────

def _parse_date(date_str: str) -> str:
    """Restituisce YYYY-MM-DD oppure stringa vuota."""
    if not date_str or date_str.startswith("0001"):
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str[:10] if len(date_str) >= 10 else date_str


def _parse_date_obj(date_str: str) -> datetime | None:
    """Restituisce oggetto datetime (UTC) o None."""
    if not date_str or date_str.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _is_within_last_week(date_str: str) -> bool:
    """True se la data cade negli ultimi WEEK_DAYS giorni."""
    dt = _parse_date_obj(date_str)
    if dt is None:
        return False  # data assente → escludi
    cutoff = datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)
    return dt >= cutoff


# ──────────────────────────────────────────────
# NORMALIZZAZIONE ARTICOLO SINGOLO
# ──────────────────────────────────────────────

def _normalize_single(item: dict) -> dict:
    title   = _clean_text(str(item.get("title", "")))
    summary = _clean_text(str(item.get("summary", "")))

    # URL: primaryLink > links[0] > id
    url = str(
        item.get("primaryLink")
        or (item.get("links", [None])[0] if isinstance(item.get("links"), list) else None)
        or item.get("id", "")
    ).strip()

    publish_date = _parse_date(str(item.get("publishDate", "")))

    # Categorie: lista oppure stringa
    categories = item.get("categories", [])
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]
    elif isinstance(categories, list):
        categories = [str(c).strip() for c in categories
                      if c and str(c).lower() not in ("app", "summary")]
    else:
        categories = []

    return {
        "title":       title,
        "url":         url,
        "summary":     summary,
        "publishDate": publish_date,
        "categories":  categories,
        "wordCount":   len(summary.split()) if summary else 0,
    }


# ──────────────────────────────────────────────
# FORMATO FLAT (Power Automate foreachItems)
# ──────────────────────────────────────────────

def _is_flat_format(items: list) -> bool:
    """Verifica se il primo elemento usa chiavi tipo "0.title"."""
    return (
        items
        and isinstance(items[0], dict)
        and any("." in k for k in items[0])
    )


def _parse_flat_format(flat_list: list) -> list:
    articles_map: dict[int, dict] = {}

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


def normalize_articles(raw_items: list) -> list:
    if _is_flat_format(raw_items):
        return _parse_flat_format(raw_items)
    return [_normalize_single(item) for item in raw_items]


# ──────────────────────────────────────────────
# SCORING
# ──────────────────────────────────────────────

def score_article(article: dict) -> float:
    score = 0.0

    # Più parole nel summary → articolo più ricco
    score += min(article["wordCount"] / 10, 10)

    # Recency: -0.5 punti per ogni giorno di vecchiaia (max bonus 10)
    if article["publishDate"]:
        try:
            pub = datetime.strptime(article["publishDate"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - pub).days
            score += max(0, 10 - days_old * 0.5)
        except Exception:
            pass

    # Penalità articoli sponsorizzati
    if "sponsored" in [c.lower() for c in article.get("categories", [])]:
        score -= 5

    return round(score, 2)


# ──────────────────────────────────────────────
# ENDPOINT PRINCIPALE
# ──────────────────────────────────────────────

@app.route("/extract-articles", methods=["POST"])
def extract_articles():
    """
    Accetta due formati di body JSON:

    1) Formato diretto (Google Blog / RSS generico):
    {
      "foreachItems": [ { "title": "…", "summary": "…", "primaryLink": "…", … } ]
    }

    2) Formato flat Power Automate:
    {
      "items": [ { "0.title": "…", "0.summary": "…", … } ]
    }

    Risposta:
    {
      "articles": [ { title, url, summary, publishDate, categories, wordCount, score } ],
      "count": N,
      "filtered": M,          ← articoli scartati perché fuori dalla settimana
      "processedAt": "…"
    }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    # Accetta sia "items" sia "foreachItems" come chiave radice
    raw_items = body.get("foreachItems") or body.get("items", [])
    if not raw_items:
        return jsonify({"error": "Campo 'items' / 'foreachItems' mancante o vuoto"}), 400

    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    # Normalizza
    all_articles = normalize_articles(raw_items)
    total_before = len(all_articles)

    # ── Filtro ultima settimana ──
    articles = [a for a in all_articles if _is_within_last_week(a["publishDate"])]
    filtered_out = total_before - len(articles)

    # Scoring e ordinamento
    for a in articles:
        a["score"] = score_article(a)
    articles.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({
        "articles":    articles,
        "count":       len(articles),
        "filtered":    filtered_out,
        "processedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ──────────────────────────────────────────────
# ENDPOINT DI DEBUG — nessun filtro temporale
# ──────────────────────────────────────────────

@app.route("/extract-articles-all", methods=["POST"])
def extract_articles_all():
    """
    Identico a /extract-articles ma SENZA filtro data.
    Utile in fase di test per verificare la normalizzazione completa.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    raw_items = body.get("foreachItems") or body.get("items", [])
    if not raw_items:
        return jsonify({"error": "Campo 'items' / 'foreachItems' mancante o vuoto"}), 400

    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    articles = normalize_articles(raw_items)
    for a in articles:
        a["score"] = score_article(a)
    articles.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({
        "articles":    articles,
        "count":       len(articles),
        "processedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)