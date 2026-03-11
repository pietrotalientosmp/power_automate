# Newsletter Article Extractor — Guida Completa

Setup **senza funzionalità Premium** di Power Automate.
Stack: Flask su Railway (free) + Custom Connector.

---

## PARTE 1 — Deploy su Railway (gratis)

### 1.1 Prepara il repository

Carica questi file su un repo GitHub:
```
flask_app.py
requirements.txt
Procfile
```

### 1.2 Deploy su Railway

1. Vai su **https://railway.app** e accedi con GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Seleziona il tuo repo → Railway rileva Flask automaticamente
4. **Settings → Networking → Generate Domain**
5. Copia il dominio: es. `newsletter-extractor.railway.app`

> Piano free Railway: 500 ore/mese, più che sufficienti per un flusso settimanale.

### 1.3 Verifica

Apri nel browser: `https://newsletter-extractor.railway.app/health`
Risposta attesa: `{"status": "ok"}`

---

## PARTE 2 — Custom Connector in Power Automate

### 2.1 Modifica openapi.yaml

Cambia la riga host con il tuo dominio Railway:
```yaml
host: newsletter-extractor.railway.app
```

### 2.2 Crea il connector

1. **make.powerautomate.com** → menu sinistro → **Custom connectors**
2. **+ New custom connector** → **Import an OpenAPI file**
3. Nome: `Newsletter Extractor`, carica `openapi.yaml`
4. Tab **Security** → Authentication: `No authentication`
5. Click **✓ Create connector**

### 2.3 Test

Tab **Test** → **+ New connection** → testa `ExtractArticles` con:
```json
{
  "items": [{
    "0.title": "Test",
    "0.primaryLink": "https://example.com",
    "0.summary": "Test summary for newsletter.",
    "0.publishDate": "2026-03-10 13:47:26Z",
    "0.categories.0": "Artificial intelligence"
  }]
}
```
Risposta attesa: `200` con array `articles`.

---

## PARTE 3 — Nel flusso Power Automate

Posizione: dopo **Normalizzazione metadati**, prima di **Analisi LLM**.

1. **+ New step** → cerca `Newsletter Extractor`
2. Azione: **Estrai e normalizza articoli**
3. Campo `items`: usa il body del passo RSS precedente
4. Aggiungi **Parse JSON** con lo schema degli `articles`
5. Usa **Filter array** per tenere solo articoli con `score >= 7`
6. Passa `title`, `summary`, `url` all'azione LLM

---

## File

| File | Scopo |
|------|-------|
| `flask_app.py` | Endpoint Flask (logica principale) |
| `requirements.txt` | flask + gunicorn |
| `Procfile` | Avvio per Railway/Render |
| `openapi.yaml` | Spec per Custom Connector |
