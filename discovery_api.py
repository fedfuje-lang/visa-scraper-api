"""
URL Discovery API for Visa Scraper
FastAPI Service deployed on Hetzner
Calls Python discovery logic and returns results to n8n

v2.0.0 - Optimized Crawling Engine
v2.0.1 - Crawling-Engine Feinschliff
v2.1.0 - Multilingual Fix
v2.2.0 - Dynamic Keywords
v2.3.0 - Smarter JS Detection
v2.3.1 - BUEROKRATIE Fix: Umlaut aus target_group entfernt (Encoding-Kompatibilität)
         GROUP F: BÜROKRATIE → GROUP F: BUEROKRATIE in allen Fallback-Maps
v2.4.0 - Status Fix: Neue URLs bekommen status='discovered' statt 'pending'
         damit WF1b sauber filtern kann und kein Endlosloop entsteht
v2.5.0 - PDF Discovery Fix: /pdf/, /download/, .doc, .xls aus BLOCKED_PATH_PATTERNS entfernt
         damit Gebührentabellen und offizielle Dokumente gecrawlt werden
v2.6.0 - Chunked Protection Fix: URLs mit status='chunked' werden beim upsert nicht
         überschrieben — nur neue URLs werden eingefügt, chunked URLs bleiben unangetastet
v2.7.0 - Parallelisierung: Rules werden parallel verarbeitet (max MAX_PARALLEL_RULES gleichzeitig)
         CONCURRENT_LIMIT pro Rule reduziert damit der Server nicht crasht
v2.7.1 - Domain Block Detection: Fehlerzähler pro Domain — nach DOMAIN_FAIL_THRESHOLD
         gescheiterten URLs wird die Domain für den Rest des Crawls übersprungen.
         Zähler wird nur beim letzten fehlgeschlagenen Versuch erhöht (nicht pro Retry).
v2.8.0 - Scoring komplett entfernt: score_url(), extract_topics(), GROUP_KEYWORDS,
         GENERAL_KEYWORDS, load_keywords(), config_keywords-Abfrage — alles raus.
         Embedding in WF6 übernimmt die Relevanzbeurteilung, kein Vorfilter mehr nötig.
         Alle gecrawlten URLs landen in discovered_urls, nur BLOCKED_PATH_PATTERNS filtert.
         Target URL garantiert gecrawlt: is_main_url=True, überspringt Domain-Block-Check.
         is_main_url wird jetzt korrekt in Supabase gespeichert.
v2.9.0 - Same-Day Protection: Rules die heute bereits gecrawlt wurden (last_crawled_at
         >= Tagesbeginn UTC) werden übersprungen. Kein doppeltes Crawlen am selben Tag.
         Erneutes Crawlen am nächsten Tag oder später ist weiterhin möglich.
         Nie gecrawlte Rules (last_crawled_at IS NULL) werden immer verarbeitet.
v3.0.0 - Job-System: /discover startet Job im Hintergrund und gibt sofort job_id zurück.
         Kein Timeout mehr in n8n möglich — Job läuft unabhängig von der HTTP-Verbindung.
         Neuer Endpoint GET /discover/status/{job_id} gibt aktuellen Fortschritt zurück.
         n8n pollt /discover/status/{job_id} bis status='completed'.
         Job-Speicher im RAM (dict) — wird bei Server-Neustart geleert, jobs laufen weiter.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
from collections import deque
import httpx
import tldextract
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from supabase import create_client, Client
import os
from typing import Optional, List, Dict
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import uuid

from fetch_markdown import router as fetch_markdown_router
from fetch_apis import router as fetch_apis_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Visa Scraper Discovery API",
    description="URL Discovery Service for Visa Immigration Data Scraping",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fetch_markdown_router)
app.include_router(fetch_apis_router)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set as environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# v3.0.0: JOB STORE — speichert laufende und abgeschlossene Jobs im RAM
# =============================================================================

# Format: { "job_id": { "status": "running"|"completed"|"failed",
#                       "total_rules": 100, "processed_rules": 42,
#                       "skipped_rules": 10, "total_urls_found": 1500,
#                       "successful_rules": 40, "failed_rules": 2,
#                       "started_at": "...", "finished_at": None,
#                       "current_country": "Portugal",
#                       "results_per_rule": [...] } }
JOB_STORE: Dict[str, Dict] = {}

# =============================================================================
# GROUP B wird von fetch_apis gehandelt
# =============================================================================

EXCLUDED_FROM_DISCOVERY = ["GROUP B: FINANZEN"]

# =============================================================================
# CRAWLING CONFIG
# =============================================================================

CONCURRENT_LIMIT = 5
MAX_PARALLEL_RULES = 3
MAX_RETRIES = 2
RETRY_DELAYS = [1, 3]
DOMAIN_FAIL_THRESHOLD = 3

IGNORED_QUERY_PARAMS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "session", "sid",
    "page", "p", "lang", "language", "locale",
]

BLOCKED_PATH_PATTERNS = [
    "/privacy", "/datenschutz", "/cookie", "/impressum", "/imprint",
    "/login", "/signin", "/signup", "/register", "/account",
    "/cart", "/checkout", "/shop", "/store", "/buy",
    "/careers", "/jobs", "/job-", "/stellenangebot",
    "/blog/", "/media/gallery", "/media/video",
    "/news/archive", "/news/2", "/press/archive", "/press/2",
    "/search", "/suche",
    "/sitemap", "/feed", "/rss",
    "/admin", "/wp-admin", "/wp-login",
    "/tag/", "/category/", "/author/",
    ".jpg", ".png", ".gif", ".zip",
    "/print/",
    "/social", "/share", "/tweet", "/facebook",
    "/calendar", "/events/2", "/archive",
    "/comment", "/reply",
    "#",
]

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in params.items() if k.lower() not in IGNORED_QUERY_PARAMS}
        clean_query = urlencode(filtered, doseq=True) if filtered else ""
    else:
        clean_query = ""
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    normalized = parsed._replace(fragment="", query=clean_query, path=path).geturl()
    return normalized


def is_internal(url: str, base_domain: str) -> bool:
    try:
        ext = tldextract.extract(url)
        current_domain = f"{ext.domain}.{ext.suffix}"
        return current_domain == base_domain
    except:
        return False


def is_blocked_path(url: str) -> bool:
    path_lower = urlparse(url).path.lower()
    return any(blocked in path_lower for blocked in BLOCKED_PATH_PATTERNS)


def get_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}"


def extract_main_content(soup: BeautifulSoup) -> str:
    content = soup.select_one(
        "main, article, [role='main'], "
        "#content, #main-content, #main, "
        ".content, .main-content, .page-content, .entry-content, "
        ".article-body, .post-content"
    )
    if content:
        for tag in content.select("nav, footer, header, aside, .sidebar, .menu, .nav, .breadcrumb"):
            tag.decompose()
        text = content.get_text(" ", strip=True)
        if len(text) > 100:
            return text
    for tag in soup.select("nav, footer, header, aside, .sidebar, .menu, .nav, .breadcrumb, .cookie, script, style"):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def get_today_start_utc() -> str:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start.isoformat()


# =============================================================================
# SITEMAP PARSER
# =============================================================================

async def fetch_sitemap_urls(base_url: str) -> List[str]:
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    urls = []
    try:
        client = await get_http_client()
        r = await client.get(sitemap_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/3.0)"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        sitemaps = root.findall(f".//{ns}sitemap/{ns}loc")
        if sitemaps:
            for sitemap_loc in sitemaps[:5]:
                sub_url = sitemap_loc.text.strip()
                try:
                    sub_r = await client.get(sub_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/3.0)"})
                    if sub_r.status_code == 200:
                        sub_root = ET.fromstring(sub_r.text)
                        sub_ns = ""
                        if sub_root.tag.startswith("{"):
                            sub_ns = sub_root.tag.split("}")[0] + "}"
                        for loc in sub_root.findall(f".//{sub_ns}loc"):
                            if loc.text:
                                urls.append(loc.text.strip())
                except Exception:
                    continue
        else:
            for loc in root.findall(f".//{ns}loc"):
                if loc.text:
                    urls.append(loc.text.strip())
        logger.info(f"📋 Sitemap: {len(urls)} URLs gefunden")
    except Exception as e:
        logger.info(f"📋 Sitemap nicht verfügbar: {str(e)}")
    return urls


# =============================================================================
# GLOBALER HTTPX CLIENT
# =============================================================================

_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=CONCURRENT_LIMIT * MAX_PARALLEL_RULES + 5,
                max_keepalive_connections=CONCURRENT_LIMIT * MAX_PARALLEL_RULES,
            ),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            }
        )
    return _http_client


async def fetch_html_fast(url: str, domain_fails: Dict[str, int], is_target: bool = False) -> Optional[str]:
    domain = get_domain(url)
    if not is_target and domain_fails.get(domain, 0) >= DOMAIN_FAIL_THRESHOLD:
        logger.info(f"⛔ Domain geblockt, skip: {domain} ({url})")
        return None
    client = await get_http_client()
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            elif r.status_code in (403, 429):
                if not is_target:
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                    logger.warning(f"⚠️ Domain Fehler {domain_fails[domain]}/{DOMAIN_FAIL_THRESHOLD}: {domain} (Status {r.status_code})")
                    if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                        logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                else:
                    logger.warning(f"⚠️ Target URL geblockt (Status {r.status_code}), kein Domain-Block: {url}")
                return None
            elif r.status_code in (503, 502):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    continue
                else:
                    if not is_target:
                        domain_fails[domain] = domain_fails.get(domain, 0) + 1
                        if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                            logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                    return None
            else:
                logger.warning(f"⚠️ httpx Status {r.status_code} für {url}")
                return None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            else:
                if not is_target:
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                    if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                        logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ httpx Fehler für {url}: {str(e)}")
            return None
    return None


async def fetch_html_playwright(url: str, browser) -> Optional[str]:
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        html = await page.content()
        await page.close()
        return html
    except Exception as e:
        logger.warning(f"⚠️ Playwright Fehler für {url}: {str(e)}")
        return None


def needs_javascript(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = extract_main_content(soup)
    script_count = len(soup.find_all("script"))
    if len(text) < 200 and script_count > 5:
        return True
    html_lower = html.lower()
    spa_indicators = ["__next", "__nuxt", "react-root", "ng-app", "v-app", 'id="app"']
    if len(text) < 200 and any(ind in html_lower for ind in spa_indicators):
        return True
    return False


# =============================================================================
# MAIN DISCOVERY FUNCTION
# =============================================================================

async def discover_urls(rule: Dict) -> List[Dict]:
    start_url    = rule['target_url']
    max_pages    = rule['max_urls']
    max_depth    = rule['max_depth']
    target_group = rule['target_group']

    ext = tldextract.extract(start_url)
    base_domain = f"{ext.domain}.{ext.suffix}"

    visited = set()
    discovered_urls = []
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    playwright_instance = None
    playwright_browser = None
    domain_fails: Dict[str, int] = {}
    normalized_start = normalize_url(start_url)

    logger.info(f"🚀 Starting discovery for: {rule['country_name']} ({rule['rule_id']})")
    logger.info(f"📊 Max URLs: {max_pages}, Max Depth: {max_depth}")

    sitemap_urls = await fetch_sitemap_urls(start_url)
    if sitemap_urls:
        sitemap_filtered = [u for u in sitemap_urls if is_internal(u, base_domain) and not is_blocked_path(u)]
        logger.info(f"📋 Sitemap: {len(sitemap_filtered)} relevante URLs (von {len(sitemap_urls)} total)")
    else:
        sitemap_filtered = []

    to_visit = deque()
    to_visit_set = set()

    for sm_url in sitemap_filtered[:max_pages]:
        normalized_sm = normalize_url(sm_url)
        if normalized_sm not in to_visit_set:
            to_visit.append((normalized_sm, 0, False))
            to_visit_set.add(normalized_sm)

    if normalized_start not in to_visit_set:
        to_visit.appendleft((normalized_start, 0, True))
        to_visit_set.add(normalized_start)

    async def process_page(url: str, depth: int, is_target: bool) -> tuple:
        nonlocal playwright_instance, playwright_browser
        async with semaphore:
            url_lower = url.lower()
            is_pdf = url_lower.endswith(".pdf") or ".pdf?" in url_lower
            if is_pdf:
                return url, depth, is_target, {
                    "url": url,
                    "page_title": url.split("/")[-1][:500],
                    "is_main_url": is_target,
                    "discovered_depth": depth,
                    "rule_id": rule['rule_id'],
                    "country_code": rule['country_iso'],
                    "country_name": rule['country_name'],
                    "target_group": rule['target_group']
                }, []

            html = await fetch_html_fast(url, domain_fails, is_target=is_target)
            needs_pw = html and needs_javascript(html)

            if html and not needs_pw:
                _soup_check = BeautifulSoup(html, "html.parser")
                internal_links = [
                    a.get("href", "") for a in _soup_check.select("a[href]")
                    if is_internal(urljoin(url, a.get("href", "")), base_domain)
                ]
                if len(internal_links) == 0:
                    needs_pw = True
                    logger.info(f"🔄 0 interne Links, nutze Playwright: {url}")

            if needs_pw:
                if not playwright_browser:
                    playwright_instance = await async_playwright().start()
                    playwright_browser = await playwright_instance.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                html = await fetch_html_playwright(url, playwright_browser)

            if not html:
                if is_target:
                    logger.warning(f"⚠️ Target URL konnte nicht geladen werden, trotzdem gespeichert: {url}")
                    return url, depth, is_target, {
                        "url": url,
                        "page_title": "",
                        "is_main_url": True,
                        "discovered_depth": depth,
                        "rule_id": rule['rule_id'],
                        "country_code": rule['country_iso'],
                        "country_name": rule['country_name'],
                        "target_group": rule['target_group']
                    }, []
                return url, depth, is_target, None, []

            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True) if title_tag else ""

            child_links = []
            if depth < max_depth:
                for a_tag in soup.select("a[href]"):
                    href = a_tag.get("href", "").strip()
                    if not href:
                        continue
                    full_url = urljoin(url, href)
                    if not full_url.startswith("http"):
                        continue
                    normalized_full = normalize_url(full_url)
                    if is_internal(normalized_full, base_domain) and not is_blocked_path(normalized_full):
                        child_links.append(normalized_full)

            result = {
                "url": url,
                "page_title": page_title[:500],
                "is_main_url": is_target,
                "discovered_depth": depth,
                "rule_id": rule['rule_id'],
                "country_code": rule['country_iso'],
                "country_name": rule['country_name'],
                "target_group": rule['target_group']
            }
            return url, depth, is_target, result, child_links

    while to_visit and len(visited) < max_pages:
        batch = []
        while to_visit and len(batch) < CONCURRENT_LIMIT:
            url, depth, is_target = to_visit.popleft()
            to_visit_set.discard(url)
            normalized = normalize_url(url)
            if normalized in visited or depth > max_depth:
                continue
            visited.add(normalized)
            batch.append((normalized, depth, is_target))

        if not batch:
            break

        logger.info(f"🔎 [{rule['rule_id']}] Batch: {len(batch)} Seiten (gesamt: {len(visited)}/{max_pages})")
        tasks = [process_page(url, depth, is_target) for url, depth, is_target in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"⚠️ Batch-Fehler: {str(result)}")
                continue
            url, depth, is_target, url_data, child_links = result
            if url_data:
                discovered_urls.append(url_data)
            for link in child_links:
                if link not in visited and link not in to_visit_set and len(visited) + len(to_visit) < max_pages * 2:
                    to_visit.append((link, depth + 1, False))
                    to_visit_set.add(link)

    if playwright_browser:
        await playwright_browser.close()
    if playwright_instance:
        await playwright_instance.stop()

    logger.info(f"✅ [{rule['rule_id']}] Discovery complete: {len(discovered_urls)} URLs (visited {len(visited)} pages)")
    return discovered_urls


# =============================================================================
# SUPABASE FUNCTIONS
# =============================================================================

def save_urls_to_supabase(discovered_urls: List[Dict]) -> int:
    if not discovered_urls:
        return 0

    logger.info(f"💾 Preparing to save {len(discovered_urls)} URLs to Supabase...")
    seen_urls = set()
    insert_data = []
    duplicates_removed = 0

    for url_data in discovered_urls:
        url = url_data["url"]
        if url in seen_urls:
            duplicates_removed += 1
            continue
        seen_urls.add(url)
        insert_data.append({
            "url":              url,
            "page_title":       url_data["page_title"],
            "is_main_url":      url_data["is_main_url"],
            "discovered_depth": url_data["discovered_depth"],
            "rule_id":          url_data["rule_id"],
            "country_code":     url_data["country_code"],
            "country_name":     url_data["country_name"],
            "target_group":     url_data["target_group"],
            "status":           "discovered"
        })

    if duplicates_removed > 0:
        logger.info(f"🧹 Removed {duplicates_removed} duplicate URLs from batch")

    try:
        all_urls = [d["url"] for d in insert_data]
        existing = supabase.table("discovered_urls").select("url, status").in_("url", all_urls).execute()
        chunked_urls = {row["url"] for row in existing.data if row["status"] == "chunked"}
        if chunked_urls:
            logger.info(f"🔒 Skipping {len(chunked_urls)} already chunked URLs")
            insert_data = [d for d in insert_data if d["url"] not in chunked_urls]
    except Exception as e:
        logger.warning(f"⚠️ Could not check existing statuses: {str(e)}")

    if not insert_data:
        logger.info("✅ No new URLs to save (all already chunked)")
        return 0

    try:
        response = supabase.table("discovered_urls").upsert(insert_data, on_conflict="url").execute()
        inserted_count = len(response.data) if response.data else 0
        logger.info(f"✅ {inserted_count} URLs saved successfully")
        return inserted_count
    except Exception as e:
        logger.error(f"❌ Error saving to Supabase: {str(e)}")
        return 0


def update_last_crawled(rule_id: str):
    try:
        supabase.table("config_rules").update({"last_crawled_at": "now()"}).eq("rule_id", rule_id).execute()
        logger.info(f"✅ Updated last_crawled_at for {rule_id}")
    except Exception as e:
        logger.warning(f"⚠️ Could not update last_crawled_at: {str(e)}")


# =============================================================================
# v3.0.0: HINTERGRUND-JOB FUNKTION
# Wird von asyncio.create_task() gestartet — läuft unabhängig von n8n-Verbindung
# =============================================================================

async def run_discovery_job(job_id: str, rules: List[Dict], max_urls_override: Optional[int]):
    """
    Verarbeitet alle Rules im Hintergrund.
    Aktualisiert JOB_STORE laufend damit /discover/status/{job_id} immer
    den aktuellen Stand zurückgeben kann.
    """
    job = JOB_STORE[job_id]
    job["status"] = "running"
    job["total_rules"] = len(rules)

    rule_semaphore = asyncio.Semaphore(MAX_PARALLEL_RULES)

    async def process_rule(rule: Dict, index: int) -> Dict:
        async with rule_semaphore:
            # Aktuelles Land im Job-Status anzeigen
            job["current_country"] = f"{rule['country_name']} ({rule['rule_id']})"
            logger.info(f"▶️  [{job_id}] Rule {index}/{len(rules)}: {rule['rule_id']} – {rule['country_name']} / {rule['target_group']}")

            try:
                if max_urls_override:
                    rule['max_urls'] = max_urls_override

                discovered = await discover_urls(rule)
                saved_count = save_urls_to_supabase(discovered)
                update_last_crawled(rule['rule_id'])

                job["processed_rules"] += 1
                job["successful_rules"] += 1
                job["total_urls_found"] += saved_count

                logger.info(f"✅ [{job_id}] Rule {rule['rule_id']} fertig: {saved_count} URLs — {job['processed_rules']}/{job['total_rules']} Rules done")

                return {
                    "rule_id":      rule['rule_id'],
                    "country":      rule['country_name'],
                    "target_group": rule['target_group'],
                    "urls_found":   saved_count,
                    "success":      True
                }

            except Exception as e:
                job["processed_rules"] += 1
                job["failed_rules"] += 1
                logger.error(f"❌ [{job_id}] Fehler bei Rule {rule['rule_id']}: {str(e)}")
                return {
                    "rule_id":      rule['rule_id'],
                    "country":      rule['country_name'],
                    "target_group": rule.get('target_group', 'unknown'),
                    "urls_found":   0,
                    "success":      False,
                    "error":        str(e)
                }

    try:
        tasks = [process_rule(rule, i) for i, rule in enumerate(rules, 1)]
        results = await asyncio.gather(*tasks)
        job["results_per_rule"] = list(results)
        job["status"] = "completed"
        job["current_country"] = None
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"🎉 [{job_id}] Job completed: {job['total_urls_found']} URLs, {job['successful_rules']} rules OK, {job['failed_rules']} failed")

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.error(f"❌ [{job_id}] Job failed: {str(e)}")


# =============================================================================
# API ENDPOINTS
# =============================================================================

class DiscoveryRequest(BaseModel):
    trigger: str = "manual"
    rule_ids: Optional[List[str]] = None
    filter: Optional[Dict[str, str]] = None
    max_urls: Optional[int] = None

class DirectDiscoveryRequest(BaseModel):
    start_urls: List[str]
    country_code: str
    country_name: str
    target_group: str
    rule_id: str
    max_depth: int = 3
    max_urls: int = 100

class DiscoveryJobResponse(BaseModel):
    job_id: str
    status: str
    total_rules: int
    skipped_rules: int
    message: str

class DiscoveryStatusResponse(BaseModel):
    job_id: str
    status: str                  # "running" | "completed" | "failed"
    total_rules: int
    processed_rules: int
    skipped_rules: int
    successful_rules: int
    failed_rules: int
    total_urls_found: int
    current_country: Optional[str]
    started_at: str
    finished_at: Optional[str]
    progress_pct: float          # 0.0 – 100.0

class DirectDiscoveryResponse(BaseModel):
    success: bool
    total_urls_found: int
    urls: List[Dict]


@app.get("/")
async def root():
    return {
        "service": "Visa Scraper Discovery API",
        "version": "3.0.0",
        "status": "running",
        "changes_v3.0.0": [
            "Job-System: /discover gibt sofort job_id zurück, Job läuft im Hintergrund",
            "Kein Timeout mehr — n8n Verbindung wird sofort geschlossen",
            "GET /discover/status/{job_id} zeigt Echtzeit-Fortschritt",
            "n8n pollt /discover/status/{job_id} bis status='completed'",
        ]
    }


@app.get("/health")
async def health():
    running_jobs = sum(1 for j in JOB_STORE.values() if j["status"] == "running")
    return {
        "status": "healthy",
        "version": "3.0.0",
        "supabase_connected": bool(SUPABASE_URL and SUPABASE_KEY),
        "active_jobs": running_jobs,
    }


@app.post("/discover", response_model=DiscoveryJobResponse)
async def run_discovery(request: DiscoveryRequest):
    """
    v3.0.0: Startet Discovery-Job im Hintergrund und gibt sofort job_id zurück.
    n8n wartet NICHT mehr auf die Fertigstellung — kein Timeout möglich.
    Fortschritt via GET /discover/status/{job_id} abrufbar.
    """
    logger.info("=" * 80)
    logger.info(f"🚀 DISCOVERY API v3.0.0 — JOB MODE")
    logger.info("=" * 80)

    try:
        query = supabase.table("config_rules").select("*").eq("active", True)

        if request.rule_ids:
            query = query.in_("rule_id", request.rule_ids)
        if request.filter:
            if "country_iso" in request.filter:
                query = query.eq("country_iso", request.filter["country_iso"])
            if "target_group" in request.filter:
                query = query.eq("target_group", request.filter["target_group"])

        response = query.execute()
        all_rules = response.data

        if not all_rules:
            job_id = str(uuid.uuid4())
            JOB_STORE[job_id] = {"status": "completed", "total_rules": 0, "processed_rules": 0,
                                  "skipped_rules": 0, "successful_rules": 0, "failed_rules": 0,
                                  "total_urls_found": 0, "current_country": None,
                                  "started_at": datetime.now(timezone.utc).isoformat(),
                                  "finished_at": datetime.now(timezone.utc).isoformat(),
                                  "results_per_rule": []}
            return DiscoveryJobResponse(job_id=job_id, status="completed", total_rules=0,
                                        skipped_rules=0, message="Keine aktiven Rules gefunden")

        # Same-Day Protection
        today_start = get_today_start_utc()
        skipped_today = []
        rules_to_process = []

        for rule in all_rules:
            last_crawled = rule.get("last_crawled_at")
            if last_crawled and last_crawled >= today_start:
                skipped_today.append(rule)
            else:
                rules_to_process.append(rule)

        if skipped_today:
            logger.info(f"⏭️ Heute bereits gecrawlt, übersprungen: {len(skipped_today)} Rules")

        # GROUP B herausfiltern
        rules = [r for r in rules_to_process if not any(excluded in r.get("target_group", "") for excluded in EXCLUDED_FROM_DISCOVERY)]
        skipped_group_b = len(rules_to_process) - len(rules)
        total_skipped = len(skipped_today) + skipped_group_b

        if not rules:
            job_id = str(uuid.uuid4())
            JOB_STORE[job_id] = {"status": "completed", "total_rules": 0, "processed_rules": 0,
                                  "skipped_rules": total_skipped, "successful_rules": 0, "failed_rules": 0,
                                  "total_urls_found": 0, "current_country": None,
                                  "started_at": datetime.now(timezone.utc).isoformat(),
                                  "finished_at": datetime.now(timezone.utc).isoformat(),
                                  "results_per_rule": []}
            return DiscoveryJobResponse(job_id=job_id, status="completed", total_rules=0,
                                        skipped_rules=total_skipped,
                                        message="Alle Rules heute bereits gecrawlt oder GROUP B")

        # Job anlegen
        job_id = str(uuid.uuid4())
        JOB_STORE[job_id] = {
            "status":           "queued",
            "total_rules":      len(rules),
            "processed_rules":  0,
            "skipped_rules":    total_skipped,
            "successful_rules": 0,
            "failed_rules":     0,
            "total_urls_found": 0,
            "current_country":  None,
            "started_at":       datetime.now(timezone.utc).isoformat(),
            "finished_at":      None,
            "results_per_rule": [],
        }

        # Job im Hintergrund starten — gibt sofort zurück
        asyncio.create_task(run_discovery_job(job_id, rules, request.max_urls))

        logger.info(f"✅ Job {job_id} gestartet: {len(rules)} Rules, {total_skipped} übersprungen")

        return DiscoveryJobResponse(
            job_id=job_id,
            status="running",
            total_rules=len(rules),
            skipped_rules=total_skipped,
            message=f"Job gestartet — {len(rules)} Rules werden verarbeitet, {total_skipped} übersprungen"
        )

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/discover/status/{job_id}", response_model=DiscoveryStatusResponse)
async def get_discovery_status(job_id: str):
    """
    v3.0.0: Gibt aktuellen Stand eines laufenden oder abgeschlossenen Jobs zurück.
    n8n pollt diesen Endpoint bis status='completed'.
    """
    if job_id not in JOB_STORE:
        raise HTTPException(status_code=404, detail=f"Job {job_id} nicht gefunden")

    job = JOB_STORE[job_id]
    total = job["total_rules"]
    processed = job["processed_rules"]
    progress = round((processed / total * 100), 1) if total > 0 else 100.0

    return DiscoveryStatusResponse(
        job_id=job_id,
        status=job["status"],
        total_rules=total,
        processed_rules=processed,
        skipped_rules=job["skipped_rules"],
        successful_rules=job["successful_rules"],
        failed_rules=job["failed_rules"],
        total_urls_found=job["total_urls_found"],
        current_country=job.get("current_country"),
        started_at=job["started_at"],
        finished_at=job.get("finished_at"),
        progress_pct=progress,
    )


@app.post("/discover-direct", response_model=DirectDiscoveryResponse)
async def discover_direct(request: DirectDiscoveryRequest):
    if any(excluded in request.target_group for excluded in EXCLUDED_FROM_DISCOVERY):
        return DirectDiscoveryResponse(success=False, total_urls_found=0, urls=[])

    logger.info("=" * 80)
    logger.info(f"🚀 DIRECT DISCOVERY: {request.country_name} ({request.country_code}) / {request.target_group}")
    logger.info("=" * 80)

    discovered_urls = []
    try:
        for i, start_url in enumerate(request.start_urls, 1):
            rule = {
                'target_url':   start_url,
                'max_urls':     request.max_urls,
                'max_depth':    request.max_depth,
                'rule_id':      request.rule_id,
                'country_iso':  request.country_code,
                'country_name': request.country_name,
                'target_group': request.target_group
            }
            urls = await discover_urls(rule)
            discovered_urls.extend(urls)
            logger.info(f"✅ Found {len(urls)} URLs from start URL {i}")

        saved_count = save_urls_to_supabase(discovered_urls)
        return DirectDiscoveryResponse(success=True, total_urls_found=saved_count, urls=discovered_urls)

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting Visa Scraper Discovery API v3.0.0...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"⚡ Concurrent limit per rule: {CONCURRENT_LIMIT}")
    logger.info(f"⚡ Max parallel rules: {MAX_PARALLEL_RULES}")
    logger.info(f"⚡ Domain fail threshold (Sub-URLs): {DOMAIN_FAIL_THRESHOLD}")
    logger.info(f"⚡ Same-Day Protection: aktiv")
    logger.info(f"⚡ Job-System: aktiv — /discover gibt sofort job_id zurück")
    logger.info("✅ API is ready!")


@app.on_event("shutdown")
async def shutdown_event():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("🔒 httpx Client geschlossen")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
