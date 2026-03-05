"""
Fetch Markdown API - Jina Replacement
FastAPI Endpoint deployed on Railway.app
Converts URLs to clean Markdown for n8n WF2 (Content Extraction)

Response format: { "data": "markdown..." }
Compatible with existing Clean Markdown Code Node in n8n WF2

v1.3.0 - Added /fetch-markdown-batch endpoint for parallel processing
"""

from fastapi import APIRouter, HTTPException, Query
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString
from pydantic import BaseModel
from typing import List
import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# =============================================================================
# ROUTER (wird in discovery_api.py eingebunden)
# =============================================================================

router = APIRouter()

# =============================================================================
# PDF DETECTION
# =============================================================================

def is_pdf_url(url: str) -> bool:
    """Erkennt ob eine URL auf eine PDF zeigt"""
    url_lower = url.lower().strip()
    if url_lower.endswith(".pdf"):
        return True
    if ".pdf?" in url_lower or ".pdf#" in url_lower:
        return True
    return False

# =============================================================================
# HTML → MARKDOWN CONVERSION
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
        logger.warning(f"📄 PDF detected, skipping: {url}")
        return {
            "data": f"[PDF-Dokument erkannt – direkte Extraktion nicht unterstützt]\nURL: {url}",
            "url": url,
            "content_type": "pdf",
            "quality_score": 0,
            "quality_details": {"quality_status": "pdf_skipped", "word_count": 0},
            "success": False,
            "error": "PDF detected - extraction not supported"
        }

    logger.info(f"🌐 Fetching: {url}")

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

        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}",
                           lambda route: route.abort())
        await context.route("**/{analytics,tracking,ads,doubleclick}**",
                           lambda route: route.abort())

        try:
            page = await context.new_page()
            response = await page.goto(url, timeout=30000, wait_until="domcontentloaded")

            content_type = ""
            if response:
                content_type = response.headers.get("content-type", "")

            if "application/pdf" in content_type:
                await browser.close()
                return {
                    "data": f"[PDF-Dokument erkannt via Content-Type]\nURL: {url}",
                    "url": url,
                    "content_type": "pdf",
                    "quality_score": 0,
                    "quality_details": {"quality_status": "pdf_skipped", "word_count": 0},
                    "success": False,
                    "error": "PDF detected via Content-Type"
                }

            await page.wait_for_timeout(1500)
            html = await page.content()
            await browser.close()

        except Exception as e:
            await browser.close()
            logger.error(f"❌ Playwright error for {url}: {str(e)}")
            raise

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
# API ENDPOINTS
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
# NEU: BATCH ENDPOINT (3 URLs parallel)
# =============================================================================

class BatchRequest(BaseModel):
    urls: List[str]

@router.post("/fetch-markdown-batch")
async def fetch_markdown_batch(request: BatchRequest):
    """
    BATCH ENDPOINT – bis zu 3 URLs parallel verarbeiten
    
    POST /fetch-markdown-batch
    Body: { "urls": ["https://url1", "https://url2", "https://url3"] }
    
    Response:
    {
        "results": [
            { "data": "...", "url": "...", "quality_score": 7, "success": true },
            { "data": "...", "url": "...", "quality_score": 5, "success": true },
            { "data": "...", "url": "...", "quality_score": 0, "success": false }
        ],
        "total": 3,
        "successful": 2,
        "failed": 1
    }
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
