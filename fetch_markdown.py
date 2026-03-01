"""
Fetch Markdown API - Jina Replacement
FastAPI Endpoint deployed on Railway.app
Converts URLs to clean Markdown for n8n WF2 (Content Extraction)

Response format: { "data": "markdown..." }
Compatible with existing Clean Markdown Code Node in n8n WF2
"""

from fastapi import APIRouter, HTTPException, Query
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, NavigableString
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
    # Direkte .pdf Extension
    if url_lower.endswith(".pdf"):
        return True
    # PDF in Query-String (z.B. ?file=doc.pdf)
    if ".pdf?" in url_lower or ".pdf#" in url_lower:
        return True
    return False

# =============================================================================
# HTML → MARKDOWN CONVERSION
# =============================================================================

def html_to_markdown(html: str, url: str = "") -> str:
    """
    Konvertiert HTML zu sauberem Markdown
    Erhält Tabellen, Listen, Überschriften
    Entfernt Navigation, Footer, Scripts, Ads
    """
    soup = BeautifulSoup(html, "html.parser")

    # Entferne irrelevante Elemente
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "svg", "button",
                     "form", "input", "select", "textarea", "meta",
                     "link", "figure", "figcaption"]):
        tag.decompose()

    # Entferne versteckte Elemente
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")):
        tag.decompose()

    # Finde den Hauptinhalt (Content-Bereich)
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

    # Zusammenführen und bereinigen
    markdown = "\n".join(lines)

    # Mehrfache Leerzeilen reduzieren
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    return markdown.strip()


def _convert_element(element, lines: list, depth: int = 0):
    """Rekursiv HTML-Elemente zu Markdown konvertieren"""

    if isinstance(element, NavigableString):
        text = str(element).strip()
        if text:
            lines.append(text)
        return

    tag = element.name if element.name else ""

    # Überschriften
    if tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        level = int(tag[1])
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"\n{'#' * level} {text}\n")
        return

    # Paragraphen
    if tag == "p":
        text = element.get_text(" ", strip=True)
        if text:
            lines.append(f"\n{text}\n")
        return

    # Tabellen → Markdown Tabelle
    if tag == "table":
        table_md = _convert_table(element)
        if table_md:
            lines.append(f"\n{table_md}\n")
        return

    # Listen
    if tag in ["ul", "ol"]:
        lines.append("")
        for i, li in enumerate(element.find_all("li", recursive=False), 1):
            text = li.get_text(" ", strip=True)
            if text:
                prefix = f"{i}." if tag == "ol" else "-"
                lines.append(f"{prefix} {text}")
        lines.append("")
        return

    # Links → Text mit URL
    if tag == "a":
        text = element.get_text(" ", strip=True)
        href = element.get("href", "").strip()
        if text and href and href.startswith("http"):
            lines.append(f"[{text}]({href})")
        elif text:
            lines.append(text)
        return

    # Fett / Kursiv
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

    # Zeilenumbruch
    if tag == "br":
        lines.append("\n")
        return

    # Horizontale Linie
    if tag == "hr":
        lines.append("\n---\n")
        return

    # Alles andere: Kinder rekursiv verarbeiten
    for child in element.children:
        _convert_element(child, lines, depth + 1)


def _convert_table(table_element) -> str:
    """Konvertiert HTML-Tabelle zu Markdown-Tabelle"""
    rows = []

    # Header-Zeilen
    header_rows = table_element.find_all("tr", limit=1)
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

        # Separator nach erster Zeile (Header)
        if row_idx == 0 and not separator_added:
            separator = "| " + " | ".join(["---"] * len(cells)) + " |"
            markdown_rows.append(separator)
            separator_added = True

    return "\n".join(markdown_rows) if markdown_rows else ""

# =============================================================================
# QUALITY SCORING
# =============================================================================

def calculate_quality_score(markdown: str) -> dict:
    """
    Berechnet Qualitäts-Score für extrahierten Markdown-Content
    Kompatibel mit dem bestehenden Pre-Check in WF2
    Score 0-10
    """
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

    # Zahlen und Währungen (wichtig für Finanz/Visa-Daten)
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

    # Tabellen (strukturierte Daten)
    table_count = markdown.count("| ---")
    details["tables_found"] = table_count
    if table_count > 0:
        score += 2
    
    # Überschriften (strukturierter Content)
    heading_count = len(re.findall(r'^#{1,6} ', markdown, re.MULTILINE))
    details["headings_found"] = heading_count
    if heading_count > 3:
        score += 1

    # Links (Navigation-Check – zu viele Links = schlechter Content)
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
    """
    Hauptfunktion: URL → sauberes Markdown
    Gibt kompatibles Format zurück: { "data": "markdown..." }
    """

    # PDF-Erkennung
    if is_pdf_url(url):
        logger.warning(f"📄 PDF detected, skipping: {url}")
        return {
            "data": f"[PDF-Dokument erkannt – direkte Extraktion nicht unterstützt]\nURL: {url}\n\nHinweis: Dieses Dokument ist eine PDF-Datei und kann nicht automatisch als Markdown extrahiert werden. Bitte manuell prüfen.",
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
            # Bilder und Fonts blockieren → schneller
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )

        # Bilder, Fonts, Media blockieren für Speed
        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}", 
                           lambda route: route.abort())
        await context.route("**/{analytics,tracking,ads,doubleclick}**", 
                           lambda route: route.abort())

        try:
            page = await context.new_page()

            response = await page.goto(
                url,
                timeout=30000,
                wait_until="domcontentloaded"
            )

            # Check ob PDF über Content-Type
            content_type = ""
            if response:
                content_type = response.headers.get("content-type", "")

            if "application/pdf" in content_type:
                await browser.close()
                logger.warning(f"📄 PDF detected via Content-Type: {url}")
                return {
                    "data": f"[PDF-Dokument erkannt via Content-Type]\nURL: {url}\n\nHinweis: Server liefert PDF-Datei. Direkte Extraktion nicht unterstützt.",
                    "url": url,
                    "content_type": "pdf",
                    "quality_score": 0,
                    "quality_details": {"quality_status": "pdf_skipped", "word_count": 0},
                    "success": False,
                    "error": "PDF detected via Content-Type"
                }

            # Kurz warten damit JS rendern kann
            await page.wait_for_timeout(1500)

            html = await page.content()
            await browser.close()

        except Exception as e:
            await browser.close()
            logger.error(f"❌ Playwright error for {url}: {str(e)}")
            raise

    # HTML → Markdown
    markdown = html_to_markdown(html, url)

    # Qualitäts-Score
    quality = calculate_quality_score(markdown)

    logger.info(f"✅ Fetched {url} → {quality['word_count']} words, score: {quality['final_score']}/10")

    return {
        # Hauptfeld: kompatibel mit bestehendem Clean Markdown Code Node
        "data": markdown,
        # Zusatzfelder für Debugging / n8n Logging
        "url": url,
        "content_type": "html",
        "quality_score": quality["final_score"],
        "quality_details": quality,
        "success": True,
        "error": None
    }

# =============================================================================
# API ENDPOINT
# =============================================================================

@router.get("/fetch-markdown")
async def fetch_markdown_endpoint(url: str = Query(..., description="URL to fetch and convert to Markdown")):
    """
    JINA REPLACEMENT ENDPOINT
    
    Fetches a URL and converts it to clean Markdown.
    Response format compatible with existing n8n Clean Markdown Code Node.
    
    Usage in n8n (replaces Jina HTTP Node):
    GET https://your-railway-url/fetch-markdown?url={url}
    
    Response:
    {
        "data": "# Page Title\\n\\nContent...",
        "url": "https://...",
        "quality_score": 7,
        "success": true
    }
    
    Jina equivalent was:
    GET https://r.jina.ai/{url}
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
        
        # Kein Hard-Crash – saubere Fehlermeldung zurückgeben
        # Clean Markdown Node bekommt trotzdem ein valides { data: ... } Objekt
        return {
            "data": f"[Fehler beim Laden der Seite]\nURL: {url}\nFehler: {str(e)}\n\nDiese URL konnte nicht verarbeitet werden.",
            "url": url,
            "content_type": "error",
            "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False,
            "error": str(e)
        }
