"""
fetch_apis.py – Generischer API Fetcher für GROUP B: FINANZEN
FastAPI Router deployed on Railway.app

Liest Regeln aus config_apis + Länderliste aus config_rules (bewährt)
Schreibt DIREKT in data_group_b_finanzen (kein Umweg über discovered_urls/WF2)
Unterstützt: World Bank, BLS (USA)
Erweiterbar für: Eurostat, EIA, College Scorecard

v2.2.0 – 2026-03-09
FIX: source_field / date_field verwenden jetzt vollen db_field Namen inkl. Suffix
     DB-Spalten wurden umbenannt: visa_tourist_max_days_num_source (nicht mehr _source ohne _num)
     DB = ACF = WF3 jetzt überall konsistent
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
# =============================================================================

def apply_transformation(
    value: float,
    transformation: str,
    multiplier_pct: Optional[float] = None,
    offset_usd: Optional[float] = None
) -> Optional[float]:
    if value is None:
        return None

    try:
        if multiplier_pct is not None:
            result = (value / 12) * float(multiplier_pct)
            if offset_usd is not None:
                result += float(offset_usd)
            return round(result, 2)

        t = (transformation or "").strip().lower()
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
            logger.warning(f"⚠️ Unknown transformation: {transformation} – using raw/12")
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
# PROVIDER ROUTER
# =============================================================================

async def fetch_value_for_rule(rule: dict, country: dict, client: httpx.AsyncClient) -> Optional[float]:
    provider = rule["provider"].lower()

    if provider == "worldbank":
        worldbank_id = country.get("worldbank_id") or country["iso2"]
        return await fetch_worldbank_value(worldbank_id, rule["series_id"], client)

    elif provider == "bls":
        if not country.get("bls_available"):
            logger.info(f"⏭️ BLS nicht verfügbar für {country['country_code']} – überspringe")
            return None
        return await fetch_bls_value(rule["series_id"], client)

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

    logger.info(f"🌍 Verarbeite {country_name} ({country_code}) – {len(relevant_rules)} Regeln")

    async with httpx.AsyncClient() as client:
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
        db_field = rule["db_field"]

        # FIX v2.2: DB-Spalten haben jetzt vollen Namen inkl. Typ-Suffix
        # z.B. cost_living_excl_rent_tier1_month_usd_num → _num_source / _num_date
        # Kein Suffix-Stripping mehr nötig – DB = ACF = WF3 konsistent
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

        transformed_value = apply_transformation(
            raw_value,
            rule.get("transformation", "none"),
            multiplier_pct=rule.get("multiplier_pct"),
            offset_usd=rule.get("offset_usd")
        )

        if transformed_value is not None:
            upsert_data[db_field] = transformed_value
            upsert_data[source_field] = rule["source_label"]
            upsert_data[date_field] = today
            fields_written += 1
            logger.info(f"  ✅ {db_field} = {transformed_value} (raw: {raw_value})")
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
    country_codes: Optional[List[str]] = None
    fetch_all_active: Optional[bool] = False


@router.post("/fetch-apis")
async def fetch_apis(request: FetchApisRequest):
    """
    Holt API-Daten für GROUP B: FINANZEN und schreibt direkt in data_group_b_finanzen.

    POST /fetch-apis
    Option A: { "country_codes": ["US", "DE"] }     → spezifische Länder
    Option B: { "fetch_all_active": true }           → alle aktiven Länder
    """

    try:
        if request.fetch_all_active:
            rules_countries_resp = supabase.table("config_rules").select(
                "country_name"
            ).eq("active", True).execute()
        elif request.country_codes:
            rules_countries_resp = supabase.table("config_rules").select(
                "country_name"
            ).eq("active", True).execute()
        else:
            return {"success": False, "error": "Provide either 'country_codes' or 'fetch_all_active': true"}

        all_names = list({r["country_name"] for r in rules_countries_resp.data})

        rules_resp_full = supabase.table("config_rules").select(
            "rule_id, country_name"
        ).eq("active", True).execute()

        name_to_code = {}
        for r in rules_resp_full.data:
            rule_id = r["rule_id"]
            country_name = r["country_name"]
            if "-" in rule_id:
                code = rule_id.split("-")[0]
                name_to_code[country_name] = code

        countries = []
        for name in all_names:
            code = name_to_code.get(name)
            if not code:
                continue
            if request.country_codes and code not in request.country_codes:
                continue
            countries.append({
                "country_code": code,
                "country_name": name,
                "iso2": code,
                "worldbank_id": code,
                "eurostat_geo": None,
                "bls_available": code == "US"
            })

        if not countries:
            return {"success": False, "error": "Keine aktiven Länder gefunden"}

        logger.info(f"📋 {len(countries)} Länder: {[c['country_code'] for c in countries]}")

    except Exception as e:
        logger.error(f"❌ Länder-Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    try:
        rules_resp = supabase.table("config_apis").select("*").eq("active", True).execute()
        api_rules = [r for r in rules_resp.data if r.get("target_table") == "data_group_b_finanzen"]

        if not api_rules:
            return {"success": False, "error": "Keine aktiven API-Regeln in config_apis"}

        logger.info(f"📋 {len(api_rules)} API-Regeln geladen")

    except Exception as e:
        logger.error(f"❌ config_apis Abfrage fehlgeschlagen: {e}")
        return {"success": False, "error": str(e)}

    results = []
    for country in countries:
        result = await process_country(country, api_rules)
        results.append(result)

    successful = sum(1 for r in results if r.get("success"))
    total_fields = sum(r.get("fields_written", 0) for r in results)

    logger.info(f"🏁 fetch-apis: {successful}/{len(results)} Länder, {total_fields} Felder total")

    return {
        "success": True,
        "total_countries": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "total_fields_written": total_fields,
        "results": results
    }
