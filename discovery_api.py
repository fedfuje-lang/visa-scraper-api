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

v2.1.0 - Multilingual Fix
  - is_relevant_path() aus Crawling-Filtern entfernt
  - Funktioniert jetzt für alle Sprachen/Länder ohne Keyword-Anpassung
  - Nur BLOCKED_PATH_PATTERNS blockt — WF1b/Gemini übernehmen Qualitätskontrolle

v2.2.0 - Dynamic Keywords
  - Keywords aus Supabase config_keywords Tabelle (ALL + länderspezifisch)
  - Cache-Key (country_iso, target_group) — kein Mix zwischen Gruppen
  - priority_weight (1-5) ersetzt fixe Gewichtung
  - is_negative dämpft Score subtraktiv (nicht blockend, min 1)
  - match_type ('url', 'content', 'both') steuert Suchbereich
  - GROUP_KEYWORDS bleibt als Fallback falls Supabase-Abfrage fehlschlägt

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
from typing import Optional, List, Dict, Tuple
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
    version="2.2.0"
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

# URL-Pfade die NICHT gecrawlt werden (spart unnötige Requests)
# Sprachunabhängig — diese Pfade sind in allen Ländern gleich unbrauchbar
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

# NOTE: is_relevant_path() wird nicht mehr als Filter verwendet (v2.1.0)
# Grund: englische Keywords blockierten nicht-englische URLs (DE, ES, FR, etc.)
# Qualitätskontrolle übernehmen WF1b (quality_score) und WF2 (Gemini)

# =============================================================================
# FALLBACK KEYWORDS — unverändert aus v2.1.0
# Nur aktiv wenn load_keywords() fehlschlägt (Supabase nicht erreichbar etc.)
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
# ÄNDERUNG 1/3: KEYWORD CACHE + load_keywords()
# Cache-Key: (country_iso, target_group) — verhindert Mix zwischen Gruppen
# =============================================================================

_keywords_cache: Dict[Tuple[str, str], Dict] = {}


async def load_keywords(country_iso: str, target_group: str) -> Optional[Dict]:
    """
    Lädt Keywords aus config_keywords für ein Land + Gruppe.
    Kombiniert 'ALL' (globale Basis) + länderspezifische Keywords.

    Cache-Key: (country_iso, target_group)
    → DE/GROUP A und DE/GROUP E werden getrennt gecacht — kein Keyword-Mix.
    → Innerhalb eines Discovery-Runs wird jede Kombination nur einmal geladen.

    Gibt None zurück wenn Supabase fehlschlägt → score_url() nutzt Fallback.
    """
    cache_key = (country_iso, target_group)

    if cache_key in _keywords_cache:
        return _keywords_cache[cache_key]

    try:
        response = supabase.table("config_keywords").select(
            "keyword, priority_weight, is_negative, match_type"
        ).eq("target_group", target_group).in_(
            "country_iso", ["ALL", country_iso]
        ).execute()

        if not response.data:
            logger.warning(
                f"⚠️ Keine Keywords in config_keywords für "
                f"({country_iso}, {target_group}) — Fallback aktiv"
            )
            return None

        positive = []
        negative = []

        for row in response.data:
            entry = {
                "keyword":    row["keyword"],
                "weight":     row["priority_weight"],
                "match_type": row["match_type"],
            }
            if row["is_negative"]:
                negative.append(entry)
            else:
                positive.append(entry)

        result = {"positive": positive, "negative": negative}
        _keywords_cache[cache_key] = result

        logger.info(
            f"📚 Keywords geladen ({country_iso}, {target_group}): "
            f"{len(positive)} positiv, {len(negative)} negativ"
        )
        return result

    except Exception as e:
        logger.warning(
            f"⚠️ config_keywords Abfrage fehlgeschlagen "
            f"({country_iso}, {target_group}): {e} — Fallback aktiv"
        )
        return None


def clear_keywords_cache():
    """Cache leeren — einmal pro /discover Aufruf."""
    global _keywords_cache
    _keywords_cache = {}


# =============================================================================
# HELPER FUNCTIONS — unverändert aus v2.1.0
# =============================================================================

def normalize_url(url: str) -> str:
    """Normalisiert URL: entfernt Fragment UND unnötige Query-Parameter."""
    parsed = urlparse(url)

    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {
            k: v for k, v in params.items()
            if k.lower() not in IGNORED_QUERY_PARAMS
        }
        clean_query = urlencode(filtered, doseq=True) if filtered else ""
    else:
        clean_query = ""

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


def extract_main_content(soup: BeautifulSoup) -> str:
    """Extrahiert nur den Hauptinhalt der Seite (nicht Navigation/Footer/Sidebar)."""
    content = soup.select_one(
        "main, "
        "article, "
        "[role='main'], "
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


# =============================================================================
# ÄNDERUNG 2/3: score_url() — nimmt keywords_config Parameter
# Fallback auf GROUP_KEYWORDS wenn keywords_config=None
# =============================================================================

def score_url(url: str, text: str, target_group: str,
              keywords_config: Optional[Dict] = None) -> int:
    """
    Relevanz-Score berechnen.

    v2.2.0: Nutzt keywords_config aus config_keywords wenn übergeben.
    - priority_weight (1-5): Punkte pro Treffer
    - URL-Pfad Treffer: weight * 2 (wie bisher stärker gewichtet)
    - URL-Domain Treffer: weight * 1
    - Content Treffer: weight * 1
    - is_negative: subtrahiert weight (dämpft, blockiert nicht)
    - match_type 'url': nur URL prüfen
    - match_type 'content': nur Text prüfen
    - match_type 'both': beide prüfen

    Fallback: GROUP_KEYWORDS mit v2.1.0 Logik wenn keywords_config=None.
    Score bleibt immer zwischen 1 und 10.
    """
    score = 0
    url_lower = url.lower()
    text_lower = text.lower()
    path_lower = urlparse(url).path.lower()

    # General Keywords — unverändert, sprachunabhängige Basis
    general_matches = sum(1 for kw in GENERAL_KEYWORDS if kw in url_lower or kw in text_lower)
    score += min(general_matches, 2)

    if keywords_config is not None:
        # ── Neue Logik v2.2.0: config_keywords ───────────────────
        for entry in keywords_config["positive"]:
            kw         = entry["keyword"]
            weight     = entry["weight"]
            match_type = entry["match_type"]

            if match_type in ("url", "both"):
                if kw in path_lower:
                    score += weight * 2      # Pfad-Treffer: doppelt gewichtet
                elif kw in url_lower:
                    score += weight          # Domain/Query-Treffer: einfach

            if match_type in ("content", "both"):
                if kw in text_lower:
                    score += weight

        for entry in keywords_config["negative"]:
            kw         = entry["keyword"]
            weight     = entry["weight"]
            match_type = entry["match_type"]

            hit = False
            if match_type in ("url", "both") and kw in url_lower:
                hit = True
            if match_type in ("content", "both") and kw in text_lower:
                hit = True
            if hit:
                score -= weight              # Dämpfen, nicht blocken

    else:
        # ── Fallback: GROUP_KEYWORDS v2.1.0 Logik ────────────────
        if target_group in GROUP_KEYWORDS:
            group_kws = GROUP_KEYWORDS[target_group]

            path_matches = sum(1 for kw in group_kws if kw in path_lower)
            score += min(path_matches * 3, 6)

            url_matches = sum(1 for kw in group_kws if kw in url_lower and kw not in path_lower)
            score += min(url_matches, 2)

            text_matches = sum(1 for kw in group_kws if kw in text_lower)
            score += min(text_matches, 4)

    # Text-Länge Bonus — unverändert
    if len(text) > 3000:
        score += 2
    elif len(text) > 1500:
        score += 1
    if len(text) < 200:
        score = max(1, score - 2)

    return max(1, min(score, 10))


def extract_topics(text: str, url: str, target_group: str) -> List[str]:
    """Extrahiert Topics aus Text und URL."""
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
# SITEMAP PARSER — unverändert aus v2.1.0
# =============================================================================

async def fetch_sitemap_urls(base_url: str) -> List[str]:
    """Versucht /sitemap.xml zu laden und gibt alle URLs zurück."""
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

        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        sitemaps = root.findall(f".//{ns}sitemap/{ns}loc")
        if sitemaps:
            logger.info(f"📋 Sitemap-Index gefunden mit {len(sitemaps)} Sub-Sitemaps")
            for sitemap_loc in sitemaps[:5]:
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
            for loc in root.findall(f".//{ns}loc"):
                if loc.text:
                    urls.append(loc.text.strip())

        logger.info(f"📋 Sitemap: {len(urls)} URLs gefunden")

    except Exception as e:
        logger.info(f"📋 Sitemap nicht verfügbar: {str(e)}")

    return urls


# =============================================================================
# GLOBALER HTTPX CLIENT — unverändert aus v2.1.0
# =============================================================================

_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Gibt den globalen httpx Client zurück."""
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
    """Schneller HTML-Fetch mit httpx + Retry-Logik."""
    client = await get_http_client()

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            elif r.status_code in (429, 503, 502):
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

    script_count = len(soup.find_all("script"))
    if len(text) < 200 and script_count > 5:
        return True

    html_lower = html.lower()
    spa_indicators = ["__next", "__nuxt", "react-root", "ng-app", "v-app", "id=\"app\""]
    if len(text) < 200 and any(ind in html_lower for ind in spa_indicators):
        return True

    return False


# =============================================================================
# MAIN DISCOVERY FUNCTION
# ÄNDERUNG 3/3: load_keywords() einmal pro Rule aufrufen, an score_url() übergeben
# =============================================================================

async def discover_urls(rule: Dict) -> List[Dict]:
    start_url    = rule['target_url']
    max_pages    = rule['max_urls']
    max_depth    = rule['max_depth']
    country_iso  = rule['country_iso']
    target_group = rule['target_group']

    ext = tldextract.extract(start_url)
    base_domain = f"{ext.domain}.{ext.suffix}"

    visited = set()
    discovered_urls = []

    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

    playwright_instance = None
    playwright_browser = None

    logger.info(f"🚀 Starting discovery for: {rule['country_name']} ({rule['rule_id']})")
    logger.info(f"📊 Max URLs: {max_pages}, Max Depth: {max_depth}")

    # v2.2.0: Keywords einmal pro Rule laden (gecacht nach country_iso + target_group)
    keywords_config = await load_keywords(country_iso, target_group)

    # ─── SCHRITT 1: Sitemap checken ───
    sitemap_urls = await fetch_sitemap_urls(start_url)

    if sitemap_urls:
        sitemap_filtered = [
            u for u in sitemap_urls
            if is_internal(u, base_domain)
            and not is_blocked_path(u)
        ]
        logger.info(f"📋 Sitemap: {len(sitemap_filtered)} relevante URLs (von {len(sitemap_urls)} total)")
    else:
        sitemap_filtered = []

    # ─── SCHRITT 2: Crawl-Queue aufbauen ───
    to_visit = deque()
    to_visit_set = set()

    for sm_url in sitemap_filtered[:max_pages]:
        normalized_sm = normalize_url(sm_url)
        if normalized_sm not in to_visit_set:
            to_visit.append((normalized_sm, 0))
            to_visit_set.add(normalized_sm)

    normalized_start = normalize_url(start_url)
    if normalized_start not in to_visit_set:
        to_visit.appendleft((normalized_start, 0))
        to_visit_set.add(normalized_start)

    # ─── SCHRITT 3: Paralleles Crawling ───

    async def process_page(url: str, depth: int) -> tuple:
        """Einzelne Seite laden und verarbeiten."""
        nonlocal playwright_instance, playwright_browser

        async with semaphore:
            html = await fetch_html_fast(url)

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

            # v2.2.0: keywords_config übergeben
            relevance = score_url(url, text, target_group, keywords_config)
            topics = extract_topics(text, url, target_group)

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

                    # Filter: intern + nicht blockiert
                    # is_relevant_path() NICHT mehr verwendet (v2.1.0 — multilingual fix)
                    if (is_internal(normalized_full, base_domain)
                            and not is_blocked_path(normalized_full)):
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

    # Crawling Loop: Batch-weise parallel — unverändert
    while to_visit and len(visited) < max_pages:
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

        tasks = [process_page(url, depth) for url, depth in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"⚠️ Batch-Fehler: {str(result)}")
                continue

            url, depth, url_data, text, child_links, topics = result

            if url_data:
                discovered_urls.append(url_data)

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
# SUPABASE FUNCTIONS — unverändert aus v2.1.0
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
# API ENDPOINTS — unverändert aus v2.1.0 (n8n Kompatibilität)
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
        "version": "2.2.0",
        "status": "running",
        "improvements": [
            "httpx + Playwright fallback (10x faster)",
            "Parallel crawling (10 concurrent)",
            "Multilingual URL filtering (v2.1.0 — no more keyword blocking)",
            "Sitemap parser (finds URLs instantly)",
            "Smart content extraction (main/article only)",
            "Better URL normalization (no utm/tracking params)",
            "Dynamic keywords from Supabase config_keywords (v2.2.0)",
            "priority_weight scoring + negative keyword dampening (v2.2.0)",
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
        "version": "2.2.0",
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

    clear_keywords_cache()

    logger.info("=" * 80)
    logger.info("🚀 DIRECT DISCOVERY STARTED (v2.2.0 – Dynamic Keywords)")
    logger.info(f"📋 Country: {request.country_name} ({request.country_code})")
    logger.info(f"📂 Group: {request.target_group}")
    logger.info(f"🔗 Start URLs: {len(request.start_urls)}")
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

    clear_keywords_cache()

    logger.info("=" * 80)
    logger.info("🚀 DISCOVERY API STARTED (v2.2.0 – Dynamic Keywords)")
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
                    "rule_id":      rule['rule_id'],
                    "country":      rule['country_name'],
                    "target_group": rule['target_group'],
                    "urls_found":   saved_count,
                    "success":      True
                })

            except Exception as e:
                logger.error(f"❌ Error processing rule {rule['rule_id']}: {str(e)}")
                results_per_rule.append({
                    "rule_id":      rule['rule_id'],
                    "country":      rule['country_name'],
                    "target_group": rule.get('target_group', 'unknown'),
                    "urls_found":   0,
                    "success":      False,
                    "error":        str(e)
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
# STARTUP / SHUTDOWN — unverändert aus v2.1.0
# =============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting Visa Scraper Discovery API v2.2.0 (Dynamic Keywords)...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"⚡ Concurrent limit: {CONCURRENT_LIMIT}")
    logger.info(f"🔄 Retry: {MAX_RETRIES}x mit Delays {RETRY_DELAYS}s")
    logger.info("✅ API is ready!")
    logger.info("📍 Endpoints: /, /health, /discover, /discover-direct, /fetch-markdown, /fetch-markdown-batch, /fetch-apis")
    logger.info("⚠️  GROUP B: FINANZEN excluded from discovery – use /fetch-apis")
    logger.info("🌍 v2.1.0: Multilingual fix – is_relevant_path() removed from crawl filters")
    logger.info("📚 v2.2.0: Dynamic keywords – config_keywords Tabelle, priority_weight, negative dampening")


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
