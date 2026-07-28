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
v2.4.0 - Status Fix: Neue URLs bekommen status='discovered' statt 'pending'
v2.5.0 - PDF Discovery Fix: /pdf/, /download/, .doc, .xls aus BLOCKED_PATH_PATTERNS entfernt
v2.6.0 - Chunked Protection Fix: URLs mit status='chunked' werden beim upsert nicht überschrieben
v2.7.0 - Parallelisierung: Rules werden parallel verarbeitet (max MAX_PARALLEL_RULES gleichzeitig)
v2.7.1 - Domain Block Detection: Fehlerzähler pro Domain
v2.8.0 - Scoring komplett entfernt: Embedding in WF6 übernimmt die Relevanzbeurteilung
v2.9.0 - Same-Day Protection: Rules die heute bereits gecrawlt wurden werden übersprungen
v3.0.0 - Job-System: /discover startet Job im Hintergrund und gibt sofort job_id zurück
v3.1.0 - NULL-first Sortierung
v3.3.0 - Pagination Fix: fetch_all_rules() umgeht 1000-Row-Limit
v3.4.0 - Trigger Fix + Timestamp Fix:
         1. target_url nicht mehr in discovered_urls schreiben — Postgres Trigger übernimmt das
         2. last_crawled_at nur bei Erfolg setzen (saved_count > 0)
v3.5.0 - Sieben Robustheits-Fixes (kein Schema-Eingriff):
         (3) IGNORED_QUERY_PARAMS: page/p/lang/language/locale ENTFERNT — Pagination und
             Sprachvarianten kollabierten vorher auf eine URL; ganze Sprachversionen gingen verloren.
         (9) normalize_url() lowercased jetzt Scheme + Host (RFC 3986) — Example.com/x und
             example.com/x sind nicht mehr zwei verschiedene URLs.
         (5) Per-Domain Rate-Limiting: zufällige Pause (RATE_LIMIT_MIN..MAX Sek.) zwischen
             Requests an dieselbe Domain. Reduziert 403/429-Blocks deutlich. 429 Retry-After
             Header wird ausgewertet (statt fixer Delays).
         (6) Content-Type- und Größen-Check in fetch_html_fast: nur text/html wird geparst,
             Antworten > MAX_CONTENT_BYTES werden verworfen. Kein Binär-/Riesencontent im RAM.
         (8) Playwright Race-Condition behoben: asyncio.Lock um Browser-Initialisierung,
             page.close() in finally (kein Page-Leak bei Exceptions).
         (7) Bereits gechunkte URLs werden VOR dem Crawl aus der Frontier gefiltert
             (chunked_skip-Set) — spart Bandbreite, Seiten werden nicht erneut geholt.
         (4) robots.txt wird gelesen — NUR um zusätzliche Sitemap:-Einträge zu finden
             (mehr echte URLs). Disallow-Regeln werden bewusst NICHT als Sperre genutzt;
             Filterung läuft weiter ausschließlich über BLOCKED_PATH_PATTERNS.
v3.6.0 - crawl_attempts Fix: Rules die dauerhaft 0 URLs liefern werden nach
         MAX_FAILED_ATTEMPTS=5 Versuchen deaktiviert (active=false).
         Erfolgreiche Rules setzen crawl_attempts zurück auf 0.
         Zwei neue Spalten in config_rules: crawl_attempts, last_crawl_failed_at.
         Behebt das Dauerschleifenproblem: tote Rules (Login-Wall, Site offline,
         dauerhaft 0 Sub-Links) bekamen nie einen Timestamp und wurden bei jedem
         Lauf erneut versucht.
v3.6.1 - Zwei Fixes (Speed + Vollständigkeit), kein Eingriff in Rate-Limits/
         Concurrency/Retry-Logik:
         1. Perf: BeautifulSoup-Parsing (needs_javascript, Link-/Title-
            Extraktion) läuft jetzt über asyncio.to_thread() statt direkt
            im Event-Loop. Reine Ausführungsverlagerung, keine Verhaltens-
            änderung — vorher blockierte ein einzelner Parse-Vorgang kurz
            alle anderen parallelen Tasks im selben Batch.
         2. Fix: PDFs, die per Content-Type (nicht per '.pdf' in der URL)
            erkannt werden — z.B. /download?id=123 — wurden bisher in
            fetch_html_fast() als "kein HTML" still verworfen und landeten
            nie in discovered_urls. fetch_html_fast() gibt jetzt (html,
            is_pdf) zurück; process_page() legt für erkannte PDFs denselben
            discovered_urls-Eintrag an wie beim '.pdf'-Suffix-Fall.
v3.7.0 - MAX_PARALLEL_RULES 2 → 4: Hetzner-Check (28.07.2026) zeigt reichlich
         Headroom (5,6GB frei von 7,6GB RAM, Load 0.3 auf 4 Kernen, Swap
         praktisch ungenutzt). Alter OOM-Kill im dmesg-Log (3,6GB RSS eines
         einzelnen Discovery-Prozesses) stammt vermutlich von vor dem
         Playwright-Leak-Fix in v3.5.0 (8) — nicht sicher verifizierbar,
         da ohne Zeitstempel im Log. Nach Deploy RAM/dmesg beobachten.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import random
from collections import deque
import httpx
import tldextract
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from supabase import create_client, Client
import os
from typing import Optional, List, Dict, Set
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
    version="3.7.0"
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

JOB_STORE: Dict[str, Dict] = {}

# =============================================================================
# GROUP B wird von fetch_apis gehandelt
# =============================================================================

EXCLUDED_FROM_DISCOVERY = ["GROUP B: FINANZEN"]

# =============================================================================
# v3.6.0: crawl_attempts — Schwellwert für Deaktivierung toter Rules
# =============================================================================

MAX_FAILED_ATTEMPTS = 5

# =============================================================================
# CRAWLING CONFIG
# =============================================================================

CONCURRENT_LIMIT = 5
# v3.7.0: 2 → 4 (war ursprünglich an CX33: 4 vCPUs/8GB RAM ausgerichtet, s. v2.7.0).
# Hetzner-Check 28.07.2026: 5,6GB frei, Load 0.3, Swap ungenutzt — Headroom vorhanden.
# Alter OOM-Kill im Log vermutlich vor dem Playwright-Leak-Fix (v3.5.0 (8)).
# Nach Deploy RAM/dmesg beobachten, bevor weiter hochgesetzt wird.
MAX_PARALLEL_RULES = 4
MAX_RETRIES = 2
RETRY_DELAYS = [1, 3]
DOMAIN_FAIL_THRESHOLD = 3

# v3.5.0 (5): Höflichkeits-Intervall pro Domain (Sekunden, zufällig).
RATE_LIMIT_MIN = 1.0
RATE_LIMIT_MAX = 3.0
# v3.5.0 (5): Obergrenze für Retry-After, damit ein bösartiger Header den Crawl nicht einfriert.
MAX_RETRY_AFTER = 30.0
# v3.5.0 (6): Antworten größer als das werden verworfen (kein Binär-/Riesencontent).
MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB

# v3.5.0 (3): page/p/lang/language/locale ENTFERNT.
# Diese Parameter unterscheiden echte Seiten (Pagination, Sprachversionen) —
# sie zu strippen kollabierte ganze Sprachversionen und Listenseiten auf eine URL.
IGNORED_QUERY_PARAMS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "session", "sid",
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
# v3.5.0 (5): PER-DOMAIN RATE LIMITER
# Stellt sicher, dass an dieselbe Domain nicht zwei Requests gleichzeitig oder
# zu schnell hintereinander gehen. Pause ist zufällig (menschlicheres Muster,
# geringere Blockrate). Jede Domain hat ihr eigenes Lock + letzten Zeitstempel.
# =============================================================================

class DomainRateLimiter:
    def __init__(self, min_delay: float, max_delay: float):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_request: Dict[str, float] = {}

    def _get_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def wait(self, domain: str):
        lock = self._get_lock(domain)
        async with lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            last = self._last_request.get(domain, 0.0)
            delay = random.uniform(self.min_delay, self.max_delay)
            elapsed = now - last
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_request[domain] = asyncio.get_event_loop().time()


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
    # v3.5.0 (9): Scheme + Host case-insensitiv (RFC 3986) — verhindert Duplikate
    # wie Example.com/x vs example.com/x. Pfad/Query bleiben case-sensitiv.
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    normalized = parsed._replace(
        scheme=scheme, netloc=netloc, fragment="", query=clean_query, path=path
    ).geturl()
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
# v3.3.0: PAGINATION FIX — holt ALLE Rules aus Supabase (nicht nur die ersten 1000)
# =============================================================================

def fetch_all_rules(base_query) -> List[Dict]:
    all_rules = []
    page_size = 1000
    offset = 0

    while True:
        response = (
            base_query
            .order("last_crawled_at", desc=False, nullsfirst=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = response.data
        if not batch:
            break
        all_rules.extend(batch)
        logger.info(f"📦 Pagination: {len(batch)} Rules geladen (offset {offset}, gesamt bisher: {len(all_rules)})")
        if len(batch) < page_size:
            break
        offset += page_size

    logger.info(f"✅ fetch_all_rules: {len(all_rules)} Rules total geladen")
    return all_rules


# =============================================================================
# v3.5.0 (7): CHUNKED-URL VORFILTER
# Holt vorab alle URLs einer Domain, die bereits status='chunked' haben, damit
# sie gar nicht erst gecrawlt werden (statt erst beim Speichern auszusortieren).
# =============================================================================

def fetch_chunked_urls_for_domain(base_domain: str) -> Set[str]:
    chunked: Set[str] = set()
    try:
        page_size = 1000
        offset = 0
        while True:
            resp = (
                supabase.table("discovered_urls")
                .select("url")
                .eq("status", "chunked")
                .ilike("url", f"%{base_domain}%")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            batch = resp.data or []
            if not batch:
                break
            for row in batch:
                chunked.add(normalize_url(row["url"]))
            if len(batch) < page_size:
                break
            offset += page_size
        if chunked:
            logger.info(f"🔒 {len(chunked)} bereits gechunkte URLs werden übersprungen ({base_domain})")
    except Exception as e:
        logger.warning(f"⚠️ Chunked-Vorfilter fehlgeschlagen für {base_domain}: {str(e)}")
    return chunked


# =============================================================================
# SITEMAP PARSER
# v3.5.0 (4): robots.txt wird gelesen, um zusätzliche Sitemap:-Einträge zu finden.
# Disallow-Regeln werden NICHT ausgewertet.
# =============================================================================

async def fetch_robots_sitemaps(base_url: str) -> List[str]:
    """Liest NUR die Sitemap:-Zeilen aus robots.txt. Disallow wird ignoriert."""
    robots_url = urljoin(base_url, "/robots.txt")
    sitemaps: List[str] = []
    try:
        client = await get_http_client()
        r = await client.get(robots_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/3.6)"})
        if r.status_code != 200:
            return []
        for line in r.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm.startswith("http"):
                    sitemaps.append(sm)
        if sitemaps:
            logger.info(f"🤖 robots.txt: {len(sitemaps)} Sitemap-Einträge gefunden")
    except Exception as e:
        logger.info(f"🤖 robots.txt nicht verfügbar: {str(e)}")
    return sitemaps


async def _parse_sitemap_url(client, sitemap_url: str) -> List[str]:
    """Parst eine einzelne Sitemap-URL (inkl. Sitemap-Index, max 5 Sub-Sitemaps)."""
    urls: List[str] = []
    try:
        r = await client.get(sitemap_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/3.6)"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        sub_sitemaps = root.findall(f".//{ns}sitemap/{ns}loc")
        if sub_sitemaps:
            for sitemap_loc in sub_sitemaps[:5]:
                if not sitemap_loc.text:
                    continue
                sub_url = sitemap_loc.text.strip()
                try:
                    sub_r = await client.get(sub_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/3.6)"})
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
    except Exception as e:
        logger.info(f"📋 Sitemap nicht parsebar ({sitemap_url}): {str(e)}")
    return urls


async def fetch_sitemap_urls(base_url: str) -> List[str]:
    """
    Sammelt URLs aus /sitemap.xml UND aus allen in robots.txt gelisteten Sitemaps.
    v3.5.0 (4): robots.txt-Sitemaps ergänzen die Standard-Sitemap.
    """
    client = await get_http_client()
    all_urls: List[str] = []
    seen_sitemaps: Set[str] = set()

    # 1. Standard /sitemap.xml
    default_sitemap = urljoin(base_url, "/sitemap.xml")
    seen_sitemaps.add(default_sitemap)
    all_urls.extend(await _parse_sitemap_url(client, default_sitemap))

    # 2. Zusätzliche Sitemaps aus robots.txt
    for sm in await fetch_robots_sitemaps(base_url):
        if sm not in seen_sitemaps:
            seen_sitemaps.add(sm)
            all_urls.extend(await _parse_sitemap_url(client, sm))

    # Dedupe unter Beibehaltung der Reihenfolge
    deduped = list(dict.fromkeys(all_urls))
    logger.info(f"📋 Sitemap(s): {len(deduped)} URLs gesamt (aus {len(seen_sitemaps)} Sitemap-Quellen)")
    return deduped


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


def _parse_retry_after(value: str) -> Optional[float]:
    """Retry-After kann Sekunden (int) oder ein HTTP-Datum sein. Gedeckelt auf MAX_RETRY_AFTER."""
    if not value:
        return None
    value = value.strip()
    try:
        secs = float(value)
        return max(0.0, min(secs, MAX_RETRY_AFTER))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, min(delta, MAX_RETRY_AFTER))
    except Exception:
        pass
    return None


async def fetch_html_fast(
    url: str,
    domain_fails: Dict[str, int],
    rate_limiter: "DomainRateLimiter",
    is_target: bool = False,
) -> tuple:
    """
    Gibt (html_or_none, is_pdf) zurueck.

    is_pdf=True heisst: der Server antwortet mit Content-Type application/pdf,
    obwohl die URL selbst kein '.pdf' im Pfad/Query hatte (z.B. Dokumenten-
    Downloads ueber IDs wie /download?id=123 — bei Behoerden-Seiten haeufig).
    Vorher wurde das hier still als "kein HTML" verworfen und die URL landete
    nie in discovered_urls — der einzige PDF-Pfad war die Substring-Pruefung
    weiter oben in process_page, die nur bei '.pdf' im URL-String greift.
    """
    domain = get_domain(url)
    if not is_target and domain_fails.get(domain, 0) >= DOMAIN_FAIL_THRESHOLD:
        logger.info(f"⛔ Domain geblockt, skip: {domain} ({url})")
        return None, False
    client = await get_http_client()
    for attempt in range(MAX_RETRIES):
        # v3.5.0 (5): Höflichkeits-Pause pro Domain vor jedem Request
        await rate_limiter.wait(domain)
        try:
            r = await client.get(url)
            if r.status_code == 200:
                # v3.5.0 (6): Content-Type prüfen — nur HTML weiterverarbeiten
                content_type = r.headers.get("content-type", "").lower()
                if "application/pdf" in content_type:
                    logger.info(f"📄 PDF per Content-Type erkannt (kein .pdf im Link): {url}")
                    return None, True
                if content_type and "html" not in content_type and "xml" not in content_type:
                    logger.info(f"⏭️ Kein HTML ({content_type.split(';')[0]}), skip: {url}")
                    return None, False
                # v3.5.0 (6): Größen-Check — Riesencontent nicht in den RAM laden
                content_length = r.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_CONTENT_BYTES:
                            logger.info(f"⏭️ Content zu groß ({content_length} bytes), skip: {url}")
                            return None, False
                    except ValueError:
                        pass
                if len(r.content) > MAX_CONTENT_BYTES:
                    logger.info(f"⏭️ Content zu groß ({len(r.content)} bytes), skip: {url}")
                    return None, False
                return r.text, False
            elif r.status_code in (403, 429):
                # v3.5.0 (5): Bei 429 Retry-After respektieren (gedeckelt)
                if r.status_code == 429 and attempt < MAX_RETRIES - 1:
                    retry_after = _parse_retry_after(r.headers.get("retry-after", ""))
                    delay = retry_after if retry_after is not None else RETRY_DELAYS[attempt]
                    logger.info(f"🔄 429 Retry-After {delay:.1f}s für {url}")
                    await asyncio.sleep(delay)
                    continue
                if not is_target:
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                    logger.warning(f"⚠️ Domain Fehler {domain_fails[domain]}/{DOMAIN_FAIL_THRESHOLD}: {domain} (Status {r.status_code})")
                    if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                        logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                else:
                    logger.warning(f"⚠️ Target URL geblockt (Status {r.status_code}), kein Domain-Block: {url}")
                return None, False
            elif r.status_code in (503, 502):
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    continue
                else:
                    if not is_target:
                        domain_fails[domain] = domain_fails.get(domain, 0) + 1
                        if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                            logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                    return None, False
            else:
                logger.warning(f"⚠️ httpx Status {r.status_code} für {url}")
                return None, False
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            else:
                if not is_target:
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                    if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                        logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                return None, False
        except Exception as e:
            logger.warning(f"⚠️ httpx Fehler für {url}: {str(e)}")
            return None, False
    return None, False


async def fetch_html_playwright(url: str, browser) -> Optional[str]:
    # v3.5.0 (8): page.close() in finally — kein Page-Leak bei Exceptions
    page = None
    try:
        page = await browser.new_page()
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        html = await page.content()
        return html
    except Exception as e:
        logger.warning(f"⚠️ Playwright Fehler für {url}: {str(e)}")
        return None
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


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


def _pdf_stub(url: str, depth: int, rule: Dict) -> Dict:
    """Baut den discovered_urls-Eintrag für ein erkanntes PDF (kein Fetch/Parse nötig)."""
    return {
        "url": url,
        "page_title": url.split("/")[-1][:500] or "document.pdf",
        "is_main_url": False,
        "discovered_depth": depth,
        "rule_id": rule['rule_id'],
        "country_code": rule['country_iso'],
        "country_name": rule['country_name'],
        "target_group": rule['target_group']
    }


def _extract_internal_links(html: str, url: str, base_domain: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        a.get("href", "") for a in soup.select("a[href]")
        if is_internal(urljoin(url, a.get("href", "")), base_domain)
    ]


def _parse_page(html: str, url: str, base_domain: str, depth: int, max_depth: int) -> tuple:
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

    return page_title, child_links


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
    # v3.5.0 (8): Lock um die Browser-Initialisierung (Race-Condition bei parallelen Tasks)
    playwright_lock = asyncio.Lock()
    domain_fails: Dict[str, int] = {}
    # v3.5.0 (5): ein Rate-Limiter pro Rule (deckt Haupt- + Sub-Domains ab)
    rate_limiter = DomainRateLimiter(RATE_LIMIT_MIN, RATE_LIMIT_MAX)
    normalized_start = normalize_url(start_url)

    # v3.5.0 (7): bereits gechunkte URLs vorab laden — werden nicht erneut gecrawlt
    chunked_skip = fetch_chunked_urls_for_domain(base_domain)

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
        if normalized_sm in chunked_skip:
            continue
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
                if is_target:
                    return url, depth, is_target, None, []
                return url, depth, is_target, _pdf_stub(url, depth, rule), []

            html, detected_pdf = await fetch_html_fast(url, domain_fails, rate_limiter, is_target=is_target)

            # fix: PDF per Content-Type erkannt, obwohl kein '.pdf' im Link stand
            # (z.B. /download?id=123). Vorher wurde das hier still verworfen und
            # die URL landete nie in discovered_urls.
            if detected_pdf:
                if is_target:
                    return url, depth, is_target, None, []
                return url, depth, is_target, _pdf_stub(url, depth, rule), []

            # perf: needs_javascript() parst mit BeautifulSoup (CPU-lastig) —
            # in Thread auslagern, damit der Event-Loop während dessen nicht
            # blockiert und die anderen Tasks im selben Batch weiterlaufen.
            needs_pw = html and await asyncio.to_thread(needs_javascript, html)

            if html and not needs_pw:
                internal_links = await asyncio.to_thread(_extract_internal_links, html, url, base_domain)
                if len(internal_links) == 0:
                    needs_pw = True
                    logger.info(f"🔄 0 interne Links, nutze Playwright: {url}")

            if needs_pw:
                # v3.5.0 (8): Lock verhindert doppelte Browser-Initialisierung
                if not playwright_browser:
                    async with playwright_lock:
                        if not playwright_browser:
                            playwright_instance = await async_playwright().start()
                            playwright_browser = await playwright_instance.chromium.launch(
                                headless=True,
                                args=['--no-sandbox', '--disable-dev-shm-usage']
                            )
                html = await fetch_html_playwright(url, playwright_browser)

            if not html:
                return url, depth, is_target, None, []

            # perf: BeautifulSoup-Parse + Link-Extraktion in Thread auslagern
            # (gleicher Grund wie oben — CPU-lastig, blockiert sonst den Loop).
            page_title, child_links = await asyncio.to_thread(
                _parse_page, html, url, base_domain, depth, max_depth
            )

            if is_target:
                return url, depth, is_target, None, child_links

            result = {
                "url": url,
                "page_title": page_title[:500],
                "is_main_url": False,
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
            # v3.5.0 (7): gechunkte URLs überspringen (außer Target)
            if not is_target and normalized in chunked_skip:
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
                if (link not in visited and link not in to_visit_set
                        and link not in chunked_skip
                        and len(visited) + len(to_visit) < max_pages * 2):
                    to_visit.append((link, depth + 1, False))
                    to_visit_set.add(link)

    if playwright_browser:
        await playwright_browser.close()
    if playwright_instance:
        await playwright_instance.stop()

    logger.info(f"✅ [{rule['rule_id']}] Discovery complete: {len(discovered_urls)} Sub-Links (visited {len(visited)} pages)")
    return discovered_urls


# =============================================================================
# SUPABASE FUNCTIONS
# =============================================================================

def save_urls_to_supabase(discovered_urls: List[Dict]) -> int:
    if not discovered_urls:
        return 0

    logger.info(f"💾 Preparing to save {len(discovered_urls)} Sub-Links to Supabase...")
    seen_urls = set()
    insert_data = []
    duplicates_removed = 0

    for url_data in discovered_urls:
        if url_data.get("is_main_url", False):
            continue
        url = url_data["url"]
        if url in seen_urls:
            duplicates_removed += 1
            continue
        seen_urls.add(url)
        insert_data.append({
            "url":              url,
            "page_title":       url_data["page_title"],
            "is_main_url":      False,
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
        logger.info("✅ No new Sub-Links to save (all already chunked or empty)")
        return 0

    try:
        response = supabase.table("discovered_urls").upsert(insert_data, on_conflict="url").execute()
        inserted_count = len(response.data) if response.data else 0
        logger.info(f"✅ {inserted_count} Sub-Links saved successfully")
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
# v3.6.0: CRAWL-ATTEMPTS TRACKING
# Rules die dauerhaft 0 Sub-Links liefern (Login-Wall, Site offline, tot) bekamen
# nie einen Timestamp und wurden bei jedem Lauf erneut versucht (Dauerschleife).
# increment_crawl_attempts() zählt Fehlversuche; nach MAX_FAILED_ATTEMPTS wird die
# Rule deaktiviert (active=false). reset_crawl_attempts() setzt bei Erfolg zurück.
# =============================================================================

def increment_crawl_attempts(rule_id: str):
    """
    Zählt Fehlversuche hoch. Nach MAX_FAILED_ATTEMPTS wird die Rule deaktiviert.
    Wird nur aufgerufen wenn saved_count = 0.
    """
    try:
        result = (
            supabase.table("config_rules")
            .select("crawl_attempts")
            .eq("rule_id", rule_id)
            .single()
            .execute()
        )
        current = (result.data.get("crawl_attempts", 0) or 0) if result.data else 0
        new_count = current + 1

        update_data = {
            "crawl_attempts": new_count,
            "last_crawl_failed_at": "now()",
        }

        if new_count >= MAX_FAILED_ATTEMPTS:
            update_data["active"] = False
            logger.warning(
                f"⛔ Rule {rule_id} nach {MAX_FAILED_ATTEMPTS} Fehlversuchen "
                f"deaktiviert (active = false)"
            )

        supabase.table("config_rules").update(update_data).eq("rule_id", rule_id).execute()
        logger.info(f"📊 crawl_attempts für {rule_id}: {new_count}/{MAX_FAILED_ATTEMPTS}")
    except Exception as e:
        logger.warning(f"⚠️ Could not increment crawl_attempts: {str(e)}")


def reset_crawl_attempts(rule_id: str):
    """
    Setzt Fehlversuche zurück wenn eine Rule erfolgreich gecrawlt wurde.
    Falls eine Site temporär down war und später wieder erreichbar ist, wird sie
    nicht dauerhaft deaktiviert.
    """
    try:
        supabase.table("config_rules").update({
            "crawl_attempts": 0,
            "last_crawl_failed_at": None,
        }).eq("rule_id", rule_id).execute()
    except Exception as e:
        logger.warning(f"⚠️ Could not reset crawl_attempts: {str(e)}")


# =============================================================================
# v3.0.0: HINTERGRUND-JOB FUNKTION
# =============================================================================

async def run_discovery_job(job_id: str, rules: List[Dict], max_urls_override: Optional[int]):
    job = JOB_STORE[job_id]
    job["status"] = "running"
    job["total_rules"] = len(rules)

    rule_semaphore = asyncio.Semaphore(MAX_PARALLEL_RULES)

    async def process_rule(rule: Dict, index: int) -> Dict:
        async with rule_semaphore:
            job["current_country"] = f"{rule['country_name']} ({rule['rule_id']})"
            logger.info(f"▶️  [{job_id}] Rule {index}/{len(rules)}: {rule['rule_id']} – {rule['country_name']} / {rule['target_group']}")

            try:
                if max_urls_override:
                    rule['max_urls'] = max_urls_override

                discovered = await discover_urls(rule)
                saved_count = save_urls_to_supabase(discovered)

                if saved_count > 0:
                    update_last_crawled(rule['rule_id'])
                    reset_crawl_attempts(rule['rule_id'])
                    logger.info(f"✅ [{job_id}] Rule {rule['rule_id']} fertig: {saved_count} Sub-Links — last_crawled_at gesetzt, crawl_attempts reset")
                else:
                    increment_crawl_attempts(rule['rule_id'])
                    logger.info(f"⚠️ [{job_id}] Rule {rule['rule_id']}: 0 Sub-Links — crawl_attempts erhöht")

                job["processed_rules"] += 1
                job["successful_rules"] += 1
                job["total_urls_found"] += saved_count

                logger.info(f"✅ [{job_id}] {job['processed_rules']}/{job['total_rules']} Rules done")

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
        logger.info(f"🎉 [{job_id}] Job completed: {job['total_urls_found']} Sub-Links, {job['successful_rules']} rules OK, {job['failed_rules']} failed")

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
    status: str
    total_rules: int
    processed_rules: int
    skipped_rules: int
    successful_rules: int
    failed_rules: int
    total_urls_found: int
    current_country: Optional[str]
    started_at: str
    finished_at: Optional[str]
    progress_pct: float

class DirectDiscoveryResponse(BaseModel):
    success: bool
    total_urls_found: int
    urls: List[Dict]


@app.get("/")
async def root():
    return {
        "service": "Visa Scraper Discovery API",
        "version": "3.7.0",
        "status": "running",
        "changes_v3.7.0": [
            "MAX_PARALLEL_RULES 2 → 4 nach Hetzner-Kapazitaetscheck (28.07.2026)",
            "Headroom bestaetigt: 5,6GB frei, Load 0.3 auf 4 Kernen, Swap ungenutzt",
            "Alter OOM-Kill im Log vermutlich vor Playwright-Leak-Fix (v3.5.0) — RAM nach Deploy beobachten",
        ],
        "changes_v3.6.0": [
            "crawl_attempts Fix: tote Rules werden nach MAX_FAILED_ATTEMPTS=5 deaktiviert",
            "reset_crawl_attempts bei Erfolg — temporär tote Sites werden nicht dauerhaft entfernt",
            "Zwei neue Spalten in config_rules: crawl_attempts, last_crawl_failed_at",
        ]
    }


@app.get("/health")
async def health():
    running_jobs = sum(1 for j in JOB_STORE.values() if j["status"] == "running")
    return {
        "status": "healthy",
        "version": "3.7.0",
        "supabase_connected": bool(SUPABASE_URL and SUPABASE_KEY),
        "active_jobs": running_jobs,
    }


@app.post("/discover", response_model=DiscoveryJobResponse)
async def run_discovery(request: DiscoveryRequest):
    logger.info("=" * 80)
    logger.info(f"🚀 DISCOVERY API v3.7.0 — JOB MODE")
    logger.info("=" * 80)

    try:
        query = (
            supabase.table("config_rules")
            .select("*")
            .eq("active", True)
        )

        if request.rule_ids:
            query = query.in_("rule_id", request.rule_ids)
        if request.filter:
            if "country_iso" in request.filter:
                query = query.eq("country_iso", request.filter["country_iso"])
            if "target_group" in request.filter:
                query = query.eq("target_group", request.filter["target_group"])

        all_rules = fetch_all_rules(query)

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
            logger.info(f"✅ Found {len(urls)} Sub-Links from start URL {i}")

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
    logger.info("🚀 Starting Visa Scraper Discovery API v3.7.0...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"⚡ Concurrent limit per rule: {CONCURRENT_LIMIT}")
    logger.info(f"⚡ Max parallel rules: {MAX_PARALLEL_RULES}")
    logger.info(f"⚡ Domain fail threshold (Sub-URLs): {DOMAIN_FAIL_THRESHOLD}")
    logger.info(f"⚡ Rate-Limit pro Domain: {RATE_LIMIT_MIN}-{RATE_LIMIT_MAX}s (random)")
    logger.info(f"⚡ Max Content: {MAX_CONTENT_BYTES // (1024*1024)} MB, nur HTML")
    logger.info(f"⚡ robots.txt: nur Sitemap-Discovery (Disallow ignoriert)")
    logger.info(f"⚡ Chunked-Vorfilter: aktiv")
    logger.info(f"⚡ crawl_attempts: Deaktivierung nach {MAX_FAILED_ATTEMPTS} Fehlversuchen")
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
