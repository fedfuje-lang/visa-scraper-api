"""
fetch_apis.py – Generischer API Fetcher
FastAPI Router deployed on Railway.app

Liest Regeln aus config_apis + Länderliste aus smart_country_data.
Schreibt ALLE Werte in die Staging-Tabelle api_values (NICHT mehr direkt in die
SSOT). Die DB-Funktion fl_sync_api_values() übernimmt kontrolliert mit
Quellen-Priorität (API > Gemini > manuell) nach smart_country_data und setzt
_source/_date. Der Endpoint ruft den Sync am Ende selbst (sync_after, default true).

Provider (Format-basiert — neue Quelle = reine config_apis-Zeile, KEIN Deploy):
  Generisch:
    - json        JSON  + response_path (Pfad-DSL)        → number | text | bool
    - sdmx_json   SDMX-JSON (feste Struktur)              → number        (Eurostat, OECD)
    - csv         CSV   + response_path (Spaltenname)      → number | text
    - xml         XML   + response_path (XPath)            → text  | bool  (eCFR, legislation.uk)
  Legacy (unverändert, speziell):
    - worldbank, worldbank_ratio, bls, oecd, eurostat, statcan, ons

config_apis-Spalten (neu ab v5):
    value_type     number|text|bool   (bei text/bool keine USD-Transform)
    response_path  wo der Wert steht   (json: Pfad-DSL, csv: Spalte, xml: XPath)
    divisor        parametrische Lineartransform: (value/divisor)*multiplier_pct+offset

Platzhalter in endpoint_url: {iso2} {iso3} {wb} {country_code} {iso2_lower} {iso3_lower}

v5.0.0 – 2026-10-04
  NEU: 4 generische Format-Handler (json/sdmx_json/csv/xml). Neue Quelle wird
       damit zur reinen config_apis-Zeile, ohne Deploy.
  NEU: value_type (number/text/bool) — Text/Bool werden ohne USD-Umrechnung
       gestaged; Bool wird aus gängigen Wahr/Falsch-Strings gemappt.
  NEU: divisor — parametrische Lineartransform, ersetzt die benannten
       transformation-Strings im Normalfall.
  NEU: ISO2→ISO3-Mapping als Konstante (UNHCR/WB u.a. brauchen iso3), keine
       neue Dependency.
  ÄNDERUNG (Write-Pfad): schreibt in Staging-Tabelle api_values statt direkt in
       smart_country_data. _source trägt jetzt die ECHTE Abruf-URL (Weg B),
       konsistent mit dem Gemini-Pfad, statt eines Sammel-Platzhalters.
  ÄNDERUNG: target_table-Filter entfernt — der Fetcher verarbeitet jetzt ALLE
       aktiven config_apis-Zeilen, nicht nur GROUP B. (Vorher erreichte keine
       Quelle außerhalb data_group_b_finanzen den Fetcher.)
  ÄNDERUNG: Endpoint ruft nach dem Lauf fl_sync_api_values() (sync_after).
  UNVERÄNDERT: Multi-Pass-WB-Drossel-Recovery, Pro-Land-Timeout, nicht-
       blockierender Upsert, Daten-Erhalt (fehlender Wert = kein Staging-Write =
       Altwert bleibt), alle Legacy-Fetcher/Transformer.

--- frühere Versionen (Zahlen-Pfad, weiterhin gültig) ---
v4.2.1 worldbank_ratio (series_id "NUM/DENOM"); v4.2.0 pli_anchor;
v4.1.3 WB-Multi-Pass-Recovery; v4.1.2 mrnev=1; v4.1.1 Pro-Land-/Upsert-Timeout +
Semaphore; v4.1.0 Länderquelle smart_country_data; v4.0.1 Daten-Erhalt + NOK/DKK;
v4.0.0 Direkt-Write smart_country_data.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional, List, Any
import httpx
import asyncio
import logging
import os
import io
import csv
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

router = APIRouter()

# =============================================================================
# SUPABASE CONNECTION
# =============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# HÄRTUNGS-PARAMETER (v4.1.1 / v4.1.3) — unverändert
# =============================================================================

COUNTRY_TIMEOUT = 90.0
UPSERT_TIMEOUT = 20.0
MAX_CONCURRENT_FETCHES = 4
MAX_PASSES = 5
PASS_COOLDOWN_SEC = 30

# Staging-Ziel
STAGING_TABLE = "api_values"
SYNC_FUNCTION = "fl_sync_api_values"

INTERNAL_FIELDS = {
    'rule_id', 'source_url', 'url_id', 'extraction_quality',
    'raw_extraction_json', 'confidence_score', 'updated_at',
}

# Bool-Mapping für value_type='bool'
BOOL_TRUE = {"true", "yes", "y", "1", "available", "allowed", "eligible",
             "permitted", "ja", "wahr", "t"}
BOOL_FALSE = {"false", "no", "n", "0", "unavailable", "not available",
              "not allowed", "none", "nein", "falsch", "f"}

# =============================================================================
# ISO2 → ISO3 (UNHCR/World Bank u.a. nutzen Alpha-3). Konstante statt Dependency.
# =============================================================================

ISO2_TO_ISO3 = {
    "AF":"AFG","AL":"ALB","DZ":"DZA","AD":"AND","AO":"AGO","AG":"ATG","AR":"ARG",
    "AM":"ARM","AU":"AUS","AT":"AUT","AZ":"AZE","BS":"BHS","BH":"BHR","BD":"BGD",
    "BB":"BRB","BY":"BLR","BE":"BEL","BZ":"BLZ","BJ":"BEN","BT":"BTN","BO":"BOL",
    "BA":"BIH","BW":"BWA","BR":"BRA","BN":"BRN","BG":"BGR","BF":"BFA","BI":"BDI",
    "CV":"CPV","KH":"KHM","CM":"CMR","CA":"CAN","CF":"CAF","TD":"TCD","CL":"CHL",
    "CN":"CHN","CO":"COL","KM":"COM","CG":"COG","CD":"COD","CR":"CRI","CI":"CIV",
    "HR":"HRV","CU":"CUB","CY":"CYP","CZ":"CZE","DK":"DNK","DJ":"DJI","DM":"DMA",
    "DO":"DOM","EC":"ECU","EG":"EGY","SV":"SLV","GQ":"GNQ","ER":"ERI","EE":"EST",
    "SZ":"SWZ","ET":"ETH","FJ":"FJI","FI":"FIN","FR":"FRA","GA":"GAB","GM":"GMB",
    "GE":"GEO","DE":"DEU","GH":"GHA","GR":"GRC","GD":"GRD","GT":"GTM","GN":"GIN",
    "GW":"GNB","GY":"GUY","HT":"HTI","HN":"HND","HK":"HKG","HU":"HUN","IS":"ISL",
    "IN":"IND","ID":"IDN","IR":"IRN","IQ":"IRQ","IE":"IRL","IL":"ISR","IT":"ITA",
    "JM":"JAM","JP":"JPN","JO":"JOR","KZ":"KAZ","KE":"KEN","KI":"KIR","KP":"PRK",
    "KR":"KOR","KW":"KWT","KG":"KGZ","LA":"LAO","LV":"LVA","LB":"LBN","LS":"LSO",
    "LR":"LBR","LY":"LBY","LI":"LIE","LT":"LTU","LU":"LUX","MO":"MAC","MG":"MDG",
    "MW":"MWI","MY":"MYS","MV":"MDV","ML":"MLI","MT":"MLT","MH":"MHL","MR":"MRT",
    "MU":"MUS","MX":"MEX","FM":"FSM","MD":"MDA","MC":"MCO","MN":"MNG","ME":"MNE",
    "MA":"MAR","MZ":"MOZ","MM":"MMR","NA":"NAM","NR":"NRU","NP":"NPL","NL":"NLD",
    "NZ":"NZL","NI":"NIC","NE":"NER","NG":"NGA","MK":"MKD","NO":"NOR","OM":"OMN",
    "PK":"PAK","PW":"PLW","PS":"PSE","PA":"PAN","PG":"PNG","PY":"PRY","PE":"PER",
    "PH":"PHL","PL":"POL","PT":"PRT","QA":"QAT","RO":"ROU","RU":"RUS","RW":"RWA",
    "KN":"KNA","LC":"LCA","VC":"VCT","WS":"WSM","SM":"SMR","ST":"STP","SA":"SAU",
    "SN":"SEN","RS":"SRB","SC":"SYC","SL":"SLE","SG":"SGP","SK":"SVK","SI":"SVN",
    "SB":"SLB","SO":"SOM","ZA":"ZAF","SS":"SSD","ES":"ESP","LK":"LKA","SD":"SDN",
    "SR":"SUR","SE":"SWE","CH":"CHE","SY":"SYR","TW":"TWN","TJ":"TJK","TZ":"TZA",
    "TH":"THA","TL":"TLS","TG":"TGO","TO":"TON","TT":"TTO","TN":"TUN","TR":"TUR",
    "TM":"TKM","TV":"TUV","UG":"UGA","UA":"UKR","AE":"ARE","GB":"GBR","US":"USA",
    "UY":"URY","UZ":"UZB","VU":"VUT","VE":"VEN","VN":"VNM","YE":"YEM","ZM":"ZMB",
    "ZW":"ZWE","XK":"XKX",
}

EXCHANGE_RATES_TO_USD = {
    "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.65, "SEK": 0.095,
    "PLN": 0.25, "CHF": 1.12, "RUB": 0.011, "SAR": 0.267, "AED": 0.272,
    "NOK": 0.094, "DKK": 0.145, "USD": 1.0,
}

CPI_BASE_VALUES = {
    "AU": {"cost_transport_month_tier1_avg_usd_num": 95.0,  "cost_utility_month_avg_usd_num": 160.0},
    "CA": {"cost_transport_month_tier1_avg_usd_num": 100.0, "cost_utility_month_avg_usd_num": 145.0},
    "DE": {"cost_transport_month_tier1_avg_usd_num": 55.0,  "cost_utility_month_avg_usd_num": 290.0},
    "ES": {"cost_transport_month_tier1_avg_usd_num": 57.0,  "cost_utility_month_avg_usd_num": 148.0},
    "FR": {"cost_transport_month_tier1_avg_usd_num": 90.0,  "cost_utility_month_avg_usd_num": 185.0},
    "GB": {"cost_transport_month_tier1_avg_usd_num": 200.0, "cost_utility_month_avg_usd_num": 320.0},
    "AT": {"cost_transport_month_tier1_avg_usd_num": 60.0,  "cost_utility_month_avg_usd_num": 180.0},
    "IT": {"cost_transport_month_tier1_avg_usd_num": 40.0,  "cost_utility_month_avg_usd_num": 200.0},
    "NL": {"cost_transport_month_tier1_avg_usd_num": 110.0, "cost_utility_month_avg_usd_num": 210.0},
    "PL": {"cost_transport_month_tier1_avg_usd_num": 25.0,  "cost_utility_month_avg_usd_num": 110.0},
    "PT": {"cost_transport_month_tier1_avg_usd_num": 45.0,  "cost_utility_month_avg_usd_num": 120.0},
    "SE": {"cost_transport_month_tier1_avg_usd_num": 80.0,  "cost_utility_month_avg_usd_num": 100.0},
}


# =============================================================================
# WÄHRUNGSUMRECHNUNG (unverändert)
# =============================================================================

async def fetch_exchange_rates(client: httpx.AsyncClient) -> dict:
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        r = await client.get(url, timeout=10.0)
        data = r.json()
        if data.get("result") == "success":
            rates = data.get("rates", {})
            usd_rates = {c: 1.0 / rate for c, rate in rates.items() if rate and rate > 0}
            logger.info(f"✅ Wechselkurse aktualisiert ({len(usd_rates)} Währungen)")
            return usd_rates
    except Exception as e:
        logger.warning(f"⚠️ Wechselkurs-Fetch fehlgeschlagen: {e} – nutze Fallback-Werte")
    return EXCHANGE_RATES_TO_USD


def convert_to_usd(value: float, currency: str, rates: dict) -> float:
    rate = rates.get(currency, EXCHANGE_RATES_TO_USD.get(currency, 1.0))
    return round(value * rate, 2)


# =============================================================================
# TRANSFORMATION ENGINE (unverändert + divisor)
# =============================================================================

def apply_transformation(
    value: float,
    transformation: str,
    multiplier_pct: Optional[float] = None,
    offset_usd: Optional[float] = None,
    country_code: Optional[str] = None,
    db_field: Optional[str] = None,
    currency: Optional[str] = None,
    exchange_rates: Optional[dict] = None,
    divisor: Optional[float] = None,
) -> Optional[float]:
    if value is None:
        return None

    rates = exchange_rates or EXCHANGE_RATES_TO_USD

    try:
        # v5: parametrische Lineartransform hat Vorrang vor den benannten Strings.
        # (value / divisor) * multiplier_pct + offset_usd
        if divisor is not None:
            d = float(divisor)
            if d == 0:
                logger.warning("⚠️ divisor=0 — übersprungen")
                return None
            result = (value / d) * float(multiplier_pct if multiplier_pct is not None else 1.0)
            if offset_usd:
                result += float(offset_usd)
            return round(result, 2)

        t = (transformation or "").strip().lower()

        if t == "pli_anchor":
            result = value * float(multiplier_pct if multiplier_pct is not None else 1.0)
            if offset_usd:
                result += float(offset_usd)
            return round(result, 2)

        if multiplier_pct is not None and t not in (
            "cpi_index_to_usd_convert",
            "kwh_price_multiply_250_plus_30pct_convert_usd",
            "cpi_transport_index_to_usd_convert",
            "pli_anchor",
        ):
            result = (value / 12) * float(multiplier_pct)
            if offset_usd:
                result += float(offset_usd)
            return round(result, 2)

        if t in ("cpi_index_to_usd_convert", "cpi_transport_index_to_usd_convert"):
            if not country_code or not db_field:
                logger.warning("⚠️ cpi_index_to_usd_convert braucht country_code + db_field")
                return None
            base = CPI_BASE_VALUES.get(country_code, {}).get(db_field)
            if base is None:
                logger.warning(f"⚠️ Kein CPI-Basiswert für {country_code}/{db_field}")
                return None
            return round(base * (value / 100.0), 2)

        if t == "kwh_price_multiply_250_plus_30pct_convert_usd":
            monthly_total = (value * 250) * 1.30
            return round(convert_to_usd(monthly_total, currency or "EUR", rates), 2)

        if t == "divide_by_12":
            return round(value / 12, 2)
        elif t == "divide_by_12_multiply_1.3":
            return round((value / 12) * 1.3, 2)
        elif t == "divide_by_12_multiply_0.7":
            return round((value / 12) * 0.7, 2)
        elif t == "divide_by_12_multiply_0.15":
            return round((value / 12) * 0.15, 2)
        elif t == "divide_by_12_multiply_0.20":
            return round((value / 12) * 0.20, 2)
        elif t in ("none", ""):
            return round(value, 2)
        else:
            logger.warning(f"⚠️ Unbekannte Transformation: {transformation} – nutze /12")
            return round(value / 12, 2)

    except Exception as e:
        logger.error(f"❌ Transformation error ({transformation}): {e}")
        return None


# =============================================================================
# LEGACY FETCHER (unverändert)
# =============================================================================

async def fetch_worldbank_value(worldbank_id: str, series_id: str, client: httpx.AsyncClient) -> Optional[float]:
    url = f"https://api.worldbank.org/v2/country/{worldbank_id}/indicator/{series_id}"
    params = {"format": "json", "mrnev": 1, "per_page": 5}
    try:
        response = await client.get(url, params=params, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        if not data or len(data) < 2 or not data[1]:
            return None
        for record in data[1]:
            if record.get("value") is not None:
                return float(record["value"])
        return None
    except Exception as e:
        logger.warning(f"⚠️ World Bank fetch failed [{series_id}] for {worldbank_id}: {e}")
        raise


async def fetch_bls_value(series_id: str, client: httpx.AsyncClient) -> Optional[float]:
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = {"seriesid": [series_id], "startyear": str(date.today().year - 3), "endyear": str(date.today().year)}
    try:
        response = await client.post(url, json=payload, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            logger.warning(f"⚠️ BLS API error for {series_id}: {data.get('message')}")
            return None
        series_data = data.get("Results", {}).get("series", [])
        if not series_data:
            return None
        for item in series_data[0].get("data", []):
            if item.get("period") == "M13":
                return float(item["value"])
        items = series_data[0].get("data", [])
        if items:
            return float(items[0]["value"])
        return None
    except Exception as e:
        logger.warning(f"⚠️ BLS fetch failed [{series_id}]: {e}")
        return None


def _sdmx_last_value(data: dict) -> Optional[float]:
    """Gemeinsame SDMX-JSON-Extraktion (OECD/Eurostat): letzter Observation-Wert."""
    datasets = data.get("dataSets", [])
    if not datasets:
        return None
    observations = datasets[0].get("observations", {})
    if not observations:
        return None
    values = [float(v[0]) for v in observations.values() if v and v[0] is not None]
    return values[-1] if values else None


async def fetch_oecd_value(endpoint_url: str, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0,
                             headers={"Accept": "application/vnd.sdmx.data+json;version=1.0"})
        r.raise_for_status()
        return _sdmx_last_value(r.json())
    except Exception as e:
        logger.warning(f"⚠️ OECD fetch failed für {endpoint_url}: {e}")
        return None


async def fetch_eurostat_value(endpoint_url: str, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        return _sdmx_last_value(r.json())
    except Exception as e:
        logger.warning(f"⚠️ Eurostat fetch failed für {endpoint_url}: {e}")
        return None


async def fetch_statcan_value(endpoint_url: str, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=30.0)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if not rows:
            return None
        for row in reversed(rows):
            val = row.get("VALUE") or row.get("value") or row.get("Value")
            if val and val.strip() not in ("", "."):
                try:
                    return float(val.strip())
                except ValueError:
                    continue
        return None
    except Exception as e:
        logger.warning(f"⚠️ StatCan fetch failed für {endpoint_url}: {e}")
        return None


async def fetch_ons_value(endpoint_url: str, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        observations = r.json().get("observations", [])
        if not observations:
            return None
        for obs in reversed(observations):
            val = obs.get("observation")
            if val and val not in ("", ".", "N/A"):
                try:
                    return float(val)
                except ValueError:
                    continue
        return None
    except Exception as e:
        logger.warning(f"⚠️ ONS fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# GENERISCHE FETCHER (v5) — Format-basiert, neue Quelle = config-Zeile
# =============================================================================

def resolve_endpoint_url(rule: dict, country: dict) -> str:
    """Platzhalter in endpoint_url auflösen: {iso2} {iso3} {wb} {country_code} ..."""
    url = rule.get("endpoint_url") or ""
    if not url:
        return url
    cc = country["country_code"]
    iso3 = ISO2_TO_ISO3.get(cc, cc)
    subs = {
        "iso2": cc, "country_code": cc, "iso": cc, "wb": cc,
        "iso3": iso3, "iso2_lower": cc.lower(), "iso3_lower": iso3.lower(),
    }
    try:
        return url.format(**subs)
    except Exception as e:
        logger.warning(f"⚠️ URL-Platzhalter nicht auflösbar ({url}): {e}")
        return url


def _json_resolve(node: Any, segs: List[str]) -> List[Any]:
    """Pfad-Segmente rekursiv auflösen. '*' = alle Listen-/Dict-Elemente."""
    if not segs:
        return [node]
    seg, rest = segs[0], segs[1:]
    if seg == "*":
        if isinstance(node, list):
            out = []
            for item in node:
                out += _json_resolve(item, rest)
            return out
        if isinstance(node, dict):
            out = []
            for v in node.values():
                out += _json_resolve(v, rest)
            return out
        return []
    if seg.lstrip("-").isdigit():
        idx = int(seg)
        if isinstance(node, list) and -len(node) <= idx < len(node):
            return _json_resolve(node[idx], rest)
        return []
    if isinstance(node, dict) and seg in node:
        return _json_resolve(node[seg], rest)
    return []


def json_path_extract(data: Any, path: Optional[str]) -> Any:
    """
    Mini-DSL: Segmente per '.', Wildcard '*', optionaler Modifier nach '|'.
    Modifier: first | last | first_non_null | last_non_null | sum | max | min
    Ohne Modifier: genau 1 Treffer → Wert; mehrere → last_non_null.
    Beispiele:
      '1.*.value|last_non_null'   World Bank JSON
      'details.body'              GOV.UK Content API
      'data.0.population'         einfacher verschachtelter Wert
    """
    if not path:
        return data
    expr, _, modifier = path.partition("|")
    modifier = modifier.strip().lower()
    segments = [s for s in expr.strip().split(".") if s != ""]
    results = _json_resolve(data, segments)
    if not results:
        return None

    non_null = [r for r in results if r is not None]

    if modifier == "first":
        return results[0]
    if modifier == "last":
        return results[-1]
    if modifier == "first_non_null":
        return non_null[0] if non_null else None
    if modifier == "last_non_null":
        return non_null[-1] if non_null else None
    if modifier in ("sum", "max", "min"):
        nums = []
        for r in non_null:
            try:
                nums.append(float(r))
            except (TypeError, ValueError):
                continue
        if not nums:
            return None
        return {"sum": sum, "max": max, "min": min}[modifier](nums)

    # kein Modifier
    if len(results) == 1:
        return results[0]
    return non_null[-1] if non_null else None


async def fetch_json_value(url: str, response_path: Optional[str], client: httpx.AsyncClient) -> Any:
    try:
        r = await client.get(url, timeout=20.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        return json_path_extract(r.json(), response_path)
    except Exception as e:
        # transienter Fehler (429/Timeout) → raise für Multi-Pass; leerer Pfad → None oben
        logger.warning(f"⚠️ json fetch failed für {url}: {e}")
        raise


async def fetch_sdmx_json_value(url: str, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(url, timeout=20.0,
                             headers={"Accept": "application/vnd.sdmx.data+json, application/json"})
        r.raise_for_status()
        return _sdmx_last_value(r.json())
    except Exception as e:
        logger.warning(f"⚠️ sdmx_json fetch failed für {url}: {e}")
        raise


def _csv_get(row: dict, col: str) -> Optional[str]:
    if col in row:
        return row[col]
    low = {k.lower(): v for k, v in row.items()}
    return low.get(col.lower())


async def fetch_csv_value(url: str, response_path: Optional[str], client: httpx.AsyncClient) -> Any:
    try:
        r = await client.get(url, timeout=30.0)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if not rows:
            return None
        col, _, mod = (response_path or "").partition("|")
        col = col.strip()
        mod = (mod.strip() or "last").lower()
        candidates = [col] if col else ["VALUE", "value", "Value"]
        seq = rows if mod == "first" else list(reversed(rows))
        for row in seq:
            for c in candidates:
                v = _csv_get(row, c)
                if v is not None and str(v).strip() not in ("", ".", "N/A"):
                    return str(v).strip()
        return None
    except Exception as e:
        logger.warning(f"⚠️ csv fetch failed für {url}: {e}")
        raise


async def fetch_xml_value(url: str, response_path: Optional[str], client: httpx.AsyncClient) -> Any:
    """
    response_path = XPath (ElementTree-Subset). Namespaces: {ns}tag-Notation
    bzw. '//{*}tag' für Namespace-agnostisch. Gibt den Text des ersten Treffers.
    """
    try:
        r = await client.get(url, timeout=30.0, headers={"Accept": "application/xml, text/xml"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        if not response_path:
            return None
        el = root.find(response_path)
        if el is None:
            found = root.findall(response_path)
            el = found[0] if found else None
        if el is None:
            return None
        # Attribut-Zugriff: XPath endet auf .../@attr → ElementTree kann das via findall nicht,
        # daher hier nur Element-Text. Für Attribute den Pfad auf das Element legen.
        text = (el.text or "").strip()
        return text or None
    except Exception as e:
        logger.warning(f"⚠️ xml fetch failed für {url}: {e}")
        raise


# =============================================================================
# PROVIDER ROUTER
# =============================================================================

async def fetch_value_for_rule(rule: dict, country: dict, client: httpx.AsyncClient) -> Any:
    provider = rule["provider"].lower()
    endpoint_url = rule.get("endpoint_url", "")

    # --- Legacy ---
    if provider == "worldbank":
        wb = country.get("worldbank_id") or country["iso2"]
        return await fetch_worldbank_value(wb, rule["series_id"], client)

    elif provider == "worldbank_ratio":
        wb = country.get("worldbank_id") or country["iso2"]
        parts = [p.strip() for p in str(rule["series_id"]).split("/") if p.strip()]
        if len(parts) != 2:
            logger.warning(f"⚠️ worldbank_ratio braucht 'NUM/DENOM', hat '{rule['series_id']}'")
            return None
        num = await fetch_worldbank_value(wb, parts[0], client)
        den = await fetch_worldbank_value(wb, parts[1], client)
        if num is None or den is None or den == 0:
            return None
        return num / den

    elif provider == "bls":
        if not country.get("bls_available"):
            return None
        return await fetch_bls_value(rule["series_id"], client)

    elif provider == "oecd":
        return await fetch_oecd_value(endpoint_url, client)

    elif provider == "eurostat":
        return await fetch_eurostat_value(endpoint_url, client)

    elif provider == "statcan":
        if country.get("country_code") != "CA":
            return None
        return await fetch_statcan_value(endpoint_url, client)

    elif provider == "ons":
        if country.get("country_code") != "GB":
            return None
        return await fetch_ons_value(endpoint_url, client)

    # --- Generisch (v5) ---
    elif provider == "json":
        return await fetch_json_value(resolve_endpoint_url(rule, country), rule.get("response_path"), client)

    elif provider == "sdmx_json":
        return await fetch_sdmx_json_value(resolve_endpoint_url(rule, country), client)

    elif provider == "csv":
        return await fetch_csv_value(resolve_endpoint_url(rule, country), rule.get("response_path"), client)

    elif provider == "xml":
        return await fetch_xml_value(resolve_endpoint_url(rule, country), rule.get("response_path"), client)

    else:
        logger.warning(f"⚠️ Unbekannter Provider: {provider} für {rule['api_id']}")
        return None


def resolve_source_url(rule: dict, country: dict) -> str:
    """Echte Abruf-URL fürs _source (Weg B). Pro Provider die tatsächliche URL."""
    provider = rule["provider"].lower()
    cc = country["country_code"]
    if provider == "worldbank":
        wb = country.get("worldbank_id") or cc
        return f"https://api.worldbank.org/v2/country/{wb}/indicator/{rule['series_id']}?format=json&mrnev=1"
    if provider == "worldbank_ratio":
        wb = country.get("worldbank_id") or cc
        base = f"https://api.worldbank.org/v2/country/{wb}/indicator/"
        parts = [p.strip() for p in str(rule["series_id"]).split("/") if p.strip()]
        return " / ".join(base + p for p in parts)
    if provider == "bls":
        return f"https://api.bls.gov/publicAPI/v2/timeseries/data/ (series {rule['series_id']})"
    resolved = resolve_endpoint_url(rule, country)
    return resolved or rule.get("endpoint_url") or (rule.get("source_label") or "")


# =============================================================================
# WÄHRUNG PRO PROVIDER/LAND (unverändert)
# =============================================================================

def get_currency_for_rule(rule: dict, country: dict) -> str:
    provider = rule["provider"].lower()
    country_code = country.get("country_code", "")
    if provider in ("bls", "worldbank", "worldbank_ratio"):
        return "USD"
    currency_map = {
        "AU": "AUD", "CA": "CAD", "GB": "GBP", "US": "USD", "RU": "RUB",
        "SA": "SAR", "AE": "AED", "DE": "EUR", "FR": "EUR", "ES": "EUR",
        "IT": "EUR", "AT": "EUR", "NL": "EUR", "PT": "EUR", "BE": "EUR",
        "FI": "EUR", "IE": "EUR", "GR": "EUR", "SE": "SEK", "PL": "PLN",
        "CH": "CHF", "NO": "NOK", "DK": "DKK",
    }
    return currency_map.get(country_code, "USD")


# =============================================================================
# WERT-NORMALISIERUNG nach value_type → Staging-Text
# =============================================================================

def coerce_bool(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if s in BOOL_TRUE:
        return True
    if s in BOOL_FALSE:
        return False
    return None


def build_staging_value(
    rule: dict, raw_value: Any, country: dict, exchange_rates: dict
) -> Optional[str]:
    """Rohwert → Staging-Text je value_type. None = überspringen (Altwert bleibt)."""
    vt = (rule.get("value_type") or "number").lower()
    db_field = rule["db_field"]

    if vt == "number":
        try:
            num_in = float(raw_value)
        except (TypeError, ValueError):
            logger.warning(f"⚠️ {db_field}: '{raw_value}' nicht numerisch — übersprungen")
            return None
        transformed = apply_transformation(
            num_in,
            rule.get("transformation", "none"),
            multiplier_pct=rule.get("multiplier_pct"),
            offset_usd=rule.get("offset_usd"),
            country_code=country["country_code"],
            db_field=db_field,
            currency=get_currency_for_rule(rule, country),
            exchange_rates=exchange_rates,
            divisor=rule.get("divisor"),
        )
        if transformed is None:
            return None
        return repr(round(float(transformed), 2))

    if vt == "bool":
        b = coerce_bool(raw_value)
        if b is None:
            logger.warning(f"⚠️ {db_field}: '{raw_value}' nicht als bool mappbar — übersprungen")
            return None
        return "true" if b else "false"

    # text
    s = str(raw_value).strip()
    return s or None


# =============================================================================
# CORE: Verarbeitung eines einzelnen Landes → schreibt in api_values (Staging)
# =============================================================================

async def process_country(country: dict, api_rules: List[dict], exchange_rates: dict) -> dict:
    country_code = country["country_code"]
    country_name = country["country_name"]
    now_iso = datetime.now(timezone.utc).isoformat()

    relevant_rules = [
        r for r in api_rules
        if r["country_iso"] is None or r["country_iso"] == country_code
    ]
    if not relevant_rules:
        return {"country_code": country_code, "success": True, "fields_written": 0, "had_error": False}

    logger.info(f"🌍 Verarbeite {country_name} ({country_code}) – {len(relevant_rules)} Regeln")

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True,
        headers={"User-Agent": "Flootloop-fetch-apis/5.0"},
    ) as client:
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def _bounded_fetch(rule):
            async with sem:
                return await fetch_value_for_rule(rule, country, client)

        raw_values = await asyncio.gather(*[_bounded_fetch(r) for r in relevant_rules],
                                          return_exceptions=True)

    # v4.1.3: nur echte Fetch-Fehler (nicht leere Daten) lösen einen Retry-Pass aus
    had_error = any(isinstance(rv, Exception) for rv in raw_values)

    staging_rows = []
    fields_skipped = 0

    for rule, raw_value in zip(relevant_rules, raw_values):
        db_field = rule["db_field"]

        if isinstance(raw_value, Exception):
            logger.warning(f"⚠️ Exception für {rule['api_id']}: {raw_value} — {db_field} übersprungen (Altwert bleibt)")
            fields_skipped += 1
            continue
        if raw_value is None:
            fields_skipped += 1
            continue

        staging_val = build_staging_value(rule, raw_value, country, exchange_rates)
        if staging_val is None:
            fields_skipped += 1
            continue

        src_url = resolve_source_url(rule, country)
        staging_rows.append({
            "country_code": country_code,
            "db_field": db_field,
            "value": staging_val,
            "value_type": (rule.get("value_type") or "number").lower(),
            "source_label": src_url,       # Weg B: echte URL ins spätere _source
            "source_channel": "apis",
            "source_ref": src_url,
            "fetched_at": now_iso,
            "synced_at": None,
        })
        logger.info(f"  ✅ staged {db_field} = {staging_val} (provider: {rule['provider']})")

    if not staging_rows:
        logger.info(f"⏭️ {country_name}: keine neuen Werte — nichts gestaged")
        return {"country_code": country_code, "country_name": country_name,
                "success": True, "fields_written": 0, "fields_skipped": fields_skipped,
                "had_error": had_error}

    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: supabase.table(STAGING_TABLE).upsert(
                    staging_rows, on_conflict="country_code,db_field"
                ).execute()
            ),
            timeout=UPSERT_TIMEOUT,
        )
        logger.info(f"✅ {country_name}: {len(staging_rows)} Werte → {STAGING_TABLE} "
                    f"({fields_skipped} übersprungen)")
        return {"country_code": country_code, "country_name": country_name,
                "success": True, "fields_written": len(staging_rows),
                "fields_skipped": fields_skipped, "had_error": had_error}
    except asyncio.TimeoutError:
        logger.error(f"⏱️ Staging-Upsert Timeout ({UPSERT_TIMEOUT}s) für {country_code}")
        return {"country_code": country_code, "country_name": country_name,
                "success": False, "error": "staging upsert timeout", "had_error": True}
    except Exception as e:
        logger.error(f"❌ Staging-Upsert fehlgeschlagen für {country_code}: {e}")
        return {"country_code": country_code, "country_name": country_name,
                "success": False, "error": str(e), "had_error": True}


# =============================================================================
# API ENDPOINT
# =============================================================================

class FetchApisRequest(BaseModel):
    country_codes: Optional[List[str]] = None
    fetch_all_active: Optional[bool] = False
    sync_after: Optional[bool] = True   # nach dem Lauf fl_sync_api_values() aufrufen


@router.post("/fetch-apis")
async def fetch_apis(request: FetchApisRequest):
    """
    Holt API-Daten laut config_apis und schreibt sie in die Staging-Tabelle
    api_values. Danach (sync_after=true) wird fl_sync_api_values() aufgerufen,
    das kontrolliert nach smart_country_data übernimmt (API > Gemini > manuell).

    POST /fetch-apis
      { "country_codes": ["US","DE"] }            spezifische Länder
      { "fetch_all_active": true }                alle Länder
      { "fetch_all_active": true, "sync_after": false }   nur stagen, Sync separat
    """
    if not request.fetch_all_active and not request.country_codes:
        return {"success": False, "error": "Provide either 'country_codes' or 'fetch_all_active': true"}

    async with httpx.AsyncClient(timeout=10.0) as fx_client:
        exchange_rates = await fetch_exchange_rates(fx_client)

    # Länder aus smart_country_data (SSOT, v4.1.0)
    try:
        query = supabase.table("smart_country_data").select("country_code, country_name")
        if request.country_codes:
            query = query.in_("country_code", request.country_codes)
        scd_resp = query.execute()
        if not scd_resp.data:
            return {"success": False, "error": "Keine Länder in smart_country_data gefunden"}

        countries, seen = [], set()
        for r in scd_resp.data:
            cc = (r.get("country_code") or "").strip()
            if not cc or cc in seen:
                continue
            seen.add(cc)
            countries.append({
                "country_code": cc,
                "country_name": r.get("country_name") or cc,
                "iso2": cc, "worldbank_id": cc,
                "bls_available": cc == "US",
            })
        if not countries:
            return {"success": False, "error": "Keine Länder nach Filterung übrig"}
        logger.info(f"📋 {len(countries)} Länder aus smart_country_data")
    except Exception as e:
        logger.error(f"❌ Länder-Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # API-Regeln: ALLE aktiven (v5: kein target_table-Filter mehr)
    try:
        rules_resp = supabase.table("config_apis").select("*").eq("active", True).execute()
        api_rules = rules_resp.data or []
        if not api_rules:
            return {"success": False, "error": "Keine aktiven API-Regeln in config_apis"}
        logger.info(f"📋 {len(api_rules)} aktive API-Regeln geladen")
    except Exception as e:
        logger.error(f"❌ config_apis Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # Multi-Pass gegen WB-Drosselung (v4.1.3) — Pro-Land-Timeout (v4.1.1 FIX A)
    async def _run_country(country):
        cc = country["country_code"]
        try:
            return await asyncio.wait_for(process_country(country, api_rules, exchange_rates),
                                         timeout=COUNTRY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"⏱️ {cc}: {COUNTRY_TIMEOUT}s-Budget überschritten — übersprungen")
            return {"country_code": cc, "success": False, "error": "country timeout", "had_error": True}
        except Exception as e:
            logger.error(f"❌ {cc}: unerwarteter Fehler — {e}")
            return {"country_code": cc, "success": False, "error": str(e), "had_error": True}

    results_by_cc, pending = {}, list(countries)
    for pass_num in range(1, MAX_PASSES + 1):
        if not pending:
            break
        if pass_num > 1:
            logger.info(f"🔁 Pass {pass_num}/{MAX_PASSES}: {len(pending)} Länder mit Fehler erneut "
                        f"(Cooldown {PASS_COOLDOWN_SEC}s)")
            await asyncio.sleep(PASS_COOLDOWN_SEC)
        retry_next = []
        for country in pending:
            result = await _run_country(country)
            results_by_cc[country["country_code"]] = result
            if result.get("had_error"):
                retry_next.append(country)
        pending = retry_next

    results = list(results_by_cc.values())
    still_throttled = len(pending)
    successful = sum(1 for r in results if r.get("success"))
    total_fields = sum(r.get("fields_written", 0) for r in results)

    # Kontrollierter Sync Staging → smart_country_data
    sync_result = None
    if request.sync_after:
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(lambda: supabase.rpc(SYNC_FUNCTION).execute()),
                timeout=UPSERT_TIMEOUT * 3,
            )
            sync_result = resp.data
            logger.info(f"🔄 {SYNC_FUNCTION}(): {sync_result}")
        except Exception as e:
            logger.error(f"❌ {SYNC_FUNCTION}() fehlgeschlagen: {e}")
            sync_result = {"error": str(e)}

    logger.info(f"🏁 fetch-apis v5.0.0: {successful}/{len(results)} Länder, "
                f"{total_fields} Werte gestaged, {still_throttled} nach {MAX_PASSES} Pässen mit Fehler")

    return {
        "success": True,
        "version": "5.0.0",
        "total_countries": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "total_fields_staged": total_fields,
        "still_throttled_after_passes": still_throttled,
        "synced": sync_result,
        "results": results,
    }
