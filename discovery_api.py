"""
URL Discovery API for Visa Scraper
FastAPI Service deployed on Railway.app
Calls Python discovery logic and returns results to n8n

v2.0.0 - Optimized Crawling Engine
  - httpx als Standard (Playwright nur Fallback für JS-Seiten)
  - Paralleles Crawling (10 gleichzeitig)
  - URL-Path-Filter (nur relevante Pfade)
  - Sitemap-Parser (checkt /sitemap.xml zuerst)
  - Bessere Text-Extraktion (main/article statt ganze Seite)
  - Bessere URL-Normalisierung (query params entfernt)

v2.0.1 - Crawling-Engine Feinschliff
  - Globaler httpx Client (Connection Pooling statt Client pro Request)
  - deque statt list für Crawl-Queue (O(1) statt O(n))
  - to_visit_set für Duplicate-Check (O(1) statt O(n) List Comprehension)
  - Retry-Logik (3 Versuche mit Backoff bei Timeout/429/503)

v1.4.0 - fetch_worldbank replaced by fetch_apis (generic API fetcher)
v1.3.0 - GROUP B excluded from Discovery (World Bank API handles GROUP B)
v1.2.0 - Added /fetch-markdown endpoint (Jina replacement)
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

# Fetch Markdown Router (Jina Replacement)
from fetch_markdown import router as fetch_markdown_router

# Generic API Fetcher Router (GROUP B Finanzen – World Bank, BLS, Eurostat...)
from fetch_apis import router as fetch_apis_router

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI App
app = FastAPI(
    title="Visa Scraper Discovery API",
    description="URL Discovery Service for Visa Immigration Data Scraping",
    version="2.0.0"
)

# CORS Middleware (für n8n)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Router einbinden
app.include_router(fetch_markdown_router)
app.include_router(fetch_apis_router)

# Supabase Connection (aus Environment Variables)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set as environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# GROUP B wird von fetch_apis gehandelt – nicht von Discovery
# =============================================================================

EXCLUDED_FROM_DISCOVERY = ["GROUP B: FINANZEN"]

# =============================================================================
# CRAWLING CONFIG
# =============================================================================

# Max gleichzeitige Requests
CONCURRENT_LIMIT = 10

# Retry-Config für fehlgeschlagene Requests
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]  # Sekunden zwischen den Versuchen

# Query-Parameter die beim Normalisieren entfernt werden
IGNORED_QUERY_PARAMS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "session", "sid",
    "page", "p", "lang", "language", "locale",
]

# URL-Pfade die NICHT gecrawlt werden (spart 80-95% unnötige Requests)
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
    ".jpg", ".png", ".gif", ".zip", ".doc", ".xls",
    "/print/", "/pdf/", "/download/",
    "/social", "/share", "/tweet", "/facebook",
    "/calendar", "/events/2", "/archive",
    "/comment", "/reply",
    "#",
]

# Relevante URL-Pfade die BEVORZUGT gecrawlt werden
RELEVANT_PATH_KEYWORDS = [
    "visa", "visum", "immigration", "einwanderung", "residence",
    "aufenthalt", "permit", "genehmigung", "migrate", "migration",
    "foreign", "ausland", "expat", "work-permit", "arbeitserlaubnis",
    "study", "studium", "student", "citizen", "staatsbürger",
    "naturalization", "einbürgerung", "asylum", "asyl", "refugee",
    "green-card", "settlement", "niederlassung", "entry", "einreise",
    "travel", "reise", "consular", "konsular", "embassy", "botschaft",
    "registration", "anmeldung", "register", "document", "dokument",
    "certificate", "bescheinigung", "passport", "reisepass",
    "education", "bildung", "school", "schule", "university", "universität",
    "tuition", "gebühr", "scholarship", "stipendium", "admission", "zulassung",
    "driver", "führerschein", "insurance", "versicherung",
    "tax", "steuer", "social-security", "sozialversicherung",
    "how-to", "guide", "ratgeber", "information", "faq",
    "requirement", "voraussetzung", "application", "antrag",
    "service", "dienstleistung",
]

# =============================================================================
# KEYWORDS CONFIG (nur noch A, E, F) — UNVERÄNDERT
# =============================================================================

GROUP_KEYWORDS = {
    "GROUP A: VISA & RECHT": [
        "visa", "residence", "permit", "immigration", "documents",
        "requirements", "processing", "application", "green card",
        "citizenship", "work permit", "entry", "border", "consulate",
        "embassy", "naturalization", "permanent residence", "asylum",
        "refugee", "deportation", "overstay", "grace period",
        "sponsorship", "petition", "adjustment of status", "i-140", "i-485",
        "eb-1", "eb-2", "eb-3", "eb-5", "h-1b", "l-1", "o-1",
        "legal status", "authorization", "immigration law", "attorney",
        "lawyer", "appeal", "denial", "approval", "biometrics"
    ],
    "GROUP E: BILDUNG": [
        "education", "school", "university", "college", "study",
        "student", "degree", "diploma", "certificate", "course",
        "program", "major", "bachelor", "master", "phd", "doctorate",
        "tuition", "tuition fee", "scholarship", "grant", "financial aid",
        "admission", "enrollment", "application", "requirements",
        "international student", "exchange student", "language course",
        "language school", "language test", "toefl", "ielts",
        "kindergarten", "preschool", "elementary", "secondary",
        "high school", "private school", "public school",
        "international school", "curriculum", "semester", "academic year",
        "grades", "transcript", "credits", "exam", "test",
        "library", "campus", "dormitory", "student visa", "f-1",
        "vocational training", "apprenticeship", "qualification recognition"
    ],
    "GROUP F: BÜROKRATIE": [
        "registration", "register", "bureaucracy", "administration",
        "office", "authority", "government", "municipal", "city hall",
        "police registration", "residence registration", "anmeldung",
        "form", "document", "certificate", "notary", "notarization",
        "apostille", "authentication", "translation", "certified copy",
        "passport", "id card", "identity document", "national id",
        "driver license", "driving license", "international permit",
        "vehicle registration", "car registration", "insurance",
        "mandatory insurance", "social security number", "tax id",
        "tax number", "tax office", "filing", "deadline",
        "mobile phone", "sim card", "phone contract", "address",
        "change of address", "utility contract", "electricity contract",
        "shipping", "relocation", "moving", "customs", "import",
        "export", "flight", "ticket", "travel document", "visa application fee"
    ]
}

GENERAL_KEYWORDS = [
    "immigrant", "expat", "foreigner", "international", "foreign national",
    "non-citizen", "relocate", "relocation", "moving to", "living in",
    "information", "guide", "how to", "requirements", "process", "procedure"
]


# =============================================================================
# HELPER FUNCTIONS (VERBESSERT)
# =============================================================================

def normalize_url(url: str) -> str:
    """Normalisiert URL: entfernt Fragment UND unnötige Query-Parameter."""
    parsed = urlparse(url)

    # Query-Parameter filtern
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {
            k: v for k, v in params.items()
            if k.lower() not in IGNORED_QUERY_PARAMS
        }
        clean_query = urlencode(filtered, doseq=True) if filtered else ""
    else:
        clean_query = ""

    # Trailing slash normalisieren
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"

    normalized = parsed._replace(fragment="", query=clean_query, path=path).geturl()
    return normalized


def is_internal(url: str, base_domain: str) -> bool:
    """Prüft ob URL zur gleichen Domain gehört."""
    try:
        ext = tldextract.extract(url)
        current_domain = f"{ext.domain}.{ext.suffix}"
        return current_domain == base_domain
    except:
        return False


def is_blocked_path(url: str) -> bool:
    """Prüft ob der URL-Pfad auf der Blocklist steht."""
    path_lower = urlparse(url).path.lower()
    return any(blocked in path_lower for blocked in BLOCKED_PATH_PATTERNS)


def is_relevant_path(url: str) -> bool:
    """Prüft ob der URL-Pfad relevant genug ist um gecrawlt zu werden.
    Lockerer Filter: erlaubt kurze Pfade, Keyword-Matches und
    Pfade mit Zahlen (oft Visa-Kategorien, Formular-Nummern, Subclasses).
    """
    path = urlparse(url).path.lower()

    segments = [s for s in path.split("/") if s]

    # Kurze Pfade (bis 2 Segmente) immer erlauben — Hauptkategorien
    if len(segments) <= 2:
        return True

    # Keyword Match
    if any(kw in path for kw in RELEVANT_PATH_KEYWORDS):
        return True

    # Zahlen im Pfad → oft Visa-Subclasses, Formular-Nummern, Programm-IDs
    # z.B. /subclass-189, /form-47, /program-2024
    if any(segment for segment in segments if any(c.isdigit() for c in segment)):
        return True

    return False


def extract_main_content(soup: BeautifulSoup) -> str:
    """Extrahiert nur den Hauptinhalt der Seite (nicht Navigation/Footer/Sidebar)."""
    # Versuche zuerst spezifische Content-Container zu finden
    content = soup.select_one(
        "main, "
        "article, "
        "[role='main'], "
        "#content, #main-content, #main, "
        ".content, .main-content, .page-content, .entry-content, "
        ".article-body, .post-content"
    )

    if content:
        # Innerhalb des Content-Containers: Navigation etc. entfernen
        for tag in content.select("nav, footer, header, aside, .sidebar, .menu, .nav, .breadcrumb"):
            tag.decompose()
        text = content.get_text(" ", strip=True)
        # Nur verwenden wenn genug Text vorhanden
        if len(text) > 100:
            return text

    # Fallback: ganze Seite, aber Navigation/Footer etc. entfernen
    for tag in soup.select("nav, footer, header, aside, .sidebar, .menu, .nav, .breadcrumb, .cookie, script, style"):
        tag.decompose()

    return soup.get_text(" ", strip=True)


def score_url(url: str, text: str, target_group: str) -> int:
    """Relevanz-Score berechnen (verbessert: URL-Pfad stärker gewichtet)."""
    score = 0
    url_lower = url.lower()
    text_lower = text.lower()
    path_lower = urlparse(url).path.lower()

    # General Keywords
    general_matches = sum(1 for kw in GENERAL_KEYWORDS if kw in url_lower or kw in text_lower)
    score += min(general_matches, 2)

    if target_group in GROUP_KEYWORDS:
        group_kws = GROUP_KEYWORDS[target_group]

        # URL-Pfad Keywords (höchstes Gewicht — Pfad ist stärkster Indikator)
        path_matches = sum(1 for kw in group_kws if kw in path_lower)
        score += min(path_matches * 3, 6)

        # URL komplett (inkl. Domain)
        url_matches = sum(1 for kw in group_kws if kw in url_lower and kw not in path_lower)
        score += min(url_matches, 2)

        # Text-Inhalt
        text_matches = sum(1 for kw in group_kws if kw in text_lower)
        score += min(text_matches, 4)

    # Text-Länge Bonus
    if len(text) > 3000:
        score += 2
    elif len(text) > 1500:
        score += 1
    if len(text) < 200:
        score = max(1, score - 2)

    return max(1, min(score, 10))


def extract_topics(text: str, url: str, target_group: str) -> List[str]:
    """Extrahiert Topics aus Text und URL — UNVERÄNDERT."""
    topics = []
    text_lower = text.lower()
    url_lower = url.lower()

    topic_maps = {
        "GROUP A: VISA & RECHT": {
            "tourist visa": ["tourist", "visitor", "b-2", "tourism"],
            "work visa": ["work permit", "employment authorization", "h-1b", "l-1"],
            "student visa": ["student", "f-1", "study"],
            "green card": ["green card", "permanent residence", "eb-1", "eb-2"],
            "citizenship": ["citizenship", "naturalization"],
            "visa application": ["application", "form", "filing"],
            "legal rights": ["legal", "rights", "law", "attorney"]
        },
        "GROUP E: BILDUNG": {
            "universities": ["university", "college"],
            "schools": ["school", "kindergarten", "elementary"],
            "tuition fees": ["tuition", "fee", "scholarship"],
            "admission": ["admission", "application", "enrollment"]
        },
        "GROUP F: BÜROKRATIE": {
            "registration": ["registration", "anmeldung", "register"],
            "documents": ["document", "certificate", "form"],
            "id documents": ["passport", "id card"],
            "driver license": ["driver license", "driving permit"]
        }
    }

    if target_group in topic_maps:
        topic_map = topic_maps[target_group]
        for topic, keywords in topic_map.items():
            if any(kw in text_lower or kw in url_lower for kw in keywords):
                topics.append(topic)

    return topics[:5]


# =============================================================================
# NEU: SITEMAP PARSER
# =============================================================================

async def fetch_sitemap_urls(base_url: str) -> List[str]:
    """Versucht /sitemap.xml zu laden und gibt alle URLs zurück.
    Unterstützt auch Sitemap-Indexes (sitemap of sitemaps).
    """
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    urls = []

    try:
        client = await get_http_client()
        r = await client.get(sitemap_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; VisaScraper/2.0)"
        })

        if r.status_code != 200:
            logger.info(f"📋 Keine Sitemap gefunden bei {sitemap_url}")
            return []

        root = ET.fromstring(r.text)

        # Namespace handling (sitemaps haben oft xmlns)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # Prüfe ob es ein Sitemap-Index ist
        sitemaps = root.findall(f".//{ns}sitemap/{ns}loc")
        if sitemaps:
            logger.info(f"📋 Sitemap-Index gefunden mit {len(sitemaps)} Sub-Sitemaps")
            for sitemap_loc in sitemaps[:5]:  # Max 5 Sub-Sitemaps
                sub_url = sitemap_loc.text.strip()
                try:
                    sub_r = await client.get(sub_url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; VisaScraper/2.0)"
                    })
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
            # Normale Sitemap
            for loc in root.findall(f".//{ns}loc"):
                if loc.text:
                    urls.append(loc.text.strip())

        logger.info(f"📋 Sitemap: {len(urls)} URLs gefunden")

    except Exception as e:
        logger.info(f"📋 Sitemap nicht verfügbar: {str(e)}")

    return urls


# =============================================================================
# NEU: GLOBALER HTTPX CLIENT (Connection Pooling)
# =============================================================================

# Ein Client für alle Requests → wiederverwendet TCP-Verbindungen
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Gibt den globalen httpx Client zurück (erstellt ihn beim ersten Aufruf)."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=CONCURRENT_LIMIT + 5,
                max_keepalive_connections=CONCURRENT_LIMIT,
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


async def fetch_html_fast(url: str) -> Optional[str]:
    """Schneller HTML-Fetch mit httpx + Retry-Logik.
    Versucht bis zu MAX_RETRIES Mal mit steigender Wartezeit.
    """
    client = await get_http_client()

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            elif r.status_code in (429, 503, 502):
                # Rate Limit oder Server überlastet → Retry
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.info(f"🔄 Retry {attempt + 1}/{MAX_RETRIES} für {url} (Status {r.status_code}, warte {delay}s)")
                    await asyncio.sleep(delay)
                    continue
            else:
                logger.warning(f"⚠️ httpx Status {r.status_code} für {url}")
                return None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                logger.info(f"🔄 Retry {attempt + 1}/{MAX_RETRIES} für {url} ({type(e).__name__}, warte {delay}s)")
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning(f"⚠️ httpx Fehler nach {MAX_RETRIES} Versuchen für {url}: {str(e)}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ httpx Fehler für {url}: {str(e)}")
            return None

    return None


async def fetch_html_playwright(url: str, browser) -> Optional[str]:
    """Fallback: Playwright für JavaScript-lastige Seiten."""
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
    """Erkennt ob die Seite JavaScript braucht um Content zu laden."""
    soup = BeautifulSoup(html, "html.parser")
    text = extract_main_content(soup)

    # Wenn kaum Text vorhanden aber viele Script-Tags → braucht JS
    script_count = len(soup.find_all("script"))
    if len(text) < 200 and script_count > 5:
        return True

    # Typische SPA-Frameworks erkennen
    html_lower = html.lower()
    spa_indicators = ["__next", "__nuxt", "react-root", "ng-app", "v-app", "id=\"app\""]
    if len(text) < 200 and any(ind in html_lower for ind in spa_indicators):
        return True

    return False


# =============================================================================
# MAIN DISCOVERY FUNCTION (OPTIMIERT)
# =============================================================================

async def discover_urls(rule: Dict) -> List[Dict]:
    start_url = rule['target_url']
    max_pages = rule['max_urls']
    max_depth = rule['max_depth']

    ext = tldextract.extract(start_url)
    base_domain = f"{ext.domain}.{ext.suffix}"

    visited = set()
    discovered_urls = []

    # Semaphore für paralleles Crawling
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

    # Playwright Browser (nur bei Bedarf gestartet)
    playwright_instance = None
    playwright_browser = None

    logger.info(f"🚀 Starting discovery for: {rule['country_name']} ({rule['rule_id']})")
    logger.info(f"📊 Max URLs: {max_pages}, Max Depth: {max_depth}")

    # ─── SCHRITT 1: Sitemap checken ───
    sitemap_urls = await fetch_sitemap_urls(start_url)

    # Sitemap-URLs filtern: nur interne + relevante Pfade
    if sitemap_urls:
        sitemap_filtered = [
            u for u in sitemap_urls
            if is_internal(u, base_domain)
            and is_relevant_path(u)
            and not is_blocked_path(u)
        ]
        logger.info(f"📋 Sitemap: {len(sitemap_filtered)} relevante URLs (von {len(sitemap_urls)} total)")
    else:
        sitemap_filtered = []

    # ─── SCHRITT 2: Crawl-Queue aufbauen ───
    # Sitemap-URLs haben Priorität (depth=0), dann die Start-URL
    # deque für O(1) popleft statt O(n) list.pop(0)
    to_visit = deque()
    to_visit_set = set()  # Schneller Duplicate-Check statt List Comprehension

    for sm_url in sitemap_filtered[:max_pages]:
        normalized_sm = normalize_url(sm_url)
        if normalized_sm not in to_visit_set:
            to_visit.append((normalized_sm, 0))
            to_visit_set.add(normalized_sm)

    # Start-URL auch hinzufügen falls nicht schon drin
    normalized_start = normalize_url(start_url)
    if normalized_start not in to_visit_set:
        to_visit.appendleft((normalized_start, 0))
        to_visit_set.add(normalized_start)

    # ─── SCHRITT 3: Paralleles Crawling ───

    async def process_page(url: str, depth: int) -> tuple:
        """Einzelne Seite laden und verarbeiten."""
        nonlocal playwright_instance, playwright_browser

        async with semaphore:
            # Schneller Fetch mit httpx
            html = await fetch_html_fast(url)

            # Fallback: Playwright wenn JS benötigt
            if html and needs_javascript(html):
                logger.info(f"🔄 JS-Seite erkannt, nutze Playwright: {url}")
                if not playwright_browser:
                    playwright_instance = await async_playwright().start()
                    playwright_browser = await playwright_instance.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                html = await fetch_html_playwright(url, playwright_browser)

            if not html:
                return url, depth, None, None, [], []

            soup = BeautifulSoup(html, "html.parser")
            text = extract_main_content(soup)

            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True) if title_tag else ""

            relevance = score_url(url, text, rule['target_group'])
            topics = extract_topics(text, url, rule['target_group'])

            # Links für weitere Crawling-Runden sammeln
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

                    # Filter: intern + relevant + nicht blockiert
                    if (is_internal(normalized_full, base_domain)
                            and not is_blocked_path(normalized_full)
                            and is_relevant_path(normalized_full)):
                        child_links.append(normalized_full)

            result = None
            if relevance >= 3:
                result = {
                    "url": url,
                    "page_title": page_title[:500],
                    "relevance_score": relevance,
                    "topics": topics,
                    "discovered_depth": depth,
                    "text_length": len(text),
                    "rule_id": rule['rule_id'],
                    "country_code": rule['country_iso'],
                    "country_name": rule['country_name'],
                    "target_group": rule['target_group']
                }

            return url, depth, result, text, child_links, topics

    # Crawling Loop: Batch-weise parallel
    while to_visit and len(visited) < max_pages:
        # Nächsten Batch vorbereiten (max CONCURRENT_LIMIT Seiten)
        batch = []
        while to_visit and len(batch) < CONCURRENT_LIMIT:
            url, depth = to_visit.popleft()
            to_visit_set.discard(url)
            normalized = normalize_url(url)
            if normalized in visited or depth > max_depth:
                continue
            visited.add(normalized)
            batch.append((normalized, depth))

        if not batch:
            break

        logger.info(f"🔎 Batch: {len(batch)} Seiten parallel (gesamt: {len(visited)}/{max_pages})")

        # Parallel verarbeiten
        tasks = [process_page(url, depth) for url, depth in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"⚠️ Batch-Fehler: {str(result)}")
                continue

            url, depth, url_data, text, child_links, topics = result

            if url_data:
                discovered_urls.append(url_data)

            # Neue Links zur Queue hinzufügen
            for link in child_links:
                if link not in visited and link not in to_visit_set and len(visited) + len(to_visit) < max_pages * 2:
                    to_visit.append((link, depth + 1))
                    to_visit_set.add(link)

    # Playwright aufräumen
    if playwright_browser:
        await playwright_browser.close()
    if playwright_instance:
        await playwright_instance.stop()

    logger.info(f"✅ Discovery complete: {len(discovered_urls)} URLs found (visited {len(visited)} pages)")
    return discovered_urls


# =============================================================================
# SUPABASE FUNCTIONS — UNVERÄNDERT
# =============================================================================

def save_urls_to_supabase(discovered_urls: List[Dict]) -> int:
    if not discovered_urls:
        logger.warning("⚠️ No URLs to save")
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
            "url": url,
            "page_title": url_data["page_title"],
            "relevance_score": url_data["relevance_score"],
            "topics": url_data["topics"],
            "discovered_depth": url_data["discovered_depth"],
            "rule_id": url_data["rule_id"],
            "country_code": url_data["country_code"],
            "country_name": url_data["country_name"],
            "target_group": url_data["target_group"],
            "status": "pending"
        })

    if duplicates_removed > 0:
        logger.info(f"🧹 Removed {duplicates_removed} duplicate URLs from batch")

    try:
        response = supabase.table("discovered_urls").upsert(
            insert_data,
            on_conflict="url"
        ).execute()
        inserted_count = len(response.data) if response.data else 0
        logger.info(f"✅ {inserted_count} URLs saved successfully")
        return inserted_count
    except Exception as e:
        logger.error(f"❌ Error saving to Supabase: {str(e)}")
        return 0


def update_last_crawled(rule_id: str):
    try:
        supabase.table("config_rules").update({
            "last_crawled_at": "now()"
        }).eq("rule_id", rule_id).execute()
        logger.info(f"✅ Updated last_crawled_at for {rule_id}")
    except Exception as e:
        logger.warning(f"⚠️ Could not update last_crawled_at: {str(e)}")


# =============================================================================
# API ENDPOINTS — UNVERÄNDERT (gleiche Interfaces für n8n)
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

class DiscoveryResponse(BaseModel):
    success: bool
    total_rules_processed: int
    total_urls_found: int
    successful_rules: int
    failed_rules: int
    results_per_rule: List[Dict]

class DirectDiscoveryResponse(BaseModel):
    success: bool
    total_urls_found: int
    urls: List[Dict]


@app.get("/")
async def root():
    return {
        "service": "Visa Scraper Discovery API",
        "version": "2.0.0",
        "status": "running",
        "improvements": [
            "httpx + Playwright fallback (10x faster)",
            "Parallel crawling (10 concurrent)",
            "URL path filtering (80-95% less noise)",
            "Sitemap parser (finds URLs instantly)",
            "Smart content extraction (main/article only)",
            "Better URL normalization (no utm/tracking params)"
        ],
        "endpoints": {
            "discover": "/discover (GROUP A, E, F only – GROUP B via fetch-apis)",
            "discover-direct": "/discover-direct (direct URLs from n8n)",
            "fetch-markdown": "/fetch-markdown?url=... (Jina replacement)",
            "fetch-markdown-batch": "/fetch-markdown-batch (3 URLs parallel)",
            "fetch-apis": "/fetch-apis (GROUP B Finanzen – World Bank, BLS, Eurostat)",
            "health": "/health"
        }
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "supabase_connected": bool(SUPABASE_URL and SUPABASE_KEY)
    }


@app.post("/discover-direct", response_model=DirectDiscoveryResponse)
async def discover_direct(request: DirectDiscoveryRequest):
    """
    Accepts start_urls directly (no Supabase config_rules needed)
    GROUP B wird automatisch abgelehnt
    """

    if any(excluded in request.target_group for excluded in EXCLUDED_FROM_DISCOVERY):
        logger.warning(f"⚠️ GROUP B rejected from discovery – use /fetch-apis instead")
        return DirectDiscoveryResponse(
            success=False,
            total_urls_found=0,
            urls=[]
        )

    logger.info("=" * 80)
    logger.info("🚀 DIRECT DISCOVERY STARTED (v2.0.0 – Optimized)")
    logger.info(f"📋 Country: {request.country_name} ({request.country_code})")
    logger.info(f"📂 Group: {request.target_group}")
    logger.info(f"🔗 Start URLs: {len(request.start_urls)}")
    logger.info("=" * 80)

    discovered_urls = []

    try:
        for i, start_url in enumerate(request.start_urls, 1):
            rule = {
                'target_url': start_url,
                'max_urls': request.max_urls,
                'max_depth': request.max_depth,
                'rule_id': request.rule_id,
                'country_iso': request.country_code,
                'country_name': request.country_name,
                'target_group': request.target_group
            }
            urls = await discover_urls(rule)
            discovered_urls.extend(urls)
            logger.info(f"✅ Found {len(urls)} URLs from start URL {i}")

        saved_count = save_urls_to_supabase(discovered_urls)
        logger.info(f"✅ DIRECT DISCOVERY COMPLETED – {saved_count} URLs saved")

        return DirectDiscoveryResponse(
            success=True,
            total_urls_found=saved_count,
            urls=discovered_urls
        )

    except Exception as e:
        logger.error(f"❌ Critical error in direct discovery: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/discover", response_model=DiscoveryResponse)
async def run_discovery(request: DiscoveryRequest):
    """
    ORIGINAL ENDPOINT (uses config_rules from Supabase)
    GROUP B: FINANZEN wird automatisch übersprungen – fetch-apis übernimmt
    """

    logger.info("=" * 80)
    logger.info("🚀 DISCOVERY API STARTED (v2.0.0 – Optimized Crawling)")
    logger.info(f"📋 Request: {request.dict()}")
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
            logger.warning("⚠️ No active rules found")
            return DiscoveryResponse(
                success=False,
                total_rules_processed=0,
                total_urls_found=0,
                successful_rules=0,
                failed_rules=0,
                results_per_rule=[]
            )

        # GROUP B herausfiltern
        rules = [
            r for r in all_rules
            if not any(excluded in r.get("target_group", "") for excluded in EXCLUDED_FROM_DISCOVERY)
        ]
        skipped = len(all_rules) - len(rules)

        if skipped > 0:
            logger.info(f"⏭️ Skipped {skipped} GROUP B rules (handled by fetch-apis)")

        logger.info(f"✅ Processing {len(rules)} rules (GROUP A, E, F only)")

        total_urls_found = 0
        results_per_rule = []

        for i, rule in enumerate(rules, 1):
            logger.info(f"\n{'=' * 80}")
            logger.info(f"📍 Rule {i}/{len(rules)}: {rule['rule_id']} – {rule['country_name']} / {rule['target_group']}")
            logger.info(f"{'=' * 80}")

            try:
                if request.max_urls:
                    rule['max_urls'] = request.max_urls

                discovered_urls = await discover_urls(rule)
                saved_count = save_urls_to_supabase(discovered_urls)
                update_last_crawled(rule['rule_id'])

                total_urls_found += saved_count
                results_per_rule.append({
                    "rule_id": rule['rule_id'],
                    "country": rule['country_name'],
                    "target_group": rule['target_group'],
                    "urls_found": saved_count,
                    "success": True
                })

            except Exception as e:
                logger.error(f"❌ Error processing rule {rule['rule_id']}: {str(e)}")
                results_per_rule.append({
                    "rule_id": rule['rule_id'],
                    "country": rule['country_name'],
                    "target_group": rule.get('target_group', 'unknown'),
                    "urls_found": 0,
                    "success": False,
                    "error": str(e)
                })

        logger.info(f"\n{'=' * 80}")
        logger.info(f"✅ DISCOVERY COMPLETED – Total URLs: {total_urls_found}")
        logger.info(f"{'=' * 80}")

        return DiscoveryResponse(
            success=True,
            total_rules_processed=len(rules),
            total_urls_found=total_urls_found,
            successful_rules=sum(1 for r in results_per_rule if r['success']),
            failed_rules=sum(1 for r in results_per_rule if not r['success']),
            results_per_rule=results_per_rule
        )

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# STARTUP
# =============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting Visa Scraper Discovery API v2.0.0 (Optimized)...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"⚡ Concurrent limit: {CONCURRENT_LIMIT}")
    logger.info(f"🔄 Retry: {MAX_RETRIES}x mit Delays {RETRY_DELAYS}s")
    logger.info("✅ API is ready!")
    logger.info("📍 Endpoints: /, /health, /discover, /discover-direct, /fetch-markdown, /fetch-markdown-batch, /fetch-apis")
    logger.info("⚠️  GROUP B: FINANZEN excluded from discovery – use /fetch-apis")


@app.on_event("shutdown")
async def shutdown_event():
    """httpx Client sauber schließen beim Shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        logger.info("🔒 httpx Client geschlossen")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
