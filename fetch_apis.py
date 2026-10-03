"""
fetch_apis.py – Generischer API Fetcher für GROUP B: FINANZEN
FastAPI Router deployed on Railway.app

Liest Regeln aus config_apis + Länderliste aus smart_country_data
Schreibt DIREKT in smart_country_data (Single Source of Truth)

Provider-Support:
  - World Bank  (global, Jahreswerte → /12)
  - BLS         (USA only, Jahreswerte → /12)
  - OECD SDMX   (AU, CA, DE, ES, FR, GB + weitere, CPI-Index)
  - Eurostat    (DE, ES, FR + EU-Länder, kWh-Preis → Utility)
  - StatCan     (CA, CPI CSV)
  - ONS         (GB, CPIH JSON)

v4.1.1 – 2026-10-03
  HÄRTUNG (Lauf kann nicht mehr einfrieren):
  FIX A (Pro-Land-Zeitbudget): jede Länder-Verarbeitung läuft unter
         asyncio.wait_for(timeout=COUNTRY_TIMEOUT). Hängt ein Land, wird es
         übersprungen (success=false, error=timeout) und der Lauf macht mit dem
         nächsten weiter. Vorher konnte ein einzelner hängender Aufruf den ganzen
         sequentiellen Lauf einfrieren (Zombie-Execution, Exec 9151).
  FIX B (Upsert nicht-blockierend): der synchrone Supabase-Upsert lief bisher im
         Event-Loop und konnte bei einem Netz-Stall unbegrenzt blockieren. Läuft
         jetzt via asyncio.to_thread + asyncio.wait_for(timeout=UPSERT_TIMEOUT).
  FIX C (WB-Drosselung): die Provider-Calls pro Land laufen jetzt über eine
         Semaphore (MAX_CONCURRENT_FETCHES). Die World Bank drosselt Bursts von
         ~9 gleichzeitigen Requests → vorher viele None-Werte (z.B. KZ: nur
         transport befüllt, utility/grocery/living leer).

v4.1.0 – 2026-10-03
  ÄNDERUNG (Reichweite): Länderliste kommt jetzt aus smart_country_data
         (Single Source of Truth, 126 Länder) statt aus config_rules (nur 48).
         Grund: fetch_all_active erreichte bisher nur die 48 config_rules-Länder;
         die restlichen ~78 Länder wurden nie verarbeitet, obwohl globale
         World-Bank-Template-Regeln (country_iso IS NULL) für ALLE Länder gelten.
         Reiner Quellen-Tausch der Länderschleife — Fetcher, Transformer und
         Provider-Logik bleiben unverändert.

v4.0.1 – 2026-06-13
  FIX 1 (Daten-Erhalt): Fehlgeschlagene Fetches (None / Exception / fehlgeschlagene
         Transformation) überschreiben vorhandene Werte NICHT mehr mit null.
         Das betroffene Feld wird einfach aus dem Upsert weggelassen — der zuletzt
         erfolgreich geholte Wert in smart_country_data bleibt erhalten.
         Vorher: ein kurzer API-Ausfall setzte gute Bestandsdaten auf null zurück
         und der trg_update_completeness-Trigger zählte die Completeness runter.
  FIX 2 (Fallback-Kurse): NOK und DKK zu EXCHANGE_RATES_TO_USD ergänzt. Vorher
         fielen sie bei Ausfall der Live-Wechselkurs-API auf Rate 1.0 zurück
         (≈10× zu hoch). Greift nur im Fallback-Pfad, aber jetzt korrekt.

v4.0.0 – 2026-06-06
  ÄNDERUNG: Schreibt jetzt direkt in smart_country_data statt data_group_b_finanzen
  ÄNDERUNG: Interne Felder (rule_id, source_url, url_id, extraction_quality,
            raw_extraction_json, confidence_score, updated_at) werden vor
            dem Upsert herausgefiltert
  Alle anderen Fetcher, Transformer und Provider-Logiken unverändert
"""

from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional, List
import httpx
import asyncio
import logging
import os
import io
import csv
from datetime import date

logger = logging.getLogger(__name__)

router = APIRouter()

# =============================================================================
# SUPABASE CONNECTION
# =============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# v4.1.1 HÄRTUNGS-PARAMETER
# =============================================================================

# Max. Sekunden pro Land (gesamt: alle Provider-Calls + Upsert). Danach wird das
# Land übersprungen und der Lauf macht weiter → kein Einfrieren mehr.
COUNTRY_TIMEOUT = 90.0
# Max. Sekunden für den Supabase-Upsert eines Landes.
UPSERT_TIMEOUT = 20.0
# Max. gleichzeitige Provider-Requests PRO LAND (WB drosselt sonst Bursts).
MAX_CONCURRENT_FETCHES = 4

# =============================================================================
# Interne Felder die NICHT in smart_country_data geschrieben werden
# =============================================================================

INTERNAL_FIELDS = {
    'rule_id',
    'source_url',
    'url_id',
    'extraction_quality',
    'raw_extraction_json',
    'confidence_score',
    'updated_at',
}

# =============================================================================
# WÄHRUNGS-BASISWERTE (Fallback falls ExchangeRate-API nicht erreichbar)
# Werden beim Start aktualisiert via fetch_exchange_rates()
# v4.0.1: NOK + DKK ergänzt (waren in currency_map, fehlten aber hier → Rate 1.0)
# =============================================================================

EXCHANGE_RATES_TO_USD = {
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "AUD": 0.65,
    "SEK": 0.095,
    "PLN": 0.25,
    "CHF": 1.12,
    "RUB": 0.011,
    "SAR": 0.267,
    "AED": 0.272,
    "NOK": 0.094,
    "DKK": 0.145,
    "USD": 1.0,
}

# CPI-Basiswerte für Index-zu-USD Umrechnung (2015=100 Basis)
CPI_BASE_VALUES = {
    "AU": {
        "cost_transport_month_tier1_avg_usd_num": 95.0,
        "cost_utility_month_avg_usd_num": 160.0,
    },
    "CA": {
        "cost_transport_month_tier1_avg_usd_num": 100.0,
        "cost_utility_month_avg_usd_num": 145.0,
    },
    "DE": {
        "cost_transport_month_tier1_avg_usd_num": 55.0,
        "cost_utility_month_avg_usd_num": 290.0,
    },
    "ES": {
        "cost_transport_month_tier1_avg_usd_num": 57.0,
        "cost_utility_month_avg_usd_num": 148.0,
    },
    "FR": {
        "cost_transport_month_tier1_avg_usd_num": 90.0,
        "cost_utility_month_avg_usd_num": 185.0,
    },
    "GB": {
        "cost_transport_month_tier1_avg_usd_num": 200.0,
        "cost_utility_month_avg_usd_num": 320.0,
    },
    "AT": {
        "cost_transport_month_tier1_avg_usd_num": 60.0,
        "cost_utility_month_avg_usd_num": 180.0,
    },
    "IT": {
        "cost_transport_month_tier1_avg_usd_num": 40.0,
        "cost_utility_month_avg_usd_num": 200.0,
    },
    "NL": {
        "cost_transport_month_tier1_avg_usd_num": 110.0,
        "cost_utility_month_avg_usd_num": 210.0,
    },
    "PL": {
        "cost_transport_month_tier1_avg_usd_num": 25.0,
        "cost_utility_month_avg_usd_num": 110.0,
    },
    "PT": {
        "cost_transport_month_tier1_avg_usd_num": 45.0,
        "cost_utility_month_avg_usd_num": 120.0,
    },
    "SE": {
        "cost_transport_month_tier1_avg_usd_num": 80.0,
        "cost_utility_month_avg_usd_num": 100.0,
    },
}


# =============================================================================
# WÄHRUNGSUMRECHNUNG
# =============================================================================

async def fetch_exchange_rates(client: httpx.AsyncClient) -> dict:
    """Holt aktuelle Wechselkurse von exchangerate-api.com (kostenlos, kein Key)"""
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        r = await client.get(url, timeout=10.0)
        data = r.json()
        if data.get("result") == "success":
            rates = data.get("rates", {})
            usd_rates = {
                currency: 1.0 / rate
                for currency, rate in rates.items()
                if rate and rate > 0
            }
            logger.info(f"✅ Wechselkurse aktualisiert ({len(usd_rates)} Währungen)")
            return usd_rates
    except Exception as e:
        logger.warning(f"⚠️ Wechselkurs-Fetch fehlgeschlagen: {e} – nutze Fallback-Werte")
    return EXCHANGE_RATES_TO_USD


def convert_to_usd(value: float, currency: str, rates: dict) -> float:
    rate = rates.get(currency, EXCHANGE_RATES_TO_USD.get(currency, 1.0))
    return round(value * rate, 2)


# =============================================================================
# TRANSFORMATION ENGINE
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
) -> Optional[float]:
    if value is None:
        return None

    rates = exchange_rates or EXCHANGE_RATES_TO_USD

    try:
        t = (transformation or "").strip().lower()

        if multiplier_pct is not None and t not in (
            "cpi_index_to_usd_convert",
            "kwh_price_multiply_250_plus_30pct_convert_usd",
            "cpi_transport_index_to_usd_convert",
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
            result = base * (value / 100.0)
            return round(result, 2)

        if t == "kwh_price_multiply_250_plus_30pct_convert_usd":
            monthly_electricity = value * 250
            monthly_total = monthly_electricity * 1.30
            usd = convert_to_usd(monthly_total, currency or "EUR", rates)
            return round(usd, 2)

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
# WORLD BANK FETCHER
# =============================================================================

async def fetch_worldbank_value(
    worldbank_id: str,
    series_id: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    url = f"https://api.worldbank.org/v2/country/{worldbank_id}/indicator/{series_id}"
    params = {"format": "json", "mrv": 5, "per_page": 5}

    try:
        response = await client.get(url, params=params, timeout=15.0)
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
        return None


# =============================================================================
# BLS FETCHER (USA only)
# =============================================================================

async def fetch_bls_value(
    series_id: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = {
        "seriesid": [series_id],
        "startyear": str(date.today().year - 3),
        "endyear": str(date.today().year),
    }

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


# =============================================================================
# OECD SDMX FETCHER
# =============================================================================

async def fetch_oecd_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/vnd.sdmx.data+json;version=1.0"
        })
        r.raise_for_status()
        data = r.json()

        datasets = data.get("dataSets", [])
        if not datasets:
            logger.warning(f"⚠️ OECD: keine dataSets in Response für {endpoint_url}")
            return None

        observations = datasets[0].get("observations", {})
        if not observations:
            logger.warning(f"⚠️ OECD: keine observations für {endpoint_url}")
            return None

        values = []
        for obs_key, obs_data in observations.items():
            if obs_data and obs_data[0] is not None:
                values.append(float(obs_data[0]))

        if not values:
            return None

        return values[-1]

    except Exception as e:
        logger.warning(f"⚠️ OECD fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# EUROSTAT FETCHER
# =============================================================================

async def fetch_eurostat_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/json"
        })
        r.raise_for_status()
        data = r.json()

        datasets = data.get("dataSets", [])
        if not datasets:
            logger.warning(f"⚠️ Eurostat: keine dataSets für {endpoint_url}")
            return None

        observations = datasets[0].get("observations", {})
        if not observations:
            logger.warning(f"⚠️ Eurostat: keine observations für {endpoint_url}")
            return None

        values = []
        for obs_key, obs_data in observations.items():
            if obs_data and obs_data[0] is not None:
                values.append(float(obs_data[0]))

        if not values:
            return None

        return values[-1]

    except Exception as e:
        logger.warning(f"⚠️ Eurostat fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# STATCAN FETCHER (Kanada)
# =============================================================================

async def fetch_statcan_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=30.0)
        r.raise_for_status()

        content = r.text
        reader = csv.DictReader(io.StringIO(content))

        rows = list(reader)
        if not rows:
            logger.warning(f"⚠️ StatCan: leere CSV für {endpoint_url}")
            return None

        for row in reversed(rows):
            val = row.get("VALUE") or row.get("value") or row.get("Value")
            if val and val.strip() not in ("", "."):
                try:
                    return float(val.strip())
                except ValueError:
                    continue

        logger.warning(f"⚠️ StatCan: kein gültiger Wert in CSV für {endpoint_url}")
        return None

    except Exception as e:
        logger.warning(f"⚠️ StatCan fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# ONS FETCHER (UK)
# =============================================================================

async def fetch_ons_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/json"
        })
        r.raise_for_status()
        data = r.json()

        observations = data.get("observations", [])
        if not observations:
            logger.warning(f"⚠️ ONS: keine observations für {endpoint_url}")
            return None

        for obs in reversed(observations):
            val = obs.get("observation")
            if val and val not in ("", ".", "N/A"):
                try:
                    return float(val)
                except ValueError:
                    continue

        logger.warning(f"⚠️ ONS: kein gültiger Wert für {endpoint_url}")
        return None

    except Exception as e:
        logger.warning(f"⚠️ ONS fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# PROVIDER ROUTER
# =============================================================================

async def fetch_value_for_rule(
    rule: dict,
    country: dict,
    client: httpx.AsyncClient
) -> Optional[float]:
    provider = rule["provider"].lower()
    endpoint_url = rule.get("endpoint_url", "")

    if provider == "worldbank":
        worldbank_id = country.get("worldbank_id") or country["iso2"]
        return await fetch_worldbank_value(worldbank_id, rule["series_id"], client)

    elif provider == "bls":
        if not country.get("bls_available"):
            logger.info(f"⏭️ BLS nicht verfügbar für {country['country_code']}")
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

    else:
        logger.warning(f"⚠️ Unbekannter Provider: {provider} für {rule['api_id']}")
        return None


# =============================================================================
# WÄHRUNG PRO PROVIDER/LAND bestimmen
# =============================================================================

def get_currency_for_rule(rule: dict, country: dict) -> str:
    provider = rule["provider"].lower()
    country_code = country.get("country_code", "")

    if provider in ("bls", "worldbank"):
        return "USD"

    currency_map = {
        "AU": "AUD", "CA": "CAD", "GB": "GBP",
        "US": "USD", "RU": "RUB", "SA": "SAR", "AE": "AED",
        "DE": "EUR", "FR": "EUR", "ES": "EUR", "IT": "EUR",
        "AT": "EUR", "NL": "EUR", "PT": "EUR", "BE": "EUR",
        "FI": "EUR", "IE": "EUR", "GR": "EUR",
        "SE": "SEK", "PL": "PLN", "CH": "CHF",
        "NO": "NOK", "DK": "DKK",
    }
    return currency_map.get(country_code, "USD")


# =============================================================================
# CORE: Verarbeitung eines einzelnen Landes
# =============================================================================

async def process_country(
    country: dict,
    api_rules: List[dict],
    exchange_rates: dict
) -> dict:
    country_code = country["country_code"]
    country_name = country["country_name"]
    today = date.today().isoformat()

    relevant_rules = [
        r for r in api_rules
        if r["country_iso"] is None or r["country_iso"] == country_code
    ]

    if not relevant_rules:
        logger.info(f"⏭️ Keine API-Regeln für {country_code}")
        return {"country_code": country_code, "success": True, "fields_written": 0}

    logger.info(
        f"🌍 Verarbeite {country_name} ({country_code}) "
        f"– {len(relevant_rules)} Regeln"
    )

    # v4.1.1 FIX C: Provider-Calls pro Land über eine Semaphore drosseln
    # (World Bank drosselt Bursts von ~9 gleichzeitigen Requests).
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "VisaScraper/4.1 fetch-apis"}
    ) as client:
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def _bounded_fetch(rule):
            async with sem:
                return await fetch_value_for_rule(rule, country, client)

        tasks = [_bounded_fetch(rule) for rule in relevant_rules]
        raw_values = await asyncio.gather(*tasks, return_exceptions=True)

    # Upsert-Dict aufbauen — nur Felder die in smart_country_data existieren
    upsert_data = {
        "country_code": country_code,
        "country_name": country_name,
    }

    fields_written = 0
    fields_skipped = 0

    for rule, raw_value in zip(relevant_rules, raw_values):
        db_field     = rule["db_field"]
        source_field = db_field + "_source"
        date_field   = db_field + "_date"

        # v4.0.1 FIX 1: Bei Exception / None / fehlgeschlagener Transformation
        # das Feld NICHT in den Upsert aufnehmen → vorhandener Wert bleibt erhalten.
        if isinstance(raw_value, Exception):
            logger.warning(f"⚠️ Exception für {rule['api_id']}: {raw_value} — Feld {db_field} übersprungen (Altwert bleibt)")
            fields_skipped += 1
            continue

        if raw_value is None:
            logger.info(f"⏭️ Kein Wert für {db_field} — übersprungen (Altwert bleibt)")
            fields_skipped += 1
            continue

        currency = get_currency_for_rule(rule, country)

        transformed_value = apply_transformation(
            raw_value,
            rule.get("transformation", "none"),
            multiplier_pct=rule.get("multiplier_pct"),
            offset_usd=rule.get("offset_usd"),
            country_code=country_code,
            db_field=db_field,
            currency=currency,
            exchange_rates=exchange_rates,
        )

        if transformed_value is not None:
            upsert_data[db_field]     = transformed_value
            upsert_data[source_field] = rule["source_label"]
            upsert_data[date_field]   = today
            fields_written += 1
            logger.info(
                f"  ✅ {db_field} = {transformed_value} "
                f"(raw: {raw_value}, provider: {rule['provider']})"
            )
        else:
            # Transformation fehlgeschlagen — Altwert ebenfalls erhalten
            logger.warning(f"⚠️ Transformation lieferte None für {db_field} — übersprungen (Altwert bleibt)")
            fields_skipped += 1

    # v4.0.1: Wenn außer den Schlüsselfeldern nichts Neues da ist, Upsert überspringen —
    # sonst würde ein leerer Upsert nur updated_at/Trigger anstoßen ohne Datengewinn.
    data_fields = [k for k in upsert_data if k not in ("country_code", "country_name")]
    if not data_fields:
        logger.info(f"⏭️ {country_name}: keine neuen Werte — Upsert übersprungen, Bestand unverändert")
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": True,
            "fields_written": 0,
            "fields_skipped": fields_skipped,
        }

    # Interne Felder herausfiltern bevor Upsert nach smart_country_data
    smart_data = {k: v for k, v in upsert_data.items() if k not in INTERNAL_FIELDS}

    try:
        # v4.1.1 FIX B: Blockierenden Supabase-Upsert in einen Thread auslagern
        # und hart timeouten, damit ein Netz-Stall den Event-Loop nicht einfriert.
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: supabase.table("smart_country_data").upsert(
                    smart_data,
                    on_conflict="country_code"
                ).execute()
            ),
            timeout=UPSERT_TIMEOUT,
        )

        logger.info(
            f"✅ {country_name}: {fields_written} Felder geschrieben, "
            f"{fields_skipped} übersprungen (Altwert erhalten) → smart_country_data"
        )
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": True,
            "fields_written": fields_written,
            "fields_skipped": fields_skipped,
        }

    except asyncio.TimeoutError:
        logger.error(f"⏱️ Supabase upsert Timeout ({UPSERT_TIMEOUT}s) für {country_code}")
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": False,
            "error": "upsert timeout",
        }
    except Exception as e:
        logger.error(f"❌ Supabase upsert fehlgeschlagen für {country_code}: {e}")
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": False,
            "error": str(e),
        }


# =============================================================================
# API ENDPOINT
# =============================================================================

class FetchApisRequest(BaseModel):
    country_codes: Optional[List[str]] = None
    fetch_all_active: Optional[bool] = False


@router.post("/fetch-apis")
async def fetch_apis(request: FetchApisRequest):
    """
    Holt API-Daten für GROUP B: FINANZEN und schreibt direkt in smart_country_data.

    POST /fetch-apis
    Option A: { "country_codes": ["US", "DE", "AU"] }  → spezifische Länder
    Option B: { "fetch_all_active": true }              → alle Länder in smart_country_data
    """

    if not request.fetch_all_active and not request.country_codes:
        return {
            "success": False,
            "error": "Provide either 'country_codes' or 'fetch_all_active': true"
        }

    # Wechselkurse laden (einmal pro Run)
    async with httpx.AsyncClient(timeout=10.0) as fx_client:
        exchange_rates = await fetch_exchange_rates(fx_client)

    # Länder laden — v4.1.0: Quelle = smart_country_data (Single Source of Truth, 126 Länder)
    # statt config_rules (nur 48). Damit erreicht fetch_all_active ALLE Länder, für die
    # globale World-Bank-Template-Regeln (country_iso IS NULL) ohnehin gelten.
    try:
        query = supabase.table("smart_country_data").select(
            "country_code, country_name"
        )

        if request.country_codes:
            query = query.in_("country_code", request.country_codes)

        scd_resp = query.execute()

        if not scd_resp.data:
            return {"success": False, "error": "Keine Länder in smart_country_data gefunden"}

        countries = []
        seen_codes = set()

        for r in scd_resp.data:
            country_code = (r.get("country_code") or "").strip()
            country_name = r.get("country_name") or country_code

            if not country_code or country_code in seen_codes:
                continue
            seen_codes.add(country_code)

            countries.append({
                "country_code": country_code,
                "country_name": country_name,
                "iso2": country_code,
                "worldbank_id": country_code,
                "bls_available": country_code == "US",
            })

        if not countries:
            return {"success": False, "error": "Keine Länder nach Filterung übrig"}

        logger.info(f"📋 {len(countries)} Länder aus smart_country_data")

    except Exception as e:
        logger.error(f"❌ Länder-Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # API-Regeln laden
    try:
        rules_resp = supabase.table("config_apis").select("*").eq("active", True).execute()
        api_rules = [
            r for r in rules_resp.data
            if r.get("target_table") == "data_group_b_finanzen"
        ]

        if not api_rules:
            return {"success": False, "error": "Keine aktiven API-Regeln in config_apis"}

        logger.info(f"📋 {len(api_rules)} API-Regeln geladen")

    except Exception as e:
        logger.error(f"❌ config_apis Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # Länder verarbeiten
    # v4.1.1 FIX A: jedes Land unter hartem Zeitbudget — ein hängendes Land
    # friert nicht mehr den ganzen Lauf ein, sondern wird übersprungen.
    results = []
    for country in countries:
        cc = country["country_code"]
        try:
            result = await asyncio.wait_for(
                process_country(country, api_rules, exchange_rates),
                timeout=COUNTRY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️ {cc}: {COUNTRY_TIMEOUT}s-Budget überschritten — übersprungen")
            result = {"country_code": cc, "success": False, "error": "country timeout"}
        except Exception as e:
            logger.error(f"❌ {cc}: unerwarteter Fehler — {e}")
            result = {"country_code": cc, "success": False, "error": str(e)}
        results.append(result)

    successful   = sum(1 for r in results if r.get("success"))
    total_fields = sum(r.get("fields_written", 0) for r in results)

    logger.info(
        f"🏁 fetch-apis v4.1.1: {successful}/{len(results)} Länder, "
        f"{total_fields} Felder total → smart_country_data"
    )

    return {
        "success": True,
        "version": "4.1.1",
        "total_countries": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "total_fields_written": total_fields,
        "results": results,
    }
