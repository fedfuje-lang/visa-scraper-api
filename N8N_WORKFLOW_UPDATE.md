# 🔄 N8N WORKFLOW UPDATE
## Von Python Code Node zu HTTP Request

---

## ⚠️ WICHTIG: Workflow A muss geändert werden!

**ALT (funktioniert nicht):**
```
[Manual Trigger] → [Python Code Node] → [Success Log]
```

**NEU (funktioniert mit Railway):**
```
[Manual Trigger] → [HTTP Request] → [Success Log]
```

---

## 🛠️ SCHRITT-FÜR-SCHRITT ANLEITUNG

### Schritt 1: Python Code Node löschen

1. Öffne deinen **"Workflow A - URL Discovery"** in n8n
2. Klick auf **"URL Discovery"** Node (die Python Code Node)
3. Drücke **Delete** oder klick auf Mülleimer-Icon
4. Node ist gelöscht ✅

---

### Schritt 2: HTTP Request Node hinzufügen

1. Klick auf **"+"** zwischen Manual Trigger und Success Log
2. Suche: `HTTP Request`
3. Wähle: **"HTTP Request"**

---

### Schritt 3: HTTP Request Node konfigurieren

**Node Name:**
```
Railway Discovery API
```

#### Parameters Tab:

**Method:**
```
POST
```

**URL:**
```
https://deine-railway-url.up.railway.app/discover
```

⚠️ **WICHTIG:** Ersetze `deine-railway-url` mit deiner echten Railway URL!

**Authentication:**
```
None
```

**Send Query Parameters:**
```
OFF
```

**Send Headers:**
```
ON
```

**Header 1:**
- Name: `Content-Type`
- Value: `application/json`

**Send Body:**
```
ON
```

**Body Content Type:**
```
JSON
```

**Body (JSON):**
```json
{
  "trigger": "manual"
}
```

#### Options Tab:

**Timeout:**
```
300000
```
(= 5 Minuten - für längere Discovery Runs)

**Response:**
```
Include Response Headers and Status: OFF
```

**Retry on Fail:**
```
ON
```

**Max Tries:**
```
3
```

**Wait Between Tries (ms):**
```
5000
```

---

### Schritt 4: Success Log Node anpassen (OPTIONAL)

Falls du die Success Log Node hast, passe sie an:

**JavaScript Code:**
```javascript
// Hole Response von Railway API
const result = $input.first().json;

console.log("="*80);
console.log("📊 DISCOVERY API RESULTS");
console.log("="*80);
console.log(`✅ Success: ${result.success}`);
console.log(`📋 Total Rules: ${result.total_rules_processed}`);
console.log(`🔗 Total URLs: ${result.total_urls_found}`);
console.log(`✅ Successful: ${result.successful_rules}`);
console.log(`❌ Failed: ${result.failed_rules}`);
console.log("="*80);

if (result.results_per_rule) {
  console.log("\n📍 Details per Rule:");
  for (const rule of result.results_per_rule) {
    const status = rule.success ? '✅' : '❌';
    console.log(`  ${status} ${rule.rule_id} (${rule.country}): ${rule.urls_found} URLs`);
  }
}

return {
  json: {
    status: "completed",
    timestamp: new Date().toISOString(),
    ...result
  }
};
```

---

### Schritt 5: Workflow speichern & testen

1. **Save** klicken (oben rechts)
2. **Test workflow** klicken
3. Beobachte die Ausgabe:
   - Manual Trigger: ✅
   - HTTP Request: Loading... (kann 1-5 Minuten dauern!)
   - Success Log: ✅

---

## 🎯 ERWARTETES ERGEBNIS

### HTTP Request Response:

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

## 🔧 ERWEITERTE OPTIONEN

### Nur bestimmte Rules crawlen:

Ändere den Body der HTTP Request:

```json
{
  "trigger": "manual",
  "rule_ids": ["US-VISA", "DE-VISA"]
}
```

### Scheduled Trigger (später):

Ersetze Manual Trigger mit Schedule Trigger:

```
Schedule: 0 2 * * *  (Täglich um 2 Uhr nachts)
    ↓
HTTP Request → Railway API
    ↓
Success Log (oder Email Notification)
```

---

## 🐛 TROUBLESHOOTING

### Problem: "Connection timeout"

**Ursache:** Discovery dauert länger als Timeout

**Lösung:** Timeout in HTTP Request Node erhöhen:
- Options Tab
- Timeout: `600000` (10 Minuten)

### Problem: "502 Bad Gateway"

**Ursache:** Railway Service ist "asleep" oder crashed

**Lösung:**
1. Prüfe Railway Dashboard → Logs
2. Service manuell neu starten
3. Warte 30 Sekunden, dann erneut versuchen

### Problem: "401 Unauthorized"

**Ursache:** Falls du später Authentication hinzufügst

**Lösung:** API Key in Headers setzen

### Problem: Keine URLs gefunden

**Check:**
1. Gehe zu Railway Logs
2. Siehst du Discovery-Logs?
3. Sind Rules in Supabase aktiv?

---

## 📊 MONITORING

### In n8n:

1. Execution History prüfen
2. Execution Time: ~2-10 Minuten normal
3. Output JSON prüfen: `total_urls_found` > 0?

### In Railway:

1. Logs Tab öffnen
2. Live-Logs während Execution
3. Siehst du Crawling-Logs?

### In Supabase:

Nach Execution:
```sql
-- Prüfe neue URLs
SELECT COUNT(*) FROM discovered_urls 
WHERE created_at > NOW() - INTERVAL '10 minutes';

-- Top URLs nach Score
SELECT url, relevance_score, topics 
FROM discovered_urls 
ORDER BY created_at DESC 
LIMIT 10;
```

---

## 🎉 FERTIG!

Dein neuer Workflow:
- ✅ Nutzt Railway API statt Python in n8n
- ✅ Keine Security-Probleme mehr
- ✅ Skaliert besser
- ✅ Einfacher zu debuggen
- ✅ Kann von überall aufgerufen werden (n8n, Postman, curl, etc.)

---

## 🚀 NEXT LEVEL

### Weitere Nodes hinzufügen:

**Email bei Erfolg:**
```
HTTP Request → IF Node (success == true)
    ↓ True
    Email Node: "Discovery erfolgreich! 45 URLs gefunden"
    ↓ False
    Email Node: "Discovery fehlgeschlagen!"
```

**Slack Notification:**
```
HTTP Request → Slack Node
Message: "🎉 Discovery fertig: {{$json.total_urls_found}} URLs gefunden!"
```

**Supabase Direct Check:**
```
HTTP Request → Supabase Node
Action: Select from discovered_urls
Filter: created_at > NOW() - 10 minutes
→ Zeigt frisch gefundene URLs
```

---

**VIEL ERFOLG! 🚀**
