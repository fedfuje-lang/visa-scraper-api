"""
fetch_apis.py – Generischer API Fetcher für GROUP B: FINANZEN
FastAPI Router deployed on Railway.app

Liest Regeln aus config_apis + Länderliste aus config_countries
Schreibt DIREKT in data_group_b_finanzen (kein Umweg über discovered_urls/WF2)
Unterstützt: World Bank, BLS (USA)
Erweiterbar für: Eurostat, EIA, College Scorecard

v2.0.0 – 2026-03-08
"""

from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional, List
import httpx
import asyncio
import logging
import os
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
# TRANSFORMATION ENGINE
# Wendet die in config_apis hinterlegte Transformation auf den Rohwert an
# =============================================================================

def apply_transformation(value: float, transformation: str) -> Optional[float]:
    """
    Wendet eine Transformation auf einen Rohwert an.
    Transformation-Strings kommen direkt aus config_apis.transformation
    """
    if value is None:
        return None

    try:
        t = transformation.strip().lower()

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
        elif t == "none" or t == "":
            return round(value, 2)
        else:
            logger.warning(f"⚠️ Unknown transformation: {transformation} – returning raw value")
            return round(value, 2)

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
    """
    Holt einen einzelnen Indikatorwert von der World Bank API.
    Gibt den neuesten verfügbaren Wert zurück.
    """
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
    """
    Holt einen Jahreswert aus der BLS Consumer Expenditure Survey.
    Gibt den neuesten Jahreswert zurück.
    BLS API v2 – kein API Key nötig für einzelne Serien.
    """
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

        # Neuesten Jahreswert (period=M13 = annual average) finden
        for item in series_data[0].get("data", []):
            if item.get("period") == "M13":  # M13 = Annual average
                return float(item["value"])

        # Fallback: ersten verfügbaren Wert nehmen
        items = series_data[0].get("data", [])
        if items:
            return float(items[0]["value"])

        return None

    except Exception as e:
        logger.warning(f"⚠️ BLS fetch failed [{series_id}]: {e}")
        return None


# =============================================================================
# PROVIDER ROUTER
# Entscheidet welcher Fetcher basierend auf config_apis.provider verwendet wird
# =============================================================================

async def fetch_value_for_rule(rule: dict, country: dict, client: httpx.AsyncClient) -> Optional[float]:
    """
    Ruft den richtigen Provider-Fetcher auf basierend auf config_apis.provider
    """
    provider = rule["provider"].lower()

    if provider == "worldbank":
        worldbank_id = country.get("worldbank_id") or country["iso2"]
        return await fetch_worldbank_value(worldbank_id, rule["series_id"], client)

    elif provider == "bls":
        # BLS nur für USA
        if not country.get("bls_available"):
            logger.info(f"⏭️ BLS nicht verfügbar für {country['country_code']} – überspringe")
            return None
        return await fetch_bls_value(rule["series_id"], client)

    # Eurostat und andere Provider – Platzhalter für spätere Erweiterung
    elif provider == "eurostat":
        logger.info(f"⏭️ Eurostat noch nicht implementiert – überspringe {rule['api_id']}")
        return None

    else:
        logger.warning(f"⚠️ Unbekannter Provider: {provider} für {rule['api_id']}")
        return None


# =============================================================================
# CORE: Verarbeitung eines einzelnen Landes
# =============================================================================

async def process_country(country: dict, api_rules: List[dict]) -> dict:
    """
    Verarbeitet alle API-Regeln für ein einzelnes Land.
    Schreibt Ergebnisse direkt in data_group_b_finanzen.
    """
    country_code = country["country_code"]
    country_name = country["country_name"]
    today = date.today().isoformat()

    # Nur Regeln die für dieses Land gelten:
    # country_iso = NULL (global) ODER country_iso = dieses Land
    relevant_rules = [
        r for r in api_rules
        if r["country_iso"] is None or r["country_iso"] == country_code
    ]

    if not relevant_rules:
        logger.info(f"⏭️ Keine API-Regeln für {country_code}")
        return {"country_code": country_code, "success": True, "fields_written": 0}

    logger.info(f"🌍 Verarbeite {country_name} ({country_code}) – {len(relevant_rules)} Regeln")

    # Alle API-Calls parallel ausführen
    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_value_for_rule(rule, country, client)
            for rule in relevant_rules
        ]
        raw_values = await asyncio.gather(*tasks, return_exceptions=True)

    # Ergebnis-Dict für Supabase Upsert aufbauen
    upsert_data = {
        "country_code": country_code,
        "country_name": country_name,
        "updated_at": today,
    }

    fields_written = 0
    fields_null = 0

    for rule, raw_value in zip(relevant_rules, raw_values):
        db_field = rule["db_field"]
        source_field = db_field.replace("_num", "_source").replace("_usd", "_usd_source")
        date_field = db_field.replace("_num", "_date").replace("_usd", "_usd_date")

        # Fehler beim API-Call
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

        # Transformation anwenden
        transformed_value = apply_transformation(raw_value, rule.get("transformation", "none"))

        if transformed_value is not None:
            upsert_data[db_field] = transformed_value
            upsert_data[source_field] = rule["source_label"]
            upsert_data[date_field] = today
            fields_written += 1
            logger.info(f"  ✅ {db_field} = {transformed_value} (raw: {raw_value}, transform: {rule['transformation']})")
        else:
            upsert_data[db_field] = None
            upsert_data[source_field] = None
            upsert_data[date_field] = None
            fields_null += 1

    # Direkt in data_group_b_finanzen schreiben
    try:
        supabase.table("data_group_b_finanzen").upsert(
            upsert_data,
            on_conflict="country_code"
        ).execute()

        logger.info(f"✅ {country_name}: {fields_written} Felder geschrieben, {fields_null} null")
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": True,
            "fields_written": fields_written,
            "fields_null": fields_null
        }

    except Exception as e:
        logger.error(f"❌ Supabase upsert fehlgeschlagen für {country_code}: {e}")
        return {
            "country_code": country_code,
            "country_name": country_name,
            "success": False,
            "error": str(e)
        }


# =============================================================================
# API ENDPOINT
# =============================================================================

class FetchApisRequest(BaseModel):
    country_codes: Optional[List[str]] = None  # ["US", "DE"] – spezifische Länder
    fetch_all_active: Optional[bool] = False    # True = alle aktiven Länder aus config_countries


@router.post("/fetch-apis")
async def fetch_apis(request: FetchApisRequest):
    """
    Holt API-Daten für GROUP B: FINANZEN und schreibt direkt in data_group_b_finanzen.

    POST /fetch-apis
    Option A: { "country_codes": ["US", "DE"] }     → spezifische Länder
    Option B: { "fetch_all_active": true }           → alle aktiven Länder aus config_countries
    """

    # ==========================================================================
    # 1. Länderliste aus config_countries holen
    # ==========================================================================
    try:
        if request.fetch_all_active:
            countries_resp = supabase.table("config_countries").select(
                "country_code, country_name, iso2, worldbank_id, eurostat_geo, bls_available"
            ).eq("active", True).execute()
        elif request.country_codes:
            countries_resp = supabase.table("config_countries").select(
                "country_code, country_name, iso2, worldbank_id, eurostat_geo, bls_available"
            ).in_("country_code", request.country_codes).eq("active", True).execute()
        else:
            return {"success": False, "error": "Provide either 'country_codes' or 'fetch_all_active': true"}

        countries = countries_resp.data
        if not countries:
            return {"success": False, "error": "Keine aktiven Länder gefunden in config_countries"}

        logger.info(f"📋 {len(countries)} Länder zu verarbeiten: {[c['country_code'] for c in countries]}")

    except Exception as e:
        logger.error(f"❌ config_countries Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # ==========================================================================
    # 2. Alle aktiven API-Regeln aus config_apis holen
    # ==========================================================================
    try:
        rules_resp = supabase.table("config_apis").select(
            "api_id, country_iso, target_table, db_field, provider, series_id, source_label, transformation"
        ).eq("active", True).eq("target_table", "data_group_b_finanzen").execute()

        api_rules = rules_resp.data
        if not api_rules:
            return {"success": False, "error": "Keine aktiven API-Regeln gefunden in config_apis"}

        logger.info(f"📋 {len(api_rules)} API-Regeln geladen")

    except Exception as e:
        logger.error(f"❌ config_apis Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    # ==========================================================================
    # 3. Länder nacheinander verarbeiten
    #    (nicht parallel – schont API Rate Limits von World Bank / BLS)
    # ==========================================================================
    results = []

    for country in countries:
        result = await process_country(country, api_rules)
        results.append(result)

    # ==========================================================================
    # 4. Zusammenfassung
    # ==========================================================================
    successful = sum(1 for r in results if r.get("success"))
    total_fields = sum(r.get("fields_written", 0) for r in results)

    logger.info(f"🏁 fetch-apis abgeschlossen: {successful}/{len(results)} Länder, {total_fields} Felder total")

    return {
        "success": True,
        "total_countries": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "total_fields_written": total_fields,
        "results": results
    }
