"""
Fetch Markdown API - Jina Replacement
FastAPI Endpoint deployed on Railway.app
Converts URLs to clean Markdown for n8n WF2 (Content Extraction)

Response format: { "data": "markdown..." }
Compatible with existing Clean Markdown Code Node in n8n WF2

v2.0.0 - Angepasst an Discovery API v2.0:
  - httpx als Standard, Playwright nur Fallback (wie Discovery)
  - Globaler httpx Client (Connection Pooling)
  - PDF-Extraktion mit pymupdf (PDFs werden nicht mehr übersprungen)
  - Retry-Logik bei Timeouts/Server-Fehlern

v1.3.0 - Added /fetch-markdown-batch endpoint for parallel processing
"""

from fastapi import APIRouter, HTTPException, Query
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import httpx
import logging
import re
import tempfile
import os

logger = logging.getLogger(__name__)

# =============================================================================
# ROUTER (wird in discovery_api.py eingebunden)
# =============================================================================

router = APIRouter()

# =============================================================================
# CONFIG
# =============================================================================

MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]

# =============================================================================
# GLOBALER HTTPX CLIENT (Connection Pooling – wie im Discovery Script)
# =============================================================================

_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Gibt den globalen httpx Client zurück."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=15,
                max_keepalive_connections=10,
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


# =============================================================================
# PDF DETECTION + EXTRACTION
# =============================================================================

def is_pdf_url(url: str) -> bool:
    """Erkennt ob eine URL auf eine PDF zeigt"""
    url_lower = url.lower().strip()
    if url_lower.endswith(".pdf"):
        return True
    if ".pdf?" in url_lower or ".pdf#" in url_lower:
        return True
    return False


async def extract_pdf_text(url: str) -> Optional[str]:
    """Lädt PDF herunter und extrahiert Text mit pymupdf.
    Gibt None zurück wenn pymupdf nicht installiert oder PDF nicht lesbar.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        logger.warning("⚠️ pymupdf nicht installiert – PDF-Extraktion nicht verfügbar")
        return None

    try:
        client = await get_http_client()
        r = await client.get(url)

        if r.status_code != 200:
            logger.warning(f"⚠️ PDF Download fehlgeschlagen: Status {r.status_code}")
            return None

        # PDF in temporäre Datei schreiben
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(r.content)
            tmp_path = tmp.name

        try:
            doc = fitz.open(tmp_path)
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()

            full_text = "\n\n".join(text_parts).strip()

            if len(full_text) < 50:
                logger.info(f"📄 PDF hat kaum Text (vermutlich gescannt): {url}")
                return None

            return full_text

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"❌ PDF-Extraktion fehlgeschlagen für {url}: {str(e)}")
        return None


def pdf_text_to_markdown(text: str, url: str) -> str:
    """Konvertiert extrahierten PDF-Text in einfaches Markdown."""
    lines = text.split("\n")
    md_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            md_lines.append("")
            continue

        # Kurze Zeilen in Großbuchstaben → vermutlich Überschriften
        if line.isupper() and len(line) < 100:
            md_lines.append(f"\n## {line.title()}\n")
        else:
            md_lines.append(line)

    markdown = "\n".join(md_lines)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


# =============================================================================
# HTML FETCHING: HTTPX (schnell) + PLAYWRIGHT FALLBACK
# =============================================================================

async def fetch_html_fast(url: str) -> Optional[str]:
    """Schneller HTML-Fetch mit httpx + Retry-Logik."""
    client = await get_http_client()

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)

            # Content-Type prüfen
            content_type = r.headers.get("content-type", "")
            if "application/pdf" in content_type:
                return None  # PDF wird separat behandelt

            if r.status_code == 200:
                return r.text
            elif r.status_code in (429, 503, 502):
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.info(f"🔄 Retry {attempt + 1}/{MAX_RETRIES} für {url} (Status {r.status_code})")
                    await asyncio.sleep(delay)
                    continue
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
                logger.warning(f"⚠️ httpx Fehler nach {MAX_RETRIES} Versuchen: {str(e)}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ httpx Fehler für {url}: {str(e)}")
            return None

    return None


async def fetch_html_playwright(url: str) -> Optional[str]:
    """Fallback: Playwright für JavaScript-lastige Seiten."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=VizDisplayCompositor'
                ]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
            )

            # Bilder/Tracking blockieren (wie vorher)
            await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}",
                               lambda route: route.abort())
            await context.route("**/{analytics,tracking,ads,doubleclick}**",
                               lambda route: route.abort())

            page = await context.new_page()
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            html = await page.content()
            await browser.close()

            return html

    except Exception as e:
        logger.error(f"❌ Playwright Fehler für {url}: {str(e)}")
        return None


def needs_javascript(html: str) -> bool:
    """Erkennt ob die Seite JavaScript braucht um Content zu laden."""
    soup = BeautifulSoup(html, "html.parser")

    # Hauptinhalt extrahieren
    content = (
        soup.find("main") or
        soup.find("article") or
        soup.find(id=re.compile(r"content|main", re.I)) or
        soup.find(class_=re.compile(r"content|main|article", re.I))
    )
    text = content.get_text(" ", strip=True) if content else soup.get_text(" ", strip=True)

    # Wenig Text + viele Scripts → braucht JS
    script_count = len(soup.find_all("script"))
    if len(text) < 200 and script_count > 5:
        return True

    # SPA-Frameworks erkennen
    html_lower = html.lower()
    spa_indicators = ["__next", "__nuxt", "react-root", "ng-app", "v-app", 'id="app"']
    if len(text) < 200 and any(ind in html_lower for ind in spa_indicators):
        return True

    return False


# =============================================================================
# HTML → MARKDOWN CONVERSION (UNVERÄNDERT – n8n Kompatibilität)
# =============================================================================

def html_to_markdown(html: str, url: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "svg", "button",
                     "form", "input", "select", "textarea", "meta",
                     "link", "figure", "figcaption"]):
        tag.decompose()

    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")):
        tag.decompose()

    main_content = (
        soup.find("main") or
        soup.find("article") or
        soup.find(id=re.compile(r"content|main|body", re.I)) or
        soup.find(class_=re.compile(r"content|main|body|article", re.I)) or
        soup.find("body") or
        soup
    )

    lines = []
    _convert_element(main_content, lines)

    markdown = "\n".join(lines)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    return markdown.strip()


def _convert_element(element, lines: list, depth: int = 0):
    if isinstance(element, NavigableString):
        text = str(element).strip()
        if text:
            lines.append(text)
        return

    tag = element.name if element.name else ""

    if tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        level = int(tag[1])
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"\n{'#' * level} {text}\n")
        return

    if tag == "p":
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"\n{text}\n")
        return

    if tag == "table":
        table_md = _convert_table(element)
        if table_md:
            lines.append(f"\n{table_md}\n")
        return

    if tag in ["ul", "ol"]:
        lines.append("")
        for i, li in enumerate(element.find_all("li", recursive=False), 1):
            text = li.get_text(" ", strip=True)
            if text:
                prefix = f"{i}." if tag == "ol" else "-"
                lines.append(f"{prefix} {text}")
        lines.append("")
        return

    if tag == "a":
        text = element.get_text(" ", strip=True)
        href = element.get("href", "").strip()
        if text and href and href.startswith("http"):
            lines.append(f"[{text}]({href})")
        elif text:
            lines.append(text)
        return

    if tag in ["strong", "b"]:
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"**{text}**")
        return

    if tag in ["em", "i"]:
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"*{text}*")
        return

    if tag == "br":
        lines.append("\n")
        return

    if tag == "hr":
        lines.append("\n---\n")
        return

    for child in element.children:
        _convert_element(child, lines, depth + 1)


def _convert_table(table_element) -> str:
    all_rows = table_element.find_all("tr")

    if not all_rows:
        return ""

    markdown_rows = []
    separator_added = False

    for row_idx, row in enumerate(all_rows):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue

        cell_texts = [cell.get_text(" ", strip=True).replace("|", "\\|") for cell in cells]
        row_str = "| " + " | ".join(cell_texts) + " |"
        markdown_rows.append(row_str)

        if row_idx == 0 and not separator_added:
            separator = "| " + " | ".join(["---"] * len(cells)) + " |"
            markdown_rows.append(separator)
            separator_added = True

    return "\n".join(markdown_rows) if markdown_rows else ""


# =============================================================================
# QUALITY SCORING (UNVERÄNDERT – n8n Kompatibilität)
# =============================================================================

def calculate_quality_score(markdown: str) -> dict:
    score = 0
    details = {}

    word_count = len(markdown.split())
    details["word_count"] = word_count
    if word_count > 500:
        score += 3
    elif word_count > 200:
        score += 2
    elif word_count > 50:
        score += 1

    numbers = re.findall(r'\b\d+[\.,]?\d*\b', markdown)
    currency_pattern = re.findall(r'[$€£¥₹]\s*\d+|\d+\s*(?:USD|EUR|GBP|CHF|AUD|CAD)', markdown)
    details["numbers_found"] = len(numbers)
    details["currencies_found"] = len(currency_pattern)
    if len(numbers) > 10:
        score += 2
    elif len(numbers) > 3:
        score += 1
    if currency_pattern:
        score += 1

    table_count = markdown.count("| ---")
    details["tables_found"] = table_count
    if table_count > 0:
        score += 2

    heading_count = len(re.findall(r'^#{1,6} ', markdown, re.MULTILINE))
    details["headings_found"] = heading_count
    if heading_count > 3:
        score += 1

    link_count = len(re.findall(r'\[.+?\]\(.+?\)', markdown))
    details["links_found"] = link_count
    if link_count > 50:
        score = max(0, score - 1)

    final_score = min(score, 10)
    details["final_score"] = final_score
    details["quality_status"] = "useful" if final_score >= 5 else "low_quality"

    return details


# =============================================================================
# MAIN FETCH FUNCTION (NEU: httpx → Playwright Fallback → PDF Support)
# =============================================================================

async def fetch_and_convert(url: str) -> dict:
    """Holt eine URL und konvertiert sie zu Markdown.
    Reihenfolge:
      1. PDF? → PDF-Extraktion
      2. httpx (schnell)
      3. Braucht JS? → Playwright Fallback
    Response-Format bleibt identisch für n8n Kompatibilität.
    """

    # ─── PDF Handling ───
    if is_pdf_url(url):
        logger.info(f"📄 PDF erkannt, versuche Extraktion: {url}")
        pdf_text = await extract_pdf_text(url)

        if pdf_text:
            markdown = pdf_text_to_markdown(pdf_text, url)
            quality = calculate_quality_score(markdown)
            logger.info(f"✅ PDF extrahiert: {url} → {quality['word_count']} Wörter")
            return {
                "data": markdown,
                "url": url,
                "content_type": "pdf",
                "quality_score": quality["final_score"],
                "quality_details": quality,
                "success": True,
                "error": None
            }
        else:
            # pymupdf nicht installiert oder PDF nicht lesbar
            logger.warning(f"⚠️ PDF-Extraktion fehlgeschlagen: {url}")
            return {
                "data": f"[PDF-Dokument – Text-Extraktion fehlgeschlagen]\nURL: {url}",
                "url": url,
                "content_type": "pdf",
                "quality_score": 0,
                "quality_details": {"quality_status": "pdf_extraction_failed", "word_count": 0},
                "success": False,
                "error": "PDF text extraction failed (pymupdf may not be installed)"
            }

    # ─── HTML Handling: httpx zuerst ───
    logger.info(f"🌐 Fetching: {url}")
    html = await fetch_html_fast(url)

    # Content-Type Check: vielleicht ist es doch ein PDF (URL hatte kein .pdf)
    if html is None:
        # Prüfe ob es ein PDF via Content-Type war
        try:
            client = await get_http_client()
            head_r = await client.head(url)
            ct = head_r.headers.get("content-type", "")
            if "application/pdf" in ct:
                logger.info(f"📄 PDF via Content-Type erkannt: {url}")
                pdf_text = await extract_pdf_text(url)
                if pdf_text:
                    markdown = pdf_text_to_markdown(pdf_text, url)
                    quality = calculate_quality_score(markdown)
                    return {
                        "data": markdown,
                        "url": url,
                        "content_type": "pdf",
                        "quality_score": quality["final_score"],
                        "quality_details": quality,
                        "success": True,
                        "error": None
                    }
        except Exception:
            pass

    # Fallback: Playwright wenn httpx komplett fehlschlägt
    if html is None:
        logger.info(f"🔄 httpx fehlgeschlagen, versuche Playwright: {url}")
        html = await fetch_html_playwright(url)

    if html is None:
        return {
            "data": f"[Seite konnte nicht geladen werden]\nURL: {url}",
            "url": url,
            "content_type": "error",
            "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False,
            "error": "Both httpx and Playwright failed to fetch the page"
        }

    # ─── JS-Check: Playwright Fallback wenn nötig ───
    if needs_javascript(html):
        logger.info(f"🔄 JS-Seite erkannt, nutze Playwright: {url}")
        pw_html = await fetch_html_playwright(url)
        if pw_html:
            html = pw_html

    # ─── Markdown Konvertierung ───
    markdown = html_to_markdown(html, url)
    quality = calculate_quality_score(markdown)

    logger.info(f"✅ Fetched {url} → {quality['word_count']} words, score: {quality['final_score']}/10")

    return {
        "data": markdown,
        "url": url,
        "content_type": "html",
        "quality_score": quality["final_score"],
        "quality_details": quality,
        "success": True,
        "error": None
    }


# =============================================================================
# API ENDPOINTS (Response-Format UNVERÄNDERT für n8n Kompatibilität)
# =============================================================================

@router.get("/fetch-markdown")
async def fetch_markdown_endpoint(url: str = Query(..., description="URL to fetch and convert to Markdown")):
    """
    EINZELNER URL ENDPOINT (unverändert)
    GET /fetch-markdown?url=https://...
    """
    if not url:
        raise HTTPException(status_code=400, detail="url parameter is required")
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")

    try:
        result = await fetch_and_convert(url)
        return result
    except Exception as e:
        logger.error(f"❌ Error fetching {url}: {str(e)}")
        return {
            "data": f"[Fehler beim Laden der Seite]\nURL: {url}\nFehler: {str(e)}",
            "url": url,
            "content_type": "error",
            "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False,
            "error": str(e)
        }


# =============================================================================
# BATCH ENDPOINT (Response-Format UNVERÄNDERT für n8n Kompatibilität)
# =============================================================================

class BatchRequest(BaseModel):
    urls: List[str]

@router.post("/fetch-markdown-batch")
async def fetch_markdown_batch(request: BatchRequest):
    """
    BATCH ENDPOINT – bis zu 3 URLs parallel verarbeiten

    POST /fetch-markdown-batch
    Body: { "urls": ["https://url1", "https://url2", "https://url3"] }

    Response-Format identisch zu v1.3.0
    """

    if not request.urls:
        raise HTTPException(status_code=400, detail="urls list is required")

    # Max 3 URLs pro Batch
    urls = request.urls[:3]

    logger.info(f"🚀 Batch fetch started: {len(urls)} URLs")

    # Alle URLs parallel fetchen
    raw_results = await asyncio.gather(
        *[fetch_and_convert(url) for url in urls],
        return_exceptions=True
    )

    # Ergebnisse aufbereiten – Exceptions sauber behandeln
    results = []
    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            logger.error(f"❌ Batch item {i} failed: {str(result)}")
            results.append({
                "data": f"[Fehler beim Laden der Seite]\nURL: {urls[i]}\nFehler: {str(result)}",
                "url": urls[i],
                "content_type": "error",
                "quality_score": 0,
                "quality_details": {"quality_status": "fetch_error", "word_count": 0},
                "success": False,
                "error": str(result)
            })
        else:
            results.append(result)

    successful = sum(1 for r in results if r.get("success", False))
    failed = len(results) - successful

    logger.info(f"✅ Batch complete: {successful} successful, {failed} failed")

    return {
        "results": results,
        "total": len(results),
        "successful": successful,
        "failed": failed
    }
