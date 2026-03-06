"""
World Bank API Fetcher for GROUP B: FINANZEN
FastAPI Router deployed on Railway.app

Holt strukturierte Kostendaten direkt von der World Bank API
und speichert sie als Markdown-ähnlichen Text in discovered_urls
(content_quality = useful, status = pending)

Kompatibel mit WF2 Content Extraction (Gemini)

v1.0.0
"""

from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional, List
import httpx
import asyncio
import logging
import os
import json
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter()

# Supabase Connection
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# WORLD BANK INDICATOR MAPPING
# Welche Indicator-IDs zu welchen GROUP B Feldern gehören
# =============================================================================

WORLDBANK_INDICATORS = {
    # Lebensmittel & Lebenshaltung
    "food_price_index": {
        "indicator_id": "FP.CPI.TOTL",
        "label": "Consumer Price Index (All Items)",
        "field_hint": "cost_living_excl_rent",
        "unit": "index"
    },
    "food_inflation": {
        "indicator_id": "FP.CPI.TOTL.ZG",
        "label": "Inflation Consumer Prices Annual Percent",
        "field_hint": "cost_living_excl_rent",
        "unit": "percent"
    },
    # Kaufkraftparität – Basis für USD-Umrechnung
    "ppp_conversion": {
        "indicator_id": "PA.NUS.PPPC.RF",
        "label": "PPP Conversion Factor Private Consumption",
        "field_hint": "ppp_base",
        "unit": "LCU per USD"
    },
    # Haushaltseinkommen / Konsum
    "household_consumption_per_capita": {
        "indicator_id": "NE.CON.PRVT.PC.KD",
        "label": "Household Final Consumption Expenditure Per Capita (USD)",
        "field_hint": "cost_living_excl_rent",
        "unit": "USD per year"
    },
    # GNI pro Kopf (Proxy für Lebenshaltungskosten-Niveau)
    "gni_per_capita": {
        "indicator_id": "NY.GNP.PCAP.PP.CD",
        "label": "GNI Per Capita PPP (current international USD)",
        "field_hint": "cost_living_excl_rent_tier2",
        "unit": "USD per year"
    },
    # Energie / Utilities
    "electricity_access": {
        "indicator_id": "EG.ELC.ACCS.ZS",
        "label": "Access to Electricity Percent of Population",
        "field_hint": "cost_utility_month_avg",
        "unit": "percent"
    },
    # Gesundheitsausgaben (Proxy für Versicherungskosten)
    "health_expenditure_per_capita": {
        "indicator_id": "SH.XPD.CHEX.PC.CD",
        "label": "Current Health Expenditure Per Capita (USD)",
        "field_hint": "cost_insurance_private_month_avg",
        "unit": "USD per year"
    },
    "out_of_pocket_health": {
        "indicator_id": "SH.XPD.OOPC.CH.ZS",
        "label": "Out of Pocket Health Expenditure Percent",
        "field_hint": "cost_insurance_private_month_avg",
        "unit": "percent of health expenditure"
    },
    # Transport
    "fuel_imports": {
        "indicator_id": "TM.VAL.FUEL.ZS.UN",
        "label": "Fuel Imports Percent of Merchandise Imports",
        "field_hint": "cost_transport_month_tier1_avg",
        "unit": "percent"
    },
    # Container / Logistik
    "logistics_performance": {
        "indicator_id": "LP.LPI.OVRL.XQ",
        "label": "Logistics Performance Index Overall",
        "field_hint": "cost_shipping_container_20ft_avg",
        "unit": "score 1-5"
    },
    "container_port_throughput": {
        "indicator_id": "IS.SHP.GOOD.TU",
        "label": "Container Port Traffic TEU",
        "field_hint": "cost_shipping_container_20ft_avg",
        "unit": "TEU"
    }
}

# =============================================================================
# WORLD BANK API FETCH
# =============================================================================

async def fetch_indicator(country_iso: str, indicator_id: str, client: httpx.AsyncClient) -> Optional[dict]:
    """
    Holt einen einzelnen Indikator für ein Land von der World Bank API
    Gibt den neuesten verfügbaren Wert zurück
    """
    url = f"https://api.worldbank.org/v2/country/{country_iso}/indicator/{indicator_id}"
    params = {
        "format": "json",
        "mrv": 5,  # Most Recent Values – letzte 5 Jahre
        "per_page": 5
    }

    try:
        response = await client.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()

        # World Bank gibt [metadata, data] zurück
        if not data or len(data) < 2 or not data[1]:
            return None

        records = data[1]

        # Ersten nicht-None Wert finden
        for record in records:
            if record.get("value") is not None:
                return {
                    "value": record["value"],
                    "year": record["date"],
                    "country": record["country"]["value"],
                    "indicator": record["indicator"]["value"]
                }

        return None

    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch {indicator_id} for {country_iso}: {str(e)}")
        return None


async def fetch_all_indicators(country_iso: str) -> dict:
    """
    Holt alle relevanten Indikatoren für ein Land parallel
    """
    async with httpx.AsyncClient() as client:
        tasks = {
            key: fetch_indicator(country_iso, meta["indicator_id"], client)
            for key, meta in WORLDBANK_INDICATORS.items()
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    fetched = {}
    for key, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            logger.warning(f"⚠️ Exception for {key}: {result}")
            fetched[key] = None
        else:
            fetched[key] = result

    return fetched


# =============================================================================
# MARKDOWN BUILDER
# Baut einen strukturierten Markdown-Text aus den API-Daten
# Gemini kann diesen Text genauso extrahieren wie normales Markdown
# =============================================================================

def build_markdown(country_name: str, country_iso: str, indicators: dict) -> str:
    """
    Wandelt World Bank API Daten in strukturierten Markdown-Text um
    der von WF2 (Gemini Extraktion) verarbeitet werden kann
    """
    lines = [
        f"# Cost of Living Data: {country_name} ({country_iso})",
        f"Source: World Bank Open Data API",
        f"Retrieved: {datetime.utcnow().strftime('%Y-%m-%d')}",
        "",
        "## Economic Overview",
        ""
    ]

    # GNI pro Kopf → monatliche Lebenshaltungskosten schätzen
    gni = indicators.get("gni_per_capita")
    household = indicators.get("household_consumption_per_capita")

    if gni and gni.get("value"):
        monthly_gni = round(gni["value"] / 12, 2)
        lines.append(f"- GNI per capita (PPP): USD {gni['value']:,.0f} per year ({gni['year']})")
        lines.append(f"- Estimated monthly equivalent: USD {monthly_gni:,.0f}")

    if household and household.get("value"):
        monthly_consumption = round(household["value"] / 12, 2)
        lines.append(f"- Household consumption per capita: USD {household['value']:,.0f} per year ({household['year']})")
        lines.append(f"- Monthly household consumption: USD {monthly_consumption:,.0f}")

    lines.append("")
    lines.append("## Inflation & Prices")
    lines.append("")

    cpi = indicators.get("food_price_index")
    inflation = indicators.get("food_inflation")
    ppp = indicators.get("ppp_conversion")

    if cpi and cpi.get("value"):
        lines.append(f"- Consumer Price Index: {cpi['value']:.1f} ({cpi['year']})")

    if inflation and inflation.get("value"):
        lines.append(f"- Annual inflation rate: {inflation['value']:.1f}% ({inflation['year']})")

    if ppp and ppp.get("value"):
        lines.append(f"- PPP conversion factor: {ppp['value']:.2f} LCU per USD ({ppp['year']})")

    lines.append("")
    lines.append("## Healthcare & Insurance Costs")
    lines.append("")

    health = indicators.get("health_expenditure_per_capita")
    oop = indicators.get("out_of_pocket_health")

    if health and health.get("value"):
        monthly_health = round(health["value"] / 12, 2)
        lines.append(f"- Health expenditure per capita: USD {health['value']:,.0f} per year ({health['year']})")
        lines.append(f"- Monthly health cost estimate: USD {monthly_health:,.0f}")

    if oop and oop.get("value"):
        lines.append(f"- Out-of-pocket health expenditure: {oop['value']:.1f}% of total health spending ({oop['year']})")

    lines.append("")
    lines.append("## Transport & Logistics")
    lines.append("")

    lpi = indicators.get("logistics_performance")
    container = indicators.get("container_port_throughput")

    if lpi and lpi.get("value"):
        lines.append(f"- Logistics Performance Index: {lpi['value']:.2f}/5.0 ({lpi['year']})")

    if container and container.get("value"):
        lines.append(f"- Container port throughput: {container['value']:,.0f} TEU ({container['year']})")

    lines.append("")
    lines.append("## Utilities & Energy")
    lines.append("")

    electricity = indicators.get("electricity_access")

    if electricity and electricity.get("value"):
        lines.append(f"- Electricity access: {electricity['value']:.1f}% of population ({electricity['year']})")

    # Zusammenfassung für Gemini
    lines.append("")
    lines.append("## Summary for Cost Estimation")
    lines.append("")
    lines.append("Note: All values are World Bank official statistics.")
    lines.append("Monthly costs can be derived by dividing annual figures by 12.")
    lines.append("PPP-adjusted values provide the best comparison across countries.")

    # Rohdaten als JSON am Ende anhängen (für Gemini Extraktion)
    lines.append("")
    lines.append("## Raw Data (JSON)")
    lines.append("")
    lines.append("```json")

    raw_data = {}
    for key, result in indicators.items():
        if result and result.get("value") is not None:
            raw_data[key] = {
                "value": result["value"],
                "year": result["year"],
                "label": WORLDBANK_INDICATORS[key]["label"],
                "field_hint": WORLDBANK_INDICATORS[key]["field_hint"],
                "unit": WORLDBANK_INDICATORS[key]["unit"]
            }

    lines.append(json.dumps(raw_data, indent=2))
    lines.append("```")

    return "\n".join(lines)


# =============================================================================
# SUPABASE SAVE
# =============================================================================

def save_worldbank_to_supabase(
    country_iso: str,
    country_name: str,
    rule_id: str,
    markdown: str
) -> bool:
    """
    Speichert World Bank Daten als discovered_url Eintrag
    Direkt als content_quality=useful und status=pending
    WF2 verarbeitet diesen Eintrag wie jeden anderen
    """

    url_key = f"worldbank://api.worldbank.org/v2/country/{country_iso}/group-b"

    data = {
        "url": url_key,
        "page_title": f"World Bank Cost Data – {country_name}",
        "relevance_score": 10,
        "topics": ["living costs", "cost of living", "economic indicators"],
        "discovered_depth": 0,
        "rule_id": rule_id,
        "country_code": country_iso,
        "country_name": country_name,
        "target_group": "GROUP B: FINANZEN",
        "status": "pending",
        "content_quality": "useful",
        "quality_score": 10,
        "markdown_content": markdown,
        "is_main_url": True
    }

    try:
        supabase.table("discovered_urls").upsert(
            data,
            on_conflict="url"
        ).execute()
        logger.info(f"✅ World Bank data saved for {country_name} ({country_iso})")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save World Bank data for {country_iso}: {str(e)}")
        return False


# =============================================================================
# API ENDPOINT
# =============================================================================

class WorldBankRequest(BaseModel):
    countries: Optional[List[dict]] = None  # [{"country_iso": "US", "country_name": "United States", "rule_id": "US-COSTS"}]
    fetch_all_active: Optional[bool] = False  # Wenn true: alle aktiven GROUP B Länder aus config_rules


@router.post("/fetch-worldbank")
async def fetch_worldbank(request: WorldBankRequest):
    """
    Holt World Bank Kostendaten für GROUP B Länder
    und speichert sie direkt in discovered_urls als useful/pending

    POST /fetch-worldbank
    Body Option A: { "countries": [{"country_iso": "US", "country_name": "United States", "rule_id": "US-COSTS"}] }
    Body Option B: { "fetch_all_active": true }  → holt alle aktiven GROUP B Länder aus config_rules
    """

    countries_to_fetch = []

    # Option B: Alle aktiven GROUP B Länder aus config_rules
    if request.fetch_all_active:
        try:
            response = supabase.table("config_rules").select(
                "country_iso, country_name, rule_id"
            ).eq("active", True).like("target_group", "%GROUP B%").execute()

            countries_to_fetch = [
                {
                    "country_iso": r["country_iso"],
                    "country_name": r["country_name"],
                    "rule_id": r["rule_id"]
                }
                for r in response.data
            ]
            logger.info(f"📋 Fetching World Bank data for {len(countries_to_fetch)} active GROUP B countries")

        except Exception as e:
            logger.error(f"❌ Failed to fetch config_rules: {str(e)}")
            return {"success": False, "error": str(e)}

    # Option A: Direkte Länder-Liste
    elif request.countries:
        countries_to_fetch = request.countries

    else:
        return {"success": False, "error": "Provide either 'countries' list or 'fetch_all_active': true"}

    # Für jedes Land Daten holen und speichern
    results = []

    for country in countries_to_fetch:
        country_iso = country["country_iso"]
        country_name = country["country_name"]
        rule_id = country["rule_id"]

        logger.info(f"🌍 Fetching World Bank data for {country_name} ({country_iso})...")

        try:
            # Alle Indikatoren parallel holen
            indicators = await fetch_all_indicators(country_iso)

            # Zähle erfolgreiche Indikatoren
            successful = sum(1 for v in indicators.values() if v and v.get("value") is not None)

            if successful == 0:
                logger.warning(f"⚠️ No data found for {country_iso}")
                results.append({
                    "country_iso": country_iso,
                    "country_name": country_name,
                    "success": False,
                    "indicators_fetched": 0,
                    "error": "No data available"
                })
                continue

            # Markdown bauen
            markdown = build_markdown(country_name, country_iso, indicators)

            # In Supabase speichern
            saved = save_worldbank_to_supabase(country_iso, country_name, rule_id, markdown)

            results.append({
                "country_iso": country_iso,
                "country_name": country_name,
                "success": saved,
                "indicators_fetched": successful,
                "markdown_length": len(markdown)
            })

        except Exception as e:
            logger.error(f"❌ Error processing {country_iso}: {str(e)}")
            results.append({
                "country_iso": country_iso,
                "country_name": country_name,
                "success": False,
                "error": str(e)
            })

    successful_countries = sum(1 for r in results if r.get("success"))

    logger.info(f"✅ World Bank fetch complete: {successful_countries}/{len(results)} countries saved")

    return {
        "success": True,
        "total_countries": len(results),
        "successful": successful_countries,
        "failed": len(results) - successful_countries,
        "results": results
    }
