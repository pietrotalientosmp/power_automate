"""
Flask endpoint — estrae, normalizza, filtra e fa scraping del testo completo
di ogni articolo a partire dal suo URL.

Dipendenze:
  pip install flask gunicorn requests beautifulsoup4 lxml

Avvio locale:
  python app.py

Endpoint: POST http://localhost:5000/extract-articles
"""
from flask import Flask, request, jsonify
import re
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────
# COSTANTI
# ──────────────────────────────────────────────

WEEK_DAYS        = 7       # finestra temporale
SCRAPE_TIMEOUT   = 10      # secondi per ogni richiesta HTTP
MAX_WORKERS      = 6       # thread paralleli per lo scraping
MAX_TEXT_WORDS   = 1200    # tronca il testo estratto oltre questa soglia

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Selettori CSS per trovare il contenuto principale (in ordine di priorità)
CONTENT_SELECTORS = [
    "article",
    '[role="main"]',
    "main",
    ".post-content",
    ".article-body",
    ".entry-content",
    ".content-body",
    "#content",
    ".story-body",
]

# Tag da rimuovere prima dell'estrazione (rumore)
NOISE_TAGS = [
    "script", "style", "noscript", "nav", "header", "footer",
    "aside", "form", "button", "iframe", "figure", "figcaption",
    "svg", "img", "picture", "video", "audio",
]


# ──────────────────────────────────────────────
# PULIZIA TESTO
# ──────────────────────────────────────────────

def _clean_text(text: str) -> str:
    text = re.sub(r"<img[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    entities = {
        "&#8230;": "…", "&amp;": "&", "&nbsp;": " ", "&#160;": " ",
        "&#8217;": "'", "&quot;": '"', "&lt;": "<", "&gt;": ">",
        "&#8216;": "'", "&#8220;": "\u201c", "&#8221;": "\u201d",
    }
    for ent, char in entities.items():
        text = text.replace(ent, char)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text != "…" else ""


# ──────────────────────────────────────────────
# SCRAPING TESTO COMPLETO ARTICOLO
# ──────────────────────────────────────────────

def scrape_article_text(url: str) -> dict:
    """
    Visita l'URL, estrae il testo pulito dell'articolo e restituisce:
      {
        "fullText":     "testo completo pulito",
        "wordCount":    N,
        "scrapeStatus": "ok" | "error" | "empty",
        "scrapeError":  "msg"   # solo in caso di errore
      }
    """
    if not url or not url.startswith("http"):
        return {"fullText": "", "wordCount": 0, "scrapeStatus": "error", "scrapeError": "URL non valido"}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=SCRAPE_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        logging.warning(f"Scrape failed [{url}]: {e}")
        return {"fullText": "", "wordCount": 0, "scrapeStatus": "error", "scrapeError": str(e)}

    soup = BeautifulSoup(resp.text, "lxml")

    # Rimuovi tag rumorosi
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()

    # Cerca il contenitore principale dell'articolo
    content_node = None
    for selector in CONTENT_SELECTORS:
        content_node = soup.select_one(selector)
        if content_node:
            break

    # Fallback al body intero
    if not content_node:
        content_node = soup.find("body") or soup

    # Estrai paragrafi significativi
    paragraphs = content_node.find_all(["p", "h1", "h2", "h3", "h4", "li"])
    lines = []
    for p in paragraphs:
        line = p.get_text(separator=" ", strip=True)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) > 40:   # scarta righe troppo corte (menu, label, ecc.)
            lines.append(line)

    full_text = "\n\n".join(lines)

    # Tronca se troppo lungo
    words = full_text.split()
    if len(words) > MAX_TEXT_WORDS:
        full_text = " ".join(words[:MAX_TEXT_WORDS]) + " […]"
        words = words[:MAX_TEXT_WORDS]

    if not full_text.strip():
        return {"fullText": "", "wordCount": 0, "scrapeStatus": "empty"}

    return {
        "fullText":     full_text,
        "wordCount":    len(words),
        "scrapeStatus": "ok",
    }


# ──────────────────────────────────────────────
# PARSING DATE
# ──────────────────────────────────────────────

def _parse_date(date_str: str) -> str:
    if not date_str or date_str.startswith("0001"):
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str[:10] if len(date_str) >= 10 else date_str


def _parse_date_obj(date_str: str):
    if not date_str or date_str.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _is_within_last_week(date_str: str) -> bool:
    dt = _parse_date_obj(date_str)
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)
    return dt >= cutoff


# ──────────────────────────────────────────────
# NORMALIZZAZIONE SINGOLO ARTICOLO
# ──────────────────────────────────────────────

def _normalize_single(item: dict) -> dict:
    title   = _clean_text(str(item.get("title", "")))
    summary = _clean_text(str(item.get("summary", "")))
    url     = str(
        item.get("primaryLink")
        or (item.get("links", [None])[0] if isinstance(item.get("links"), list) else None)
        or item.get("id", "")
    ).strip()
    publish_date = _parse_date(str(item.get("publishDate", "")))

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
    }


# ──────────────────────────────────────────────
# FORMATO FLAT POWER AUTOMATE
# ──────────────────────────────────────────────

def _is_flat_format(items: list) -> bool:
    return items and isinstance(items[0], dict) and any("." in k for k in items[0])


def _parse_flat_format(flat_list: list) -> list:
    articles_map: dict = {}
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
    wc = article.get("fullTextWordCount", 0)
    score += min(wc / 50, 10)   # fino a 10 punti per testo ricco

    if article["publishDate"]:
        try:
            pub = datetime.strptime(article["publishDate"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - pub).days
            score += max(0, 10 - days_old * 0.5)
        except Exception:
            pass

    if "sponsored" in [c.lower() for c in article.get("categories", [])]:
        score -= 5

    if article.get("scrapeStatus") != "ok":
        score -= 3

    return round(score, 2)


# ──────────────────────────────────────────────
# ENDPOINT PRINCIPALE
# ──────────────────────────────────────────────

@app.route("/extract-articles", methods=["POST"])
def extract_articles():
    """
    Input (accetta sia foreachItems sia items):
    { "foreachItems": [ { "title": "…", "primaryLink": "…", "publishDate": "…", … } ] }

    Output:
    {
      "articles": [
        {
          "title":              "…",
          "url":                "https://…",
          "summary":            "…breve dal feed RSS…",
          "fullText":           "…testo completo estratto dalla pagina…",
          "fullTextWordCount":  N,
          "scrapeStatus":       "ok" | "error" | "empty",
          "publishDate":        "YYYY-MM-DD",
          "categories":         […],
          "score":              9.5
        }
      ],
      "count":       N,
      "filtered":    M,
      "processedAt": "2026-03-12T10:00:00Z"
    }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    raw_items = body.get("foreachItems") or body.get("items", [])
    if not raw_items:
        return jsonify({"error": "Campo 'items' / 'foreachItems' mancante o vuoto"}), 400

    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    # 1. Normalizza metadati dal feed
    all_articles = normalize_articles(raw_items)
    total_before = len(all_articles)

    # 2. Filtro ultima settimana
    articles = [a for a in all_articles if _is_within_last_week(a["publishDate"])]
    filtered_out = total_before - len(articles)
    logging.info(f"Articoli dopo filtro data: {len(articles)} / {total_before}")

    # 3. Scraping parallelo del testo completo per ogni articolo
    def _scrape_and_merge(article: dict) -> dict:
        result = scrape_article_text(article["url"])
        article["fullText"]          = result.get("fullText", "")
        article["fullTextWordCount"] = result.get("wordCount", 0)
        article["scrapeStatus"]      = result.get("scrapeStatus", "error")
        if "scrapeError" in result:
            article["scrapeError"]   = result["scrapeError"]
        return article

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_scrape_and_merge, a): a for a in articles}
        scraped = []
        for future in as_completed(futures):
            try:
                scraped.append(future.result())
            except Exception as e:
                art = futures[future]
                art.update({"fullText": "", "fullTextWordCount": 0,
                             "scrapeStatus": "error", "scrapeError": str(e)})
                scraped.append(art)

    # 4. Scoring e ordinamento
    for a in scraped:
        a["score"] = score_article(a)
    scraped.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({
        "articles":    scraped,
        "count":       len(scraped),
        "filtered":    filtered_out,
        "processedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ──────────────────────────────────────────────
# ENDPOINT DEBUG — nessun filtro data
# ──────────────────────────────────────────────

@app.route("/extract-articles-all", methods=["POST"])
def extract_articles_all():
    """Identico a /extract-articles ma senza filtro temporale. Per test."""
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    raw_items = body.get("foreachItems") or body.get("items", [])
    if not raw_items:
        return jsonify({"error": "Campo 'items' / 'foreachItems' mancante o vuoto"}), 400

    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    articles = normalize_articles(raw_items)

    def _scrape_and_merge(article):
        result = scrape_article_text(article["url"])
        article["fullText"]          = result.get("fullText", "")
        article["fullTextWordCount"] = result.get("wordCount", 0)
        article["scrapeStatus"]      = result.get("scrapeStatus", "error")
        return article

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_scrape_and_merge, a): a for a in articles}
        scraped = []
        for future in as_completed(futures):
            try:
                scraped.append(future.result())
            except Exception as e:
                art = futures[future]
                art.update({"fullText": "", "fullTextWordCount": 0, "scrapeStatus": "error"})
                scraped.append(art)

    for a in scraped:
        a["score"] = score_article(a)
    scraped.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({
        "articles":    scraped,
        "count":       len(scraped),
        "processedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)