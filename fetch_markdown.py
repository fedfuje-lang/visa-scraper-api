"""
Fetch Markdown API - Jina Replacement
FastAPI Endpoint deployed on Railway.app
Converts URLs to clean Markdown for n8n WF2 (Content Extraction)

v2.3.0 - Drei Qualitäts-Fixes:
  1. Relative Links Fix: urljoin für alle Links (keine PDF-Formulare mehr verloren)
  2. Colspan Fix: Verbundene Tabellenzellen werden korrekt aufgefüllt
  3. Trafilatura Hybrid: Trafilatura für saubere Texte, BS4 als sicheres Fallback

v2.2.0 - Encoding Fix: UTF-8 first, Fallback auf deklariertes Encoding
v2.1.0 - Batch Limit 15, Semaphore(8)
v2.0.0 - httpx Standard, Playwright Fallback, PDF Support
v2.4.0 - Null-Byte Fix: clean_text() entfernt Null-Bytes und Steuerzeichen
         aus allen Markdown-Outputs bevor sie zurückgegeben werden.
         Verhindert Supabase-Fehler "null character not permitted".
v2.4.1 - Null-Byte Fix erweitert: clean_text() jetzt auch in allen Fehler-Returns
         (html is None, Exception Handler, Batch Exception).
"""

from fastapi import APIRouter, HTTPException, Query
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString
from pydantic import BaseModel
from typing import List, Optional
from urllib.parse import urljoin
import asyncio
import httpx
import logging
import re
import tempfile
import os
import trafilatura

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]

_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=30,
                max_keepalive_connections=20,
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
# v2.4.0: NULL-BYTE BEREINIGUNG
# Entfernt Null-Bytes (\x00) und andere unerlaubte Steuerzeichen aus Strings.
# Verhindert Supabase-Fehler: "null character not permitted" (PostgreSQL Code 54000)
# Erlaubt: \t (Tab), \n (Newline), \r (Carriage Return) — alles andere unter \x20 raus.
# v2.4.1: Wird jetzt auch in allen Fehler-Returns verwendet.
# =============================================================================

def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', text)


# =============================================================================
# PDF DETECTION + EXTRACTION
# =============================================================================

def is_pdf_url(url: str) -> bool:
    url_lower = url.lower().strip()
    if url_lower.endswith(".pdf"):
        return True
    if ".pdf?" in url_lower or ".pdf#" in url_lower:
        return True
    return False


async def extract_pdf_text(url: str) -> Optional[str]:
    try:
        import fitz
    except ImportError:
        logger.warning("⚠️ pymupdf nicht installiert")
        return None

    try:
        client = await get_http_client()
        r = await client.get(url)
        if r.status_code != 200:
            return None

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(r.content)
            tmp_path = tmp.name

        try:
            doc = fitz.open(tmp_path)
            text_parts = [page.get_text() for page in doc]
            doc.close()
            full_text = "\n\n".join(text_parts).strip()
            return full_text if len(full_text) >= 50 else None
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"❌ PDF-Extraktion fehlgeschlagen für {url}: {str(e)}")
        return None


def pdf_text_to_markdown(text: str, url: str) -> str:
    lines = text.split("\n")
    md_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            md_lines.append("")
        elif line.isupper() and len(line) < 100:
            md_lines.append(f"\n## {line.title()}\n")
        else:
            md_lines.append(line)
    markdown = "\n".join(md_lines)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


# =============================================================================
# ENCODING FIX (v2.2.0)
# =============================================================================

def decode_response(r: httpx.Response) -> str:
    """UTF-8 first — viele Seiten deklarieren latin-1 aber senden UTF-8."""
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        pass

    detected_encoding = r.encoding or "latin-1"
    try:
        return r.content.decode(detected_encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        pass

    return r.content.decode("latin-1", errors="replace")


# =============================================================================
# HTML FETCHING
# =============================================================================

async def fetch_html_fast(url: str) -> Optional[str]:
    client = await get_http_client()

    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(url)

            content_type = r.headers.get("content-type", "")
            if "application/pdf" in content_type:
                return None

            if r.status_code == 200:
                return decode_response(r)
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
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9,de;q=0.8"}
            )

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
    soup = BeautifulSoup(html, "html.parser")

    content = (
        soup.find("main") or
        soup.find("article") or
        soup.find(id=re.compile(r"content|main", re.I)) or
        soup.find(class_=re.compile(r"content|main|article", re.I))
    )
    text = content.get_text(" ", strip=True) if content else soup.get_text(" ", strip=True)

    script_count = len(soup.find_all("script"))
    if len(text) < 200 and script_count > 5:
        return True

    html_lower = html.lower()
    spa_indicators = ["__next", "__nuxt", "react-root", "ng-app", "v-app", 'id="app"']
    if len(text) < 200 and any(ind in html_lower for ind in spa_indicators):
        return True

    return False


# =============================================================================
# HTML → MARKDOWN: BS4 FALLBACK (v2.3.0: relative links + colspan fix)
# =============================================================================

def html_to_markdown_bs4(html: str, url: str = "") -> str:
    """BS4-basierte Markdown-Konvertierung — sicheres Fallback."""
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
    _convert_element(main_content, lines, url=url)

    markdown = "\n".join(lines)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    return markdown.strip()


def html_to_markdown(html: str, url: str = "") -> str:
    """
    v2.3.0: Trafilatura Hybrid-Weiche.
    Trafilatura für saubere Texte (70-80% der Fälle),
    BS4 als sicheres Fallback wenn Trafilatura zu wenig extrahiert.
    """
    try:
        traf_markdown = trafilatura.extract(
            html,
            include_links=True,
            include_tables=True,
            include_formatting=True,
            output_format="markdown"
        )

        if traf_markdown:
            word_count = len(traf_markdown.split())
            if word_count >= 40:
                logger.info(f"⚡ Trafilatura erfolgreich: {url} ({word_count} Wörter)")
                return re.sub(r"\n{3,}", "\n\n", traf_markdown).strip()

    except Exception as e:
        logger.warning(f"⚠️ Trafilatura Fehler für {url}: {str(e)}")

    logger.info(f"🛡️ BS4 Fallback aktiv für: {url}")
    return html_to_markdown_bs4(html, url)


def _convert_element(element, lines: list, depth: int = 0, url: str = ""):
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

        if href and url:
            href = urljoin(url, href)

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
        _convert_element(child, lines, depth + 1, url=url)


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

        cell_texts = []
        for cell in cells:
            text = cell.get_text(" ", strip=True).replace("|", "\\|")
            colspan = int(cell.get("colspan", 1))
            cell_texts.extend([text] * colspan)

        row_str = "| " + " | ".join(cell_texts) + " |"
        markdown_rows.append(row_str)

        if row_idx == 0 and not separator_added:
            separator = "| " + " | ".join(["---"] * len(cell_texts)) + " |"
            markdown_rows.append(separator)
            separator_added = True

    return "\n".join(markdown_rows) if markdown_rows else ""


# =============================================================================
# QUALITY SCORING
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
# MAIN FETCH FUNCTION
# =============================================================================

async def fetch_and_convert(url: str) -> dict:
    if is_pdf_url(url):
        logger.info(f"📄 PDF erkannt: {url}")
        pdf_text = await extract_pdf_text(url)

        if pdf_text:
            markdown = pdf_text_to_markdown(pdf_text, url)
            quality = calculate_quality_score(markdown)
            return {
                "data": clean_text(markdown),
                "url": url, "content_type": "pdf",
                "quality_score": quality["final_score"], "quality_details": quality,
                "success": True, "error": None
            }
        else:
            return {
                "data": clean_text(f"[PDF-Extraktion fehlgeschlagen]\nURL: {url}"),
                "url": url, "content_type": "pdf", "quality_score": 0,
                "quality_details": {"quality_status": "pdf_extraction_failed", "word_count": 0},
                "success": False, "error": "PDF text extraction failed"
            }

    logger.info(f"🌐 Fetching: {url}")
    html = await fetch_html_fast(url)

    if html is None:
        try:
            client = await get_http_client()
            head_r = await client.head(url)
            ct = head_r.headers.get("content-type", "")
            if "application/pdf" in ct:
                pdf_text = await extract_pdf_text(url)
                if pdf_text:
                    markdown = pdf_text_to_markdown(pdf_text, url)
                    quality = calculate_quality_score(markdown)
                    return {
                        "data": clean_text(markdown),
                        "url": url, "content_type": "pdf",
                        "quality_score": quality["final_score"], "quality_details": quality,
                        "success": True, "error": None
                    }
        except Exception:
            pass

    if html is None:
        logger.info(f"🔄 httpx fehlgeschlagen, versuche Playwright: {url}")
        html = await fetch_html_playwright(url)

    if html is None:
        return {
            "data": clean_text(f"[Seite konnte nicht geladen werden]\nURL: {url}"),  # v2.4.1
            "url": url, "content_type": "error", "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False, "error": "Both httpx and Playwright failed"
        }

    if needs_javascript(html):
        logger.info(f"🔄 JS-Seite erkannt, nutze Playwright: {url}")
        pw_html = await fetch_html_playwright(url)
        if pw_html:
            html = pw_html

    markdown = html_to_markdown(html, url)
    quality = calculate_quality_score(markdown)

    logger.info(f"✅ Fetched {url} → {quality['word_count']} words, score: {quality['final_score']}/10")

    return {
        "data": clean_text(markdown),
        "url": url, "content_type": "html",
        "quality_score": quality["final_score"], "quality_details": quality,
        "success": True, "error": None
    }


# =============================================================================
# API ENDPOINTS
# =============================================================================

@router.get("/fetch-markdown")
async def fetch_markdown_endpoint(url: str = Query(..., description="URL to fetch and convert to Markdown")):
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
            "data": clean_text(f"[Fehler]\nURL: {url}\nFehler: {str(e)}"),  # v2.4.1
            "url": url, "content_type": "error", "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False, "error": clean_text(str(e))
        }


class BatchRequest(BaseModel):
    urls: List[str]


@router.post("/fetch-markdown-batch")
async def fetch_markdown_batch(request: BatchRequest):
    """
    BATCH ENDPOINT – bis zu 15 URLs parallel
    v2.4.1: clean_text() jetzt auch in allen Fehler-Returns
    v2.4.0: Null-Byte Fix via clean_text() in fetch_and_convert()
    v2.3.0: Trafilatura Hybrid + Relative Links Fix + Colspan Fix
    """
    if not request.urls:
        raise HTTPException(status_code=400, detail="urls list is required")

    urls = request.urls[:15]
    logger.info(f"🚀 Batch fetch started: {len(urls)} URLs")

    semaphore = asyncio.Semaphore(8)

    async def fetch_with_limit(url: str) -> dict:
        async with semaphore:
            return await fetch_and_convert(url)

    raw_results = await asyncio.gather(
        *[fetch_with_limit(url) for url in urls],
        return_exceptions=True
    )

    results = []
    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            logger.error(f"❌ Batch item {i} failed: {str(result)}")
            results.append({
                "data": clean_text(f"[Fehler]\nURL: {urls[i]}\nFehler: {str(result)}"),  # v2.4.1
                "url": urls[i], "content_type": "error", "quality_score": 0,
                "quality_details": {"quality_status": "fetch_error", "word_count": 0},
                "success": False, "error": clean_text(str(result))
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
