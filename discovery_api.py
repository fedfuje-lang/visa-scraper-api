"""
URL Discovery API for Visa Scraper
FastAPI Service deployed on Railway.app
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

from fetch_markdown import router as fetch_markdown_router
from fetch_apis import router as fetch_apis_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Visa Scraper Discovery API",
    description="URL Discovery Service for Visa Immigration Data Scraping",
    version="2.7.1"
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
# GROUP B wird von fetch_apis gehandelt
# =============================================================================

EXCLUDED_FROM_DISCOVERY = ["GROUP B: FINANZEN"]

# =============================================================================
# CRAWLING CONFIG
# =============================================================================

# v2.7.0: Reduziert von 10 → 5, da mehrere Rules parallel laufen
# Bei MAX_PARALLEL_RULES=3 → max ~15 gleichzeitige Requests gesamt
CONCURRENT_LIMIT = 5

# v2.7.0: Maximale Anzahl parallel laufender Rules (CX33: 4 vCPUs, 8GB RAM)
MAX_PARALLEL_RULES = 3

MAX_RETRIES = 2
RETRY_DELAYS = [1, 3]

# v2.7.1: Nach dieser Anzahl gescheiterter URLs wird die Domain übersprungen
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
# FALLBACK KEYWORDS
# v2.3.1: GROUP F: BÜROKRATIE → GROUP F: BUEROKRATIE
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
    "GROUP F: BUEROKRATIE": [
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
# KEYWORD CACHE + load_keywords()
# =============================================================================

_keywords_cache: Dict[Tuple[str, str], Dict] = {}


async def load_keywords(country_iso: str, target_group: str) -> Optional[Dict]:
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
            logger.warning(f"⚠️ Keine Keywords für ({country_iso}, {target_group}) — Fallback aktiv")
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

        logger.info(f"📚 Keywords geladen ({country_iso}, {target_group}): {len(positive)} positiv, {len(negative)} negativ")
        return result

    except Exception as e:
        logger.warning(f"⚠️ config_keywords Abfrage fehlgeschlagen ({country_iso}, {target_group}): {e} — Fallback aktiv")
        return None


def clear_keywords_cache():
    global _keywords_cache
    _keywords_cache = {}


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
    """Extrahiert die Basis-Domain aus einer URL. z.B. 'canada.ca'"""
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


# =============================================================================
# SCORE URL
# =============================================================================

def score_url(url: str, text: str, target_group: str,
              keywords_config: Optional[Dict] = None) -> int:
    score = 0
    url_lower = url.lower()
    text_lower = text.lower()
    path_lower = urlparse(url).path.lower()

    general_matches = sum(1 for kw in GENERAL_KEYWORDS if kw in url_lower or kw in text_lower)
    score += min(general_matches, 2)

    if keywords_config is not None:
        for entry in keywords_config["positive"]:
            kw         = entry["keyword"]
            weight     = entry["weight"]
            match_type = entry["match_type"]

            if match_type in ("url", "both"):
                if kw in path_lower:
                    score += weight * 2
                elif kw in url_lower:
                    score += weight

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
                score -= weight

    else:
        if target_group in GROUP_KEYWORDS:
            group_kws = GROUP_KEYWORDS[target_group]

            path_matches = sum(1 for kw in group_kws if kw in path_lower)
            score += min(path_matches * 3, 6)

            url_matches = sum(1 for kw in group_kws if kw in url_lower and kw not in path_lower)
            score += min(url_matches, 2)

            text_matches = sum(1 for kw in group_kws if kw in text_lower)
            score += min(text_matches, 4)

    if len(text) > 3000:
        score += 2
    elif len(text) > 1500:
        score += 1
    if len(text) < 200:
        score = max(1, score - 2)

    return max(1, min(score, 10))


def extract_topics(text: str, url: str, target_group: str) -> List[str]:
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
        "GROUP F: BUEROKRATIE": {
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
# SITEMAP PARSER
# =============================================================================

async def fetch_sitemap_urls(base_url: str) -> List[str]:
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    urls = []

    try:
        client = await get_http_client()
        r = await client.get(sitemap_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/2.0)"})

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
                    sub_r = await client.get(sub_url, headers={"User-Agent": "Mozilla/5.0 (compatible; VisaScraper/2.0)"})
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


async def fetch_html_fast(url: str, domain_fails: Dict[str, int]) -> Optional[str]:
    """
    Fetcht HTML einer URL mit Retry-Logik und Domain Block Detection.

    v2.7.1: domain_fails Zähler wird nur beim letzten fehlgeschlagenen
    Versuch erhöht — nicht bei jedem einzelnen Retry. So entspricht
    1 gescheiterte URL = 1 Fehler für die Domain.
    """
    domain = get_domain(url)

    # Domain bereits geblockt → sofort skippen
    if domain_fails.get(domain, 0) >= DOMAIN_FAIL_THRESHOLD:
        logger.info(f"⛔ Domain geblockt, skip: {domain} ({url})")
        return None

    client = await get_http_client()

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
            elif r.status_code in (403, 429):
                # Klares Blocksignal → kein Retry, sofort zählen
                domain_fails[domain] = domain_fails.get(domain, 0) + 1
                logger.warning(f"⚠️ Domain Fehler {domain_fails[domain]}/{DOMAIN_FAIL_THRESHOLD}: {domain} (Status {r.status_code})")
                if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                    logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                return None
            elif r.status_code in (503, 502):
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.info(f"🔄 Retry {attempt + 1}/{MAX_RETRIES} für {url} (Status {r.status_code})")
                    await asyncio.sleep(delay)
                    continue
                else:
                    # Letzter Versuch gescheitert → jetzt zählen
                    domain_fails[domain] = domain_fails.get(domain, 0) + 1
                    logger.warning(f"⚠️ Domain Fehler {domain_fails[domain]}/{DOMAIN_FAIL_THRESHOLD}: {domain} (Status {r.status_code})")
                    if domain_fails[domain] >= DOMAIN_FAIL_THRESHOLD:
                        logger.warning(f"⛔ Domain geblockt nach {DOMAIN_FAIL_THRESHOLD} Fehlern: {domain}")
                    return None
            else:
                logger.warning(f"⚠️ httpx Status {r.status_code} für {url}")
                return None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                logger.info(f"🔄 Retry {attempt + 1}/{MAX_RETRIES} für {url} ({type(e).__name__})")
                await asyncio.sleep(delay)
                continue
            else:
                # Letzter Versuch gescheitert → jetzt zählen
                domain_fails[domain] = domain_fails.get(domain, 0) + 1
                logger.warning(f"⚠️ Domain Fehler {domain_fails[domain]}/{DOMAIN_FAIL_THRESHOLD}: {domain} ({type(e).__name__})")
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
    country_iso  = rule['country_iso']
    target_group = rule['target_group']

    ext = tldextract.extract(start_url)
    base_domain = f"{ext.domain}.{ext.suffix}"

    visited = set()
    discovered_urls = []
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    playwright_instance = None
    playwright_browser = None

    # v2.7.1: Fehlerzähler pro Domain — frisch pro Rule
    domain_fails: Dict[str, int] = {}

    logger.info(f"🚀 Starting discovery for: {rule['country_name']} ({rule['rule_id']})")
    logger.info(f"📊 Max URLs: {max_pages}, Max Depth: {max_depth}")

    keywords_config = await load_keywords(country_iso, target_group)

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
            to_visit.append((normalized_sm, 0))
            to_visit_set.add(normalized_sm)

    normalized_start = normalize_url(start_url)
    if normalized_start not in to_visit_set:
        to_visit.appendleft((normalized_start, 0))
        to_visit_set.add(normalized_start)

    async def process_page(url: str, depth: int) -> tuple:
        nonlocal playwright_instance, playwright_browser

        async with semaphore:
            url_lower = url.lower()
            is_pdf = url_lower.endswith(".pdf") or ".pdf?" in url_lower

            if is_pdf:
                return url, depth, {
                    "url": url,
                    "page_title": url.split("/")[-1][:500],
                    "relevance_score": 5,
                    "topics": [],
                    "discovered_depth": depth,
                    "text_length": 0,
                    "rule_id": rule['rule_id'],
                    "country_code": rule['country_iso'],
                    "country_name": rule['country_name'],
                    "target_group": rule['target_group']
                }, None, [], []

            # v2.7.1: domain_fails weitergeben
            html = await fetch_html_fast(url, domain_fails)

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
                return url, depth, None, None, [], []

            soup = BeautifulSoup(html, "html.parser")
            text = extract_main_content(soup)

            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True) if title_tag else ""

            relevance = score_url(url, text, target_group, keywords_config)
            topics = extract_topics(text, url, target_group)

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

        logger.info(f"🔎 [{rule['rule_id']}] Batch: {len(batch)} Seiten (gesamt: {len(visited)}/{max_pages})")

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
            "url": url,
            "page_title": url_data["page_title"],
            "relevance_score": url_data["relevance_score"],
            "topics": url_data["topics"],
            "discovered_depth": url_data["discovered_depth"],
            "rule_id": url_data["rule_id"],
            "country_code": url_data["country_code"],
            "country_name": url_data["country_name"],
            "target_group": url_data["target_group"],
            "status": "discovered"
        })

    if duplicates_removed > 0:
        logger.info(f"🧹 Removed {duplicates_removed} duplicate URLs from batch")

    # ==========================================================================
    # v2.6.0 FIX: Chunked URLs vor dem upsert rausfiltern
    # URLs mit status='chunked' wurden bereits von WF2 verarbeitet und dürfen
    # nicht überschrieben werden — nur neue oder discovered/pending URLs updaten
    # ==========================================================================
    try:
        all_urls = [d["url"] for d in insert_data]
        existing = supabase.table("discovered_urls").select("url, status").in_("url", all_urls).execute()
        chunked_urls = {row["url"] for row in existing.data if row["status"] == "chunked"}

        if chunked_urls:
            logger.info(f"🔒 Skipping {len(chunked_urls)} already chunked URLs — nicht überschreiben")
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
        "version": "2.7.1",
        "status": "running",
        "changes_v2.7.1": "Domain Block Detection: Nach DOMAIN_FAIL_THRESHOLD gescheiterten URLs wird die Domain übersprungen. Zähler erhöht sich nur beim letzten fehlgeschlagenen Versuch."
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "2.7.1",
        "supabase_connected": bool(SUPABASE_URL and SUPABASE_KEY)
    }


@app.post("/discover-direct", response_model=DirectDiscoveryResponse)
async def discover_direct(request: DirectDiscoveryRequest):
    if any(excluded in request.target_group for excluded in EXCLUDED_FROM_DISCOVERY):
        return DirectDiscoveryResponse(success=False, total_urls_found=0, urls=[])

    clear_keywords_cache()

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


@app.post("/discover", response_model=DiscoveryResponse)
async def run_discovery(request: DiscoveryRequest):
    clear_keywords_cache()

    logger.info("=" * 80)
    logger.info(f"🚀 DISCOVERY API v2.7.1 — PARALLEL MODE (max {MAX_PARALLEL_RULES} Rules gleichzeitig)")
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
            return DiscoveryResponse(
                success=False, total_rules_processed=0, total_urls_found=0,
                successful_rules=0, failed_rules=0, results_per_rule=[]
            )

        rules = [r for r in all_rules if not any(excluded in r.get("target_group", "") for excluded in EXCLUDED_FROM_DISCOVERY)]
        skipped = len(all_rules) - len(rules)

        if skipped > 0:
            logger.info(f"⏭️ Skipped {skipped} GROUP B rules")

        logger.info(f"📋 {len(rules)} Rules werden verarbeitet (max {MAX_PARALLEL_RULES} parallel, {CONCURRENT_LIMIT} URLs/Rule)")

        rule_semaphore = asyncio.Semaphore(MAX_PARALLEL_RULES)
        results_per_rule = []
        results_lock = asyncio.Lock()
        total_urls_found = 0

        async def process_rule(rule: Dict, index: int) -> Dict:
            async with rule_semaphore:
                logger.info(f"▶️  Rule {index}/{len(rules)}: {rule['rule_id']} – {rule['country_name']} / {rule['target_group']}")
                try:
                    if request.max_urls:
                        rule['max_urls'] = request.max_urls

                    discovered_urls = await discover_urls(rule)
                    saved_count = save_urls_to_supabase(discovered_urls)
                    update_last_crawled(rule['rule_id'])

                    logger.info(f"✅ Rule {rule['rule_id']} fertig: {saved_count} URLs gespeichert")
                    return {
                        "rule_id": rule['rule_id'], "country": rule['country_name'],
                        "target_group": rule['target_group'], "urls_found": saved_count, "success": True
                    }

                except Exception as e:
                    logger.error(f"❌ Fehler bei Rule {rule['rule_id']}: {str(e)}")
                    return {
                        "rule_id": rule['rule_id'], "country": rule['country_name'],
                        "target_group": rule.get('target_group', 'unknown'),
                        "urls_found": 0, "success": False, "error": str(e)
                    }

        tasks = [process_rule(rule, i) for i, rule in enumerate(rules, 1)]
        results_per_rule = await asyncio.gather(*tasks)
        total_urls_found = sum(r['urls_found'] for r in results_per_rule)

        return DiscoveryResponse(
            success=True,
            total_rules_processed=len(rules),
            total_urls_found=total_urls_found,
            successful_rules=sum(1 for r in results_per_rule if r['success']),
            failed_rules=sum(1 for r in results_per_rule if not r['success']),
            results_per_rule=list(results_per_rule)
        )

    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting Visa Scraper Discovery API v2.7.1...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info(f"⚡ Concurrent limit per rule: {CONCURRENT_LIMIT}")
    logger.info(f"⚡ Max parallel rules: {MAX_PARALLEL_RULES}")
    logger.info(f"⚡ Domain fail threshold: {DOMAIN_FAIL_THRESHOLD}")
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
