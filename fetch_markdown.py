"""
Fetch Markdown API - Jina Replacement
FastAPI Endpoint deployed on Railway.app
Converts URLs to clean Markdown for n8n WF2 (Content Extraction)

v2.7.0 - Speed (Event-Loop + Playwright) + PDF-Fixes:
  Speed:
  1. needs_javascript/html_to_markdown/calculate_quality_score laufen jetzt
     ueber asyncio.to_thread() statt synchron im Event-Loop. Diese Aufrufe
     sind CPU-lastig (BeautifulSoup, trafilatura) und blockierten bisher
     kurz ALLE parallelen Tasks im selben Batch (Uvicorn = 1 Worker = 1
     Event-Loop). Reine Ausfuehrungsverlagerung, kein Verhaltensunterschied.
  2. fetch_html_playwright() akzeptiert jetzt einen optionalen, bereits
     laufenden `browser`. fetch_markdown_batch() startet EINEN Browser pro
     Batch-Aufruf statt bis zu Semaphore(8)-mal einen eigenen Chromium-
     Prozess pro JS-Seite. Pro URL weiterhin ein eigener Context. Der
     Einzel-Endpoint /fetch-markdown ruft ohne `browser`-Param auf und
     verhaelt sich exakt wie vorher.
  PDF-Fixes:
  3. Groessen-Check VOR dem Download: extract_pdf_text() laedt jetzt
     gestreamt (client.stream statt client.get), prueft Content-Length
     vorab und bricht bei Ueberschreitung von MAX_PDF_BYTES (25 MB) ab -
     vorher gab es hier ueberhaupt keinen Groessen-Check, anders als beim
     HTML-Pfad.
  4. fitz.open() + Tabellenerkennung pro PDF-Seite (CPU-lastig) laeuft
     jetzt ueber asyncio.to_thread() - gleicher Grund wie Fix 1.
  5. pdf_text_to_markdown(): die alte Heading-Regel (jede GROSSGESCHRIEBENE
     Zeile < 100 Zeichen wird zu "## Heading") erzeugte bei Behoerden-PDFs
     massenhaft Falsch-Headings aus Formular-Labels und Rechtshinweisen.
     Da calculate_quality_score() heading_count > 3 belohnt, wurden PDFs
     voller Boilerplate dadurch systematisch BESSER bewertet als PDFs mit
     dichtem echtem Flieesstext. Neue Regel (_looks_like_pdf_heading):
     zusaetzlich keine Satzende-Interpunktion und 1-8 Woerter.

v2.6.0 - Drei Performance-/Resilienz-Fixes (zähe Zone + RAM-Sicherheit):
  1. Per-URL-Timeout: asyncio.wait_for(fetch_and_convert(url), timeout=60) im
     Batch-Handler. Eine zähe URL belegt damit höchstens 60s einen
     Semaphore-Platz statt ~130-165s (httpx-Retries + HEAD + Playwright).
     Hebt den Durchsatz in den langsamen Ländern.
  2. fetch_html_playwright cancel-sicher: browser.close() ins finally. Bricht
     der Per-URL-Timeout die Coroutine mitten im goto() ab, wird der Browser
     trotzdem geschlossen — kein Chromium-Leak, kein OOM über die Hintertür.
  3. MAX_RETRIES 3 -> 1: eine tote/lahme Seite wird beim Wiederholen nicht
     schnell; die 3 Versuche x 25s addierten nur Wartezeit pro kaputter URL.

v2.5.1 - Drei Robustheits-Fixes:
  1. Markdown-Tabellen-Erkennung via Regex (\\|\\s*-{3,}) statt exaktem "| ---"
     — im Tabellen-Wächter UND im Quality Score. Trafilatura gibt Separatoren
     je nach Version auch ohne Leerzeichen aus (|---|); der exakte String-Match
     hätte dann 0 gezählt → unnötiger BS4-Fallback bei jeder Tabellen-Seite
     und um 2 Punkte zu niedrige Quality Scores.
  2. PDF-Block-Filter: get_text("blocks") liefert auch Bild-Blöcke
     (block_type 1, Text wie "<image: DeviceRGB...>"). Diese werden jetzt
     übersprungen — keine Bild-Artefakte mehr im extrahierten Fließtext.
  3. Batch Limit 15 → 20: passend zu WF2 "Get Pending URLs" (limit=20).
     Bisher wurden die letzten 5 URLs jedes Zyklus kommentarlos verworfen
     und im Folgezyklus erneut geholt (25% Leerlauf). Semaphore(8) bleibt —
     die Parallelität regelt weiterhin die Queue, nicht das Batch-Limit.

v2.5.0 - Drei Fakten-Extraktions-Fixes:
  1. Trafilatura Tabellen-Wächter: Trafilatura-Output wird nur akzeptiert,
     wenn keine Tabellen verloren gingen (HTML-Tabellen vs. Markdown-Tabellen)
     und die Zahlen-Dichte nicht stark eingebrochen ist. Sonst BS4-Fallback.
  2. PDF-Tabellenextraktion: page.find_tables() (PyMuPDF >= 1.23) erkennt
     Tabellen in PDFs und gibt sie als Markdown-Tabellen aus. Fließtext
     außerhalb der Tabellen bleibt erhalten, Reihenfolge nach Y-Position.
     Fallback: bisheriges get_text()-Verhalten.
  3. BS4-Tabellen-Konverter robust: rowspan + colspan werden korrekt
     aufgefüllt, <br> in Zellen zerstört die Tabelle nicht mehr,
     Header-Erkennung via <th> (synthetischer Header wenn keiner existiert),
     alle Zeilen auf einheitliche Spaltenbreite gepolstert.

v2.4.1 - Null-Byte Fix erweitert: clean_text() jetzt auch in allen Fehler-Returns
v2.4.0 - Null-Byte Fix: clean_text() entfernt Null-Bytes und Steuerzeichen
v2.3.0 - Relative Links Fix, Colspan Fix, Trafilatura Hybrid
v2.2.0 - Encoding Fix: UTF-8 first, Fallback auf deklariertes Encoding
v2.1.0 - Batch Limit 15, Semaphore(8)
v2.0.0 - httpx Standard, Playwright Fallback, PDF Support
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

# v2.6.0: 3 -> 1. Eine tote/lahme Seite wird beim Wiederholen nicht schnell.
MAX_RETRIES = 1
RETRY_DELAYS = [1, 3, 5]

# v2.6.0: Hartes Per-URL-Limit im Batch-Handler (Sekunden).
PER_URL_TIMEOUT = 60

# fix: PDFs hatten bisher KEINEN Größen-Check vor dem Download (anders als
# der HTML-Pfad, der Content-Length schon vorher prüft). Ein sehr großes
# PDF (Scan-Bände, Gesetzestexte als Sammelband) konnte damit unkontrolliert
# Zeit/RAM ziehen, bevor überhaupt entschieden wurde ob sich die Extraktion
# lohnt. 25 MB deckt reguläre Behörden-PDFs komfortabel ab.
MAX_PDF_BYTES = 25 * 1024 * 1024

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
# v2.5.0: ZAHLEN-ZÄHLUNG (für Fix 1: Trafilatura Tabellen-Wächter)
# =============================================================================

def _count_numbers(text: str) -> int:
    """Zählt Zahlen-Tokens in einem Text (Indikator für Fakten-Dichte)."""
    if not text:
        return 0
    return len(re.findall(r'\b\d+[\.,]?\d*\b', text))


# =============================================================================
# v2.5.1 FIX 1: MARKDOWN-TABELLEN-ZÄHLUNG
# Erkennt Tabellen-Separatoren unabhängig vom Format: "| ---", "|---|",
# "| :--- |" etc. Ein exakter String-Match auf "| ---" verfehlt
# Trafilatura-Output je nach Version komplett.
# Wird im Tabellen-Wächter UND im Quality Score verwendet.
# =============================================================================

def _count_md_tables(text: str) -> int:
    """
    Zählt Markdown-Tabellen-Separatorzeilen (eine pro Tabelle).
    Mindestens eine Pipe gefordert — sonst zählen <hr>-Linien ("---")
    aus dem BS4-Konverter fälschlich als Tabelle.
    """
    if not text:
        return 0
    return len(re.findall(r'^\s*(?=.*\|)[\s:|]*-{3,}[\s:|-]*$', text, re.MULTILINE))


def _html_content_numbers(html: str) -> int:
    """
    Zählt Zahlen im inhaltlichen Teil des HTML (ohne Navigation/Footer/Scripts).
    Dient als Referenzwert: Verliert Trafilatura mehr als die Hälfte dieser
    Zahlen, war die Extraktion zu aggressiv → BS4-Fallback.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "noscript"]):
            tag.decompose()
        return _count_numbers(soup.get_text(" ", strip=True))
    except Exception:
        return 0


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


# =============================================================================
# v2.5.0 FIX 2: PDF-TABELLENEXTRAKTION
# page.find_tables() erkennt Tabellen, diese werden als Markdown-Tabellen
# ausgegeben. Fließtext außerhalb der Tabellen-Bereiche wird per
# get_text("blocks") extrahiert und nach vertikaler Position einsortiert.
# Fallback bei jedem Fehler: page.get_text() wie bisher.
# v2.5.1: Bild-Blöcke (block_type 1) werden übersprungen.
# =============================================================================

def _pdf_table_to_markdown(table) -> str:
    """Konvertiert eine von PyMuPDF erkannte Tabelle in Markdown."""
    try:
        data = table.extract()
    except Exception:
        return ""

    rows = []
    for raw_row in data:
        cells = []
        for cell in raw_row:
            text = (cell or "")
            text = re.sub(r"\s+", " ", text).strip()
            text = text.replace("|", "\\|")
            cells.append(text)
        if any(cells):
            rows.append(cells)

    if not rows:
        return ""

    width = max(len(r) for r in rows)
    md_rows = []
    for i, r in enumerate(rows):
        r = r + [""] * (width - len(r))
        md_rows.append("| " + " | ".join(r) + " |")
        if i == 0:
            md_rows.append("| " + " | ".join(["---"] * width) + " |")

    return "\n".join(md_rows)


def _pdf_page_to_markdown(page, fitz_module) -> str:
    """
    Extrahiert eine PDF-Seite mit Tabellen-Erkennung.
    Tabellen → Markdown-Tabellen, restlicher Text → Fließtext,
    Reihenfolge nach Y-Position auf der Seite.
    """
    try:
        tab_finder = page.find_tables()
        tables = list(tab_finder.tables) if tab_finder else []
    except Exception as e:
        logger.warning(f"⚠️ find_tables() fehlgeschlagen, nutze get_text(): {str(e)}")
        tables = []

    if not tables:
        return page.get_text()

    table_items = []   # (y0, markdown)
    table_rects = []

    for t in tables:
        md = _pdf_table_to_markdown(t)
        if md:
            rect = fitz_module.Rect(t.bbox)
            table_items.append((rect.y0, md))
            table_rects.append(rect)

    if not table_items:
        return page.get_text()

    # Fließtext-Blöcke außerhalb der Tabellen-Bereiche einsammeln
    text_items = []
    try:
        for block in page.get_text("blocks"):
            # v2.5.1: Bild-Blöcke überspringen (block_type 1 = Bild).
            # Tuple-Format: (x0, y0, x1, y1, text, block_no, block_type)
            if len(block) >= 7 and block[6] != 0:
                continue
            x0, y0, x1, y1, block_text = block[0], block[1], block[2], block[3], block[4]
            block_rect = fitz_module.Rect(x0, y0, x1, y1)
            if any(block_rect.intersects(tr) for tr in table_rects):
                continue
            block_text = block_text.strip()
            if block_text:
                text_items.append((y0, block_text))
    except Exception as e:
        logger.warning(f"⚠️ Block-Extraktion fehlgeschlagen: {str(e)}")

    all_items = sorted(table_items + text_items, key=lambda item: item[0])
    return "\n\n".join(item[1] for item in all_items)


def _extract_pdf_text_sync(pdf_bytes: bytes, fitz_module) -> Optional[str]:
    """Reiner CPU-Teil der PDF-Extraktion — wird über asyncio.to_thread() aufgerufen."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        doc = fitz_module.open(tmp_path)
        # v2.5.0: pro Seite Tabellen-Erkennung statt nur get_text()
        text_parts = [_pdf_page_to_markdown(page, fitz_module) for page in doc]
        doc.close()
        full_text = "\n\n".join(text_parts).strip()
        return full_text if len(full_text) >= 50 else None
    finally:
        if tmp_path:
            os.unlink(tmp_path)


async def extract_pdf_text(url: str) -> Optional[str]:
    try:
        import fitz
    except ImportError:
        logger.warning("⚠️ pymupdf nicht installiert")
        return None

    try:
        client = await get_http_client()

        # fix: gestreamt laden statt client.get() (laedt sonst den ganzen
        # Body sofort in den RAM). So kann Content-Length VOR dem Lesen
        # geprüft werden, und ein zu großes PDF wird abgebrochen statt
        # komplett heruntergeladen.
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                return None

            content_length = r.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_PDF_BYTES:
                        logger.warning(
                            f"⏭️ PDF zu groß ({content_length} bytes > "
                            f"{MAX_PDF_BYTES}), skip: {url}"
                        )
                        return None
                except ValueError:
                    pass

            chunks = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    logger.warning(
                        f"⏭️ PDF-Download abgebrochen bei {total} bytes "
                        f"(Limit {MAX_PDF_BYTES}, kein Content-Length-Header "
                        f"vorab): {url}"
                    )
                    return None
                chunks.append(chunk)
            pdf_bytes = b"".join(chunks)

        # perf: fitz.open() + Tabellenerkennung pro Seite ist CPU-lastig,
        # gleiches Prinzip wie beim HTML-Parsing — in Thread auslagern.
        return await asyncio.to_thread(_extract_pdf_text_sync, pdf_bytes, fitz)

    except Exception as e:
        logger.error(f"❌ PDF-Extraktion fehlgeschlagen für {url}: {str(e)}")
        return None


def _looks_like_pdf_heading(line: str) -> bool:
    """
    fix: die alte Regel (line.isupper() and len(line) < 100) machte aus JEDER
    großgeschriebenen Zeile unter 100 Zeichen eine Überschrift. Bei Behörden-
    PDFs sind Formular-Labels, Rechtshinweise und Disclaimer-Sätze aber
    häufig komplett großgeschrieben, ohne echte Überschriften zu sein.
    calculate_quality_score() belohnt heading_count > 3 mit einem Bonus-
    punkt — PDFs voller Boilerplate wurden dadurch systematisch besser
    bewertet als PDFs mit dichtem echtem Fließtext ohne Großschreibung.

    Zusätzliche, konservative Kriterien: keine Satzende-Interpunktion
    (echte Überschriften sind selten ganze Sätze, Rechtshinweise fast
    immer) und eine plausible Wortzahl (1–8 Wörter).
    """
    if not line.isupper() or len(line) >= 100:
        return False
    if line[-1] in ".!?":
        return False
    word_count = len(line.split())
    return 1 <= word_count <= 8


def pdf_text_to_markdown(text: str, url: str) -> str:
    lines = text.split("\n")
    md_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            md_lines.append("")
        elif line.startswith("|"):
            # v2.5.0: Markdown-Tabellenzeilen aus _pdf_page_to_markdown()
            # unverändert durchreichen (sonst macht die Heading-Prüfung
            # daraus Headings)
            md_lines.append(line)
        elif _looks_like_pdf_heading(line):
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


async def fetch_html_playwright(url: str, browser=None) -> Optional[str]:
    """
    Rendert eine Seite per Playwright.

    perf: Wenn ein bereits laufender `browser` uebergeben wird (Batch-Modus,
    siehe fetch_markdown_batch), wird dieser wiederverwendet - kein neuer
    Chromium-Prozess pro URL mehr (vorher: bis zu Semaphore(8) parallele
    Browser-Starts im selben Batch, je ~1-3s Overhead + RAM/CPU-Spitze auf
    dem Hetzner-Server, der sich das mit n8n teilt). Pro URL weiterhin ein
    eigener Context, damit Cookies/Storage zwischen URLs isoliert bleiben -
    Playwrights vorgesehenes Muster fuer nebenlaeufige Nutzung eines Browsers.

    Ohne uebergebenen Browser (z.B. der Einzel-Endpoint /fetch-markdown)
    startet die Funktion wie bisher ihren eigenen kurzlebigen Browser und
    schliesst ihn danach wieder - unveraendertes Verhalten dort.
    """
    owns_browser = browser is None
    playwright_ctx = None
    active_browser = browser
    context = None

    try:
        if owns_browser:
            playwright_ctx = await async_playwright().start()
            active_browser = await playwright_ctx.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=VizDisplayCompositor'
                ]
            )

        context = await active_browser.new_context(
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
        return html

    except Exception as e:
        logger.error(f"❌ Playwright Fehler für {url}: {str(e)}")
        return None
    finally:
        # v2.6.0: cancel-sicher - auch bei Abbruch durch den Per-URL-Timeout
        # (asyncio.wait_for) wird hier sauber aufgeraeumt, kein Leak.
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if owns_browser:
            if active_browser is not None:
                try:
                    await active_browser.close()
                except Exception:
                    pass
            if playwright_ctx is not None:
                try:
                    await playwright_ctx.stop()
                except Exception:
                    pass


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
# HTML → MARKDOWN: BS4 FALLBACK
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


# =============================================================================
# v2.5.0 FIX 1: TRAFILATURA TABELLEN-WÄCHTER
# Trafilatura-Output wird nur akzeptiert wenn:
#   a) keine Tabellen verloren gingen (HTML-Tabellen vs. Markdown-Tabellen)
#   b) die Zahlen-Dichte nicht um mehr als 50% eingebrochen ist
# Sonst: BS4-Fallback (verlustfrei, aber weniger sauber).
# v2.5.1: Tabellen-Zählung via _count_md_tables() (Regex) statt "| ---" —
# erkennt alle Separator-Varianten, kein falscher Fallback mehr.
# =============================================================================

def html_to_markdown(html: str, url: str = "") -> str:
    html_table_count = len(re.findall(r"<table[\s>]", html, re.I))

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

                # Check a: Tabellen-Verlust (v2.5.1: Regex-Zählung)
                md_table_count = _count_md_tables(traf_markdown)
                if html_table_count > 0 and md_table_count == 0:
                    logger.info(
                        f"📊 Tabellen-Wächter: {html_table_count} Tabelle(n) im HTML, "
                        f"0 im Trafilatura-Output → BS4 Fallback: {url}"
                    )
                else:
                    # Check b: Zahlen-Verlust
                    html_numbers = _html_content_numbers(html)
                    md_numbers = _count_numbers(traf_markdown)
                    if html_numbers > 10 and md_numbers < html_numbers * 0.5:
                        logger.info(
                            f"🔢 Zahlen-Wächter: {html_numbers} Zahlen im HTML, "
                            f"nur {md_numbers} im Trafilatura-Output → BS4 Fallback: {url}"
                        )
                    else:
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


# =============================================================================
# v2.5.0 FIX 3: ROBUSTER BS4-TABELLEN-KONVERTER
#   - rowspan + colspan werden korrekt über Zeilen/Spalten aufgefüllt
#   - <br> und Mehrzeiligkeit in Zellen zerstören die Tabelle nicht mehr
#   - Header via <th> erkannt; synthetischer Header wenn keiner existiert
#     (Markdown verlangt eine Header-Zeile — ohne diese würde die erste
#      Datenzeile fälschlich als Header interpretiert)
#   - alle Zeilen auf einheitliche Spaltenbreite gepolstert
# =============================================================================

def _cell_text(cell) -> str:
    """Zellentext extrahieren ohne die Markdown-Tabelle zu zerstören."""
    for br in cell.find_all("br"):
        br.replace_with(" ")
    text = cell.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.replace("|", "\\|")


def _safe_int(value, default: int = 1) -> int:
    try:
        n = int(value)
        return n if 1 <= n <= 50 else default
    except (TypeError, ValueError):
        return default


def _convert_table(table_element) -> str:
    all_rows = table_element.find_all("tr")
    if not all_rows:
        return ""

    # pending: Spaltenindex → (verbleibende Zeilen, Text) für aktive rowspans
    pending = {}
    grid = []        # Liste von Zell-Listen
    header_flags = []  # pro Zeile: enthält <th>?

    def take_pending(col_idx: int) -> str:
        remaining, text = pending[col_idx]
        if remaining > 1:
            pending[col_idx] = (remaining - 1, text)
        else:
            del pending[col_idx]
        return text

    for row in all_rows:
        cells = row.find_all(["th", "td"])
        if not cells and not pending:
            continue

        row_data = []
        col = 0

        for cell in cells:
            # Spalten überspringen, die von rowspans darüber belegt sind
            while col in pending:
                row_data.append(take_pending(col))
                col += 1

            text = _cell_text(cell)
            colspan = _safe_int(cell.get("colspan"))
            rowspan = _safe_int(cell.get("rowspan"))

            for _ in range(colspan):
                row_data.append(text)
                if rowspan > 1:
                    pending[col] = (rowspan - 1, text)
                col += 1

        # rowspans rechts der letzten Zelle dieser Zeile auffüllen
        while pending and col <= max(pending.keys()):
            if col in pending:
                row_data.append(take_pending(col))
            else:
                row_data.append("")
            col += 1

        if row_data:
            grid.append(row_data)
            header_flags.append(row.find("th") is not None)

    if not grid:
        return ""

    # Einheitliche Spaltenbreite
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]

    # Header-Erkennung: erste Zeile mit <th> ist Header.
    # Hat keine Zeile <th>, wird ein synthetischer (leerer) Header eingefügt,
    # damit die erste Datenzeile nicht als Header verloren geht.
    markdown_rows = []
    separator = "| " + " | ".join(["---"] * width) + " |"

    if header_flags[0]:
        markdown_rows.append("| " + " | ".join(grid[0]) + " |")
        markdown_rows.append(separator)
        data_rows = grid[1:]
    else:
        markdown_rows.append("| " + " | ".join([" "] * width) + " |")
        markdown_rows.append(separator)
        data_rows = grid

    for r in data_rows:
        markdown_rows.append("| " + " | ".join(r) + " |")

    return "\n".join(markdown_rows)


# =============================================================================
# QUALITY SCORING
# v2.5.1: Tabellen-Zählung via _count_md_tables() — konsistent mit dem
# Tabellen-Wächter. Trafilatura-Tabellen wurden vorher nicht gezählt
# (Separator-Format) → Score war um bis zu 2 Punkte zu niedrig.
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

    table_count = _count_md_tables(markdown)  # v2.5.1
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

async def fetch_and_convert(url: str, browser=None) -> dict:
    """
    browser: optionaler, bereits laufender Playwright-Browser (Batch-Modus,
    siehe fetch_markdown_batch). Wenn None (z.B. Einzel-Endpoint
    /fetch-markdown), startet fetch_html_playwright() weiterhin seinen
    eigenen kurzlebigen Browser wie bisher — unveraendertes Verhalten dort.
    """
    if is_pdf_url(url):
        logger.info(f"📄 PDF erkannt: {url}")
        pdf_text = await extract_pdf_text(url)

        if pdf_text:
            markdown = pdf_text_to_markdown(pdf_text, url)
            quality = await asyncio.to_thread(calculate_quality_score, markdown)
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
                    quality = await asyncio.to_thread(calculate_quality_score, markdown)
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
        html = await fetch_html_playwright(url, browser=browser)

    if html is None:
        return {
            "data": clean_text(f"[Seite konnte nicht geladen werden]\nURL: {url}"),  # v2.4.1
            "url": url, "content_type": "error", "quality_score": 0,
            "quality_details": {"quality_status": "fetch_error", "word_count": 0},
            "success": False, "error": "Both httpx and Playwright failed"
        }

    if await asyncio.to_thread(needs_javascript, html):
        logger.info(f"🔄 JS-Seite erkannt, nutze Playwright: {url}")
        pw_html = await fetch_html_playwright(url, browser=browser)
        if pw_html:
            html = pw_html

    markdown = await asyncio.to_thread(html_to_markdown, html, url)
    quality = await asyncio.to_thread(calculate_quality_score, markdown)

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
    BATCH ENDPOINT – bis zu 20 URLs parallel
    v2.6.0: Per-URL-Timeout (60s) via asyncio.wait_for, cancel-sicherer
            Playwright-Browser (finally), MAX_RETRIES 3->1
    v2.5.1: Batch Limit 15 → 20 (passend zu WF2 limit=20), Tabellen-Zählung
            via Regex, PDF-Bild-Blöcke gefiltert
    v2.5.0: Tabellen-Wächter, PDF-Tabellenextraktion, robuster Tabellen-Konverter
    v2.4.1: clean_text() jetzt auch in allen Fehler-Returns
    v2.4.0: Null-Byte Fix via clean_text() in fetch_and_convert()
    v2.3.0: Trafilatura Hybrid + Relative Links Fix + Colspan Fix
    """
    if not request.urls:
        raise HTTPException(status_code=400, detail="urls list is required")

    # v2.5.1: 15 → 20, passend zu WF2 "Get Pending URLs" (limit=20).
    # Semaphore(8) begrenzt weiterhin die echte Parallelität.
    urls = request.urls[:20]
    logger.info(f"🚀 Batch fetch started: {len(urls)} URLs")

    semaphore = asyncio.Semaphore(8)

    # perf: EIN Browser fuer den ganzen Batch statt einem pro JS-Seite.
    # Vorher konnte fetch_html_playwright() bis zu 8x gleichzeitig einen
    # eigenen Chromium-Prozess starten (Semaphore(8)) - spuerbarer Overhead
    # und RAM/CPU-Spitze auf dem Hetzner-Server, der sich das mit n8n teilt.
    playwright_ctx = await async_playwright().start()
    shared_browser = await playwright_ctx.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
            '--disable-features=VizDisplayCompositor'
        ]
    )

    async def fetch_with_limit(url: str) -> dict:
        async with semaphore:
            # v2.6.0: Hartes Per-URL-Limit. Eine zähe URL belegt höchstens
            # PER_URL_TIMEOUT Sekunden einen Semaphore-Platz statt ~130-165s.
            try:
                return await asyncio.wait_for(
                    fetch_and_convert(url, browser=shared_browser), timeout=PER_URL_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Per-URL-Timeout (>{PER_URL_TIMEOUT}s) für {url}")
                return {
                    "data": clean_text(f"[Timeout >{PER_URL_TIMEOUT}s]\nURL: {url}"),
                    "url": url, "content_type": "error", "quality_score": 0,
                    "quality_details": {"quality_status": "timeout", "word_count": 0},
                    "success": False, "error": "per-url timeout"
                }

    try:
        raw_results = await asyncio.gather(
            *[fetch_with_limit(url) for url in urls],
            return_exceptions=True
        )
    finally:
        # perf: Browser IMMER schliessen, auch bei Exceptions im gather.
        try:
            await shared_browser.close()
        except Exception:
            pass
        try:
            await playwright_ctx.stop()
        except Exception:
            pass

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
