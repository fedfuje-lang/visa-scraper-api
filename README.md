# 🚀 Visa Scraper Discovery API

FastAPI Service für URL Discovery - deployed auf Railway.app

## 📋 Was macht dieser Service?


Dieser API-Service:
1. ✅ Liest aktive Rules aus Supabase `config_rules`
2. ✅ Crawlt Webseiten mit Playwright + BeautifulSoup
3. ✅ Bewertet URLs nach Relevanz (Keyword-Scoring)
4. ✅ Speichert gefundene URLs in Supabase `discovered_urls`
5. ✅ Gibt JSON-Ergebnis zurück an n8n

---

## 🛠️ DEPLOYMENT AUF RAILWAY

### Schritt 1: Repository zu Railway verbinden

1. Gehe zu https://railway.app
2. Login mit GitHub Account
3. Klick auf **"New Project"**
4. Wähle **"Deploy from GitHub repo"**
5. Wähle **`visa-scraper-api`** Repository
6. Railway startet automatisch das Deployment! 🎉

### Schritt 2: Environment Variables setzen

Nach dem Deployment:

1. Klick auf dein Projekt in Railway
2. Gehe zu **"Variables"** Tab
3. Füge diese 2 Variables hinzu:

```
SUPABASE_URL=https://pcpkvecexyqkheehsutp.supabase.co
SUPABASE_KEY=dein-service-role-key-hier
```

4. **Save** klicken
5. Railway deployed automatisch neu mit den neuen Variables

### Schritt 3: Deployment URL kopieren

1. In Railway klick auf **"Settings"**
2. Unter **"Domains"** siehst du deine URL:
   ```
   https://visa-scraper-api-production-xxxx.up.railway.app
   ```
3. **Diese URL kopieren!** - brauchst du für n8n

---

## 🔌 N8N INTEGRATION

### Workflow Structure:

```
[Manual Trigger]
    ↓
[HTTP Request] → POST zu Railway API
    ↓
[Success Log]
```

### HTTP Request Node Settings:

**Method:** `POST`

**URL:** 
```
https://visa-scraper-api-production-xxxx.up.railway.app/discover
```

**Headers:**
```json
{
  "Content-Type": "application/json"
}
```

**Body (JSON):**
```json
{
  "trigger": "manual"
}
```

**Optional - Nur bestimmte Rules:**
```json
{
  "trigger": "manual",
  "rule_ids": ["US-VISA", "DE-VISA"]
}
```

---

## 📡 API ENDPOINTS

### `GET /`
Health check & service info

**Response:**
```json
{
  "service": "Visa Scraper Discovery API",
  "version": "1.0.0",
  "status": "running"
}
```

### `GET /health`
Service health status

**Response:**
```json
{
  "status": "healthy",
  "supabase_connected": true
}
```

### `POST /discover`
Main discovery endpoint - startet URL Discovery

**Request Body:**
```json
{
  "trigger": "manual",
  "rule_ids": ["US-VISA"] // Optional
}
```

**Response:**
```json
{
  "success": true,
  "total_rules_processed": 2,
  "total_urls_found": 45,
  "successful_rules": 2,
  "failed_rules": 0,
  "results_per_rule": [
    {
      "rule_id": "US-VISA",
      "country": "United States",
      "urls_found": 25,
      "success": true
    },
    {
      "rule_id": "US-COSTS",
      "country": "United States",
      "urls_found": 20,
      "success": true
    }
  ]
}
```

---

## 🧪 TESTING

### Test 1: Service läuft?

```bash
curl https://deine-railway-url.up.railway.app/
```

Erwartete Antwort: Service Info JSON

### Test 2: Health Check

```bash
curl https://deine-railway-url.up.railway.app/health
```

Erwartete Antwort: `{"status": "healthy", "supabase_connected": true}`

### Test 3: Discovery starten

```bash
curl -X POST https://deine-railway-url.up.railway.app/discover \
  -H "Content-Type: application/json" \
  -d '{"trigger":"manual"}'
```

Erwartete Antwort: Discovery Results JSON

---

## 📊 MONITORING

### Railway Dashboard:

1. **Logs ansehen:**
   - Klick auf dein Projekt
   - Tab **"Deployments"**
   - Klick auf letztes Deployment
   - Siehst Live-Logs 🔴

2. **Resource Usage:**
   - Tab **"Metrics"**
   - CPU, RAM, Network Usage

3. **Deployment History:**
   - Alle Deployments
   - Rollback möglich

---

## 🐛 TROUBLESHOOTING

### Problem: Deployment schlägt fehl

**Lösung 1:** Check Build Logs in Railway
- Playwright Installation fehlgeschlagen?
- Dependencies nicht installiert?

**Lösung 2:** Railway Config prüfen
- `railway.json` korrekt?
- `Procfile` vorhanden?

### Problem: API gibt 500 Error

**Check:**
1. Environment Variables gesetzt? (`SUPABASE_URL`, `SUPABASE_KEY`)
2. Supabase Connection funktioniert?
3. Logs in Railway prüfen

### Problem: Playwright crasht

**Ursache:** Zu wenig RAM

**Lösung:** 
- Railway Hobby Plan nutzen (8 GB RAM)
- Nicht Free Plan (0.5 GB RAM)

### Problem: Discovery findet keine URLs

**Check:**
1. `config_rules` Tabelle in Supabase hat aktive Rules?
2. `target_url` erreichbar?
3. Keywords passen zur Webseite?

---

## 📁 PROJECT STRUCTURE

```
visa-scraper-api/
├── discovery_api.py      # FastAPI Service (Hauptdatei)
├── requirements.txt      # Python Dependencies
├── Procfile             # Railway Start Command
├── railway.json         # Railway Config
├── .dockerignore        # Docker Ignores
├── .env.example         # Environment Variables Template
└── README.md            # Diese Datei
```

---

## 🔄 UPDATES

### Code ändern:

1. Ändere `discovery_api.py` lokal
2. Commit & Push zu GitHub
3. Railway deployed automatisch neu! 🎉

### Dependencies hinzufügen:

1. Füge zu `requirements.txt` hinzu
2. Commit & Push
3. Railway installiert automatisch

---

## 💰 KOSTEN (Railway Hobby Plan)

```
$5/Monat Grundpreis
Inkl. $5 Credits

Typische Nutzung:
- 1 Discovery Run = ~5-10 Minuten
- 100 Runs/Monat = ~$1-2
- Du bleibst unter $5! ✅
```

---

## 🆘 SUPPORT

### Railway Discord:
https://discord.gg/railway

### Railway Docs:
https://docs.railway.app

### Supabase Docs:
https://supabase.com/docs

---

## 📝 LICENSE

MIT License - Free to use!

---

## 🎯 NEXT STEPS

Nach erfolgreichem Deployment:

1. ✅ URL in n8n HTTP Request Node einfügen
2. ✅ Workflow testen
3. ✅ Neue Länder in `config_rules` hinzufügen
4. ✅ Discovery für 60 Länder × 4 Gruppen = 240 Rules! 🚀

---

**🎉 VIEL ERFOLG MIT DEINEM VISA SCRAPER!** 🌍✈️
