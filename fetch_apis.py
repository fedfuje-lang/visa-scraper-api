"""
fetch_apis.py – Generischer API Fetcher für GROUP B: FINANZEN
FastAPI Router deployed on Railway.app

Liest Regeln aus config_apis + Länderliste aus config_rules (bewährt)
Schreibt DIREKT in data_group_b_finanzen (kein Umweg über discovered_urls/WF2)

Provider-Support:
  - World Bank  (global, Jahreswerte → /12)
  - BLS         (USA only, Jahreswerte → /12)
  - OECD SDMX   (AU, CA, DE, ES, FR, GB + weitere, CPI-Index)
  - Eurostat    (DE, ES, FR + EU-Länder, kWh-Preis → Utility)
  - StatCan     (CA, CPI CSV)
  - ONS         (GB, CPIH JSON)

v3.0.0 – 2026-04-23
  NEU: OECD SDMX Fetcher (CP04 Utility, CP07 Transport)
  NEU: Eurostat Fetcher (nrg_pc_204 kWh-Preis → Monatskosten)
  NEU: StatCan Fetcher (CPI CSV Tabellen)
  NEU: ONS Fetcher (CPIH JSON Beta API)
  NEU: Transformation Engine erweitert um CPI-Index und kWh-Logik
  NEU: Währungsumrechnung via ExchangeRate-API (kostenlos, kein Key)
  FIX: country_codes Filter wird direkt in Supabase-Query angewendet
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
# WÄHRUNGS-BASISWERTE (Fallback falls ExchangeRate-API nicht erreichbar)
# Werden beim Start aktualisiert via fetch_exchange_rates()
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
    "USD": 1.0,
}

# CPI-Basiswerte für Index-zu-USD Umrechnung (2015=100 Basis)
# Werden genutzt wenn OECD/ONS/StatCan nur einen Index liefern
# Format: country_code -> {field -> base_usd_value}
CPI_BASE_VALUES = {
    "AU": {
        "cost_transport_month_tier1_avg_usd_num": 95.0,   # AUD ~147 / 1.55
        "cost_utility_month_avg_usd_num": 160.0,          # AUD ~247 / 1.55
    },
    "CA": {
        "cost_transport_month_tier1_avg_usd_num": 100.0,  # CAD ~137 * 0.73
        "cost_utility_month_avg_usd_num": 145.0,
    },
    "DE": {
        "cost_transport_month_tier1_avg_usd_num": 55.0,   # EUR ~51 * 1.08
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
        "cost_transport_month_tier1_avg_usd_num": 200.0,  # GBP ~158 * 1.27
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
            # Umkehren: wir wollen X → USD, die API liefert USD → X
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
# TRANSFORMATION ENGINE (erweitert v3.0)
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
    """
    Transformation Engine — unterstützt:
    - World Bank: divide_by_12[_multiply_X]
    - OECD/StatCan/ONS: cpi_index_to_usd_convert
    - Eurostat: kwh_price_multiply_250_plus_30pct_convert_usd
    """
    if value is None:
        return None

    rates = exchange_rates or EXCHANGE_RATES_TO_USD

    try:
        t = (transformation or "").strip().lower()

        # ------------------------------------------------------------------
        # WORLD BANK: Jahreswert / 12 * Multiplikator
        # ------------------------------------------------------------------
        if multiplier_pct is not None and t not in (
            "cpi_index_to_usd_convert",
            "kwh_price_multiply_250_plus_30pct_convert_usd",
            "cpi_transport_index_to_usd_convert",
        ):
            result = (value / 12) * float(multiplier_pct)
            if offset_usd:
                result += float(offset_usd)
            return round(result, 2)

        # ------------------------------------------------------------------
        # OECD / StatCan / ONS: CPI-Index → absoluter USD-Betrag
        # Index 2015=100 → Basiswert * (Index/100)
        # ------------------------------------------------------------------
        if t in ("cpi_index_to_usd_convert", "cpi_transport_index_to_usd_convert"):
            if not country_code or not db_field:
                logger.warning("⚠️ cpi_index_to_usd_convert braucht country_code + db_field")
                return None

            base = CPI_BASE_VALUES.get(country_code, {}).get(db_field)
            if base is None:
                logger.warning(f"⚠️ Kein CPI-Basiswert für {country_code}/{db_field}")
                return None

            # value = CPI-Index (z.B. 118.4)
            result = base * (value / 100.0)
            return round(result, 2)

        # ------------------------------------------------------------------
        # EUROSTAT: kWh-Preis → monatliche Utility-Kosten
        # Formel: kWh_preis * 250 kWh/Monat * 1.30 (Gas+Wasser+Heizung) → EUR → USD
        # ------------------------------------------------------------------
        if t == "kwh_price_multiply_250_plus_30pct_convert_usd":
            # value = EUR pro kWh (z.B. 0.38 für DE)
            monthly_electricity = value * 250          # 250 kWh/Monat für 85m²
            monthly_total = monthly_electricity * 1.30  # +30% für Gas/Wasser/Heizung
            usd = convert_to_usd(monthly_total, currency or "EUR", rates)
            return round(usd, 2)

        # ------------------------------------------------------------------
        # STANDARD World Bank Transformationen
        # ------------------------------------------------------------------
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
# Format: jsondata
# Response: data.dataSets[0].observations → {key: [value, ...]}
# =============================================================================

async def fetch_oecd_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    """
    Holt CPI-Index von OECD SDMX API.
    Erwartet format=jsondata in der URL.
    Gibt den neuesten Beobachtungswert zurück.
    """
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/vnd.sdmx.data+json;version=1.0"
        })
        r.raise_for_status()
        data = r.json()

        # SDMX-JSON Struktur: dataSets[0].observations = {"0:0:..": [value, status]}
        datasets = data.get("dataSets", [])
        if not datasets:
            logger.warning(f"⚠️ OECD: keine dataSets in Response für {endpoint_url}")
            return None

        observations = datasets[0].get("observations", {})
        if not observations:
            logger.warning(f"⚠️ OECD: keine observations für {endpoint_url}")
            return None

        # Letzten Wert nehmen (observations sind dict mit key→[value, status])
        values = []
        for obs_key, obs_data in observations.items():
            if obs_data and obs_data[0] is not None:
                values.append(float(obs_data[0]))

        if not values:
            return None

        # Neuesten Wert (letzter in der Liste)
        return values[-1]

    except Exception as e:
        logger.warning(f"⚠️ OECD fetch failed für {endpoint_url}: {e}")
        return None


# =============================================================================
# EUROSTAT FETCHER
# Dataset: nrg_pc_204 – Electricity prices for household consumers
# Gibt kWh-Preis in EUR zurück
# =============================================================================

async def fetch_eurostat_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    """
    Holt Strompreis (EUR/kWh) von Eurostat SDMX API.
    Gibt den neuesten Wert zurück.
    """
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/json"
        })
        r.raise_for_status()
        data = r.json()

        # Eurostat SDMX-JSON: gleiche Struktur wie OECD
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
# CSV-Download von Statistics Canada
# Gibt CPI-Index zurück
# =============================================================================

async def fetch_statcan_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    """
    Holt CPI-Index-Wert von Statistics Canada (CSV-Download).
    Filtert nach der neuesten Periode und gibt den Index-Wert zurück.
    """
    try:
        r = await client.get(endpoint_url, timeout=30.0)
        r.raise_for_status()

        # CSV parsen
        content = r.text
        reader = csv.DictReader(io.StringIO(content))

        rows = list(reader)
        if not rows:
            logger.warning(f"⚠️ StatCan: leere CSV für {endpoint_url}")
            return None

        # Letzte Zeile mit einem gültigen Wert
        for row in reversed(rows):
            # StatCan CSV hat typischerweise "VALUE" oder "value" Spalte
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
# Beta API von Office for National Statistics
# Gibt CPIH-Index zurück
# =============================================================================

async def fetch_ons_value(
    endpoint_url: str,
    client: httpx.AsyncClient
) -> Optional[float]:
    """
    Holt CPIH-Index von UK ONS Beta API.
    Gibt den neuesten Beobachtungswert zurück.
    """
    try:
        r = await client.get(endpoint_url, timeout=20.0, headers={
            "Accept": "application/json"
        })
        r.raise_for_status()
        data = r.json()

        # ONS Beta API: {"observations": [{"observation": "123.4", "time": "2024"}, ...]}
        observations = data.get("observations", [])
        if not observations:
            logger.warning(f"⚠️ ONS: keine observations für {endpoint_url}")
            return None

        # Neueste Beobachtung (letzte in der Liste)
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
# PROVIDER ROUTER — erkennt Provider und ruft richtigen Fetcher auf
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
    """Bestimmt die Ausgangswährung basierend auf Provider und Land."""
    provider = rule["provider"].lower()
    country_code = country.get("country_code", "")

    if provider in ("bls", "worldbank"):
        return "USD"

    currency_map = {
        "AU": "AUD", "CA": "CAD", "GB": "GBP",
        "US": "USD", "RU": "RUB", "SA": "SAR", "AE": "AED",
        # Euro-Länder
        "DE": "EUR", "FR": "EUR", "ES": "EUR", "IT": "EUR",
        "AT": "EUR", "NL": "EUR", "PT": "EUR", "BE": "EUR",
        "FI": "EUR", "IE": "EUR", "GR": "EUR",
        # Andere
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

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "VisaScraper/3.0 fetch-apis"}
    ) as client:
        tasks = [
            fetch_value_for_rule(rule, country, client)
            for rule in relevant_rules
        ]
        raw_values = await asyncio.gather(*tasks, return_exceptions=True)

    upsert_data = {
        "country_code": country_code,
        "country_name": country_name,
        "rule_id": f"{country_code}-COSTS",
        "source_url": "api://fetch-apis",
        "updated_at": today,
    }

    fields_written = 0
    fields_null = 0

    for rule, raw_value in zip(relevant_rules, raw_values):
        db_field    = rule["db_field"]
        source_field = db_field + "_source"
        date_field   = db_field + "_date"

        if isinstance(raw_value, Exception):
            logger.warning(f"⚠️ Exception für {rule['api_id']}: {raw_value}")
            upsert_data[db_field] = None
            upsert_data[source_field] = None
            upsert_data[date_field] = None
            fields_null += 1
            continue

        if raw_value is None:
            upsert_data[db_field] = None
            upsert_data[source_field] = None
            upsert_data[date_field] = None
            fields_null += 1
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
            upsert_data[db_field] = transformed_value
            upsert_data[source_field] = rule["source_label"]
            upsert_data[date_field] = today
            fields_written += 1
            logger.info(
                f"  ✅ {db_field} = {transformed_value} "
                f"(raw: {raw_value}, provider: {rule['provider']})"
            )
        else:
            upsert_data[db_field] = None
            upsert_data[source_field] = None
            upsert_data[date_field] = None
            fields_null += 1

    try:
        supabase.table("data_group_b_finanzen").upsert(
            upsert_data,
            on_conflict="country_code"
        ).execute()

        logger.info(
            f"✅ {country_name}: {fields_written} Felder geschrieben, "
            f"{fields_null} null"
        )
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": True,
            "fields_written": fields_written,
            "fields_null": fields_null,
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
    Holt API-Daten für GROUP B: FINANZEN und schreibt direkt in data_group_b_finanzen.

    POST /fetch-apis
    Option A: { "country_codes": ["US", "DE", "AU"] }  → spezifische Länder
    Option B: { "fetch_all_active": true }              → alle aktiven Länder
    """

    if not request.fetch_all_active and not request.country_codes:
        return {
            "success": False,
            "error": "Provide either 'country_codes' or 'fetch_all_active': true"
        }

    # -------------------------------------------------------------------------
    # Wechselkurse laden (einmal pro Run)
    # -------------------------------------------------------------------------
    async with httpx.AsyncClient(timeout=10.0) as fx_client:
        exchange_rates = await fetch_exchange_rates(fx_client)

    # -------------------------------------------------------------------------
    # Länder laden
    # -------------------------------------------------------------------------
    try:
        query = supabase.table("config_rules").select(
            "rule_id, country_name, country_iso"
        ).eq("active", True)

        if request.country_codes:
            query = query.in_("country_iso", request.country_codes)

        rules_resp = query.execute()

        if not rules_resp.data:
            return {"success": False, "error": "Keine aktiven Länder gefunden"}

        countries = []
        seen_codes = set()

        for r in rules_resp.data:
            country_code = r.get("country_iso", "").strip()
            country_name = r["country_name"]

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

        logger.info(
            f"📋 {len(countries)} Länder: "
            f"{[c['country_code'] for c in countries]}"
        )

    except Exception as e:
        logger.error(f"❌ Länder-Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # API-Regeln laden
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # Länder verarbeiten
    # -------------------------------------------------------------------------
    results = []
    for country in countries:
        result = await process_country(country, api_rules, exchange_rates)
        results.append(result)

    successful   = sum(1 for r in results if r.get("success"))
    total_fields = sum(r.get("fields_written", 0) for r in results)

    logger.info(
        f"🏁 fetch-apis v3.0: {successful}/{len(results)} Länder, "
        f"{total_fields} Felder total"
    )

    return {
        "success": True,
        "version": "3.0.0",
        "total_countries": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "total_fields_written": total_fields,
        "results": results,
    }
