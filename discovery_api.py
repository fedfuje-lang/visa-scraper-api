"""
URL Discovery API for Visa Scraper
FastAPI Service deployed on Railway.app
Calls Python discovery logic and returns results to n8n
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import tldextract
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from supabase import create_client, Client
import os
from typing import Optional, List, Dict
import logging

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI App
app = FastAPI(
    title="Visa Scraper Discovery API",
    description="URL Discovery Service for Visa Immigration Data Scraping",
    version="1.0.0"
)

# CORS Middleware (für n8n)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase Connection (aus Environment Variables)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set as environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================================================================
# KEYWORDS CONFIG (4 GRUPPEN)
# =============================================================================

GROUP_KEYWORDS = {
    "GROUP A: VISA & RECHT": [
        "visa", "residence", "permit", "immigration", "documents", 
        "requirements", "processing", "application", "green card",
        "citizenship", "work permit", "entry", "border", "consulate",
        "embassy", "naturalization", "permanent residence", "asylum",
        "refugee", "deportation", "overstay", "grace period",
        "sponsorship", "petition", "adjustment of status", "i-140", "i-485",
        "eb-1", "eb-2", "eb-3", "eb-5", "h-1b", "l-1", "o-1",
        "legal status", "authorization", "immigration law", "attorney",
        "lawyer", "appeal", "denial", "approval", "biometrics"
    ],
    "GROUP B: FINANZEN": [
        "cost", "fee", "price", "payment", "finance", "expense",
        "budget", "money", "salary", "income", "tax", "taxes",
        "living cost", "cost of living", "rent", "housing cost",
        "food cost", "grocery", "transport cost", "utilities",
        "insurance", "health insurance", "bank", "banking",
        "account", "credit card", "currency", "exchange rate",
        "minimum wage", "average salary", "income tax", "vat",
        "social security", "pension", "savings", "investment",
        "financial", "afford", "expensive", "cheap", "pricing"
    ],
    "GROUP E: BILDUNG": [
        "education", "school", "university", "college", "study",
        "student", "degree", "diploma", "certificate", "course",
        "program", "major", "bachelor", "master", "phd", "doctorate",
        "tuition", "tuition fee", "scholarship", "grant", "financial aid",
        "admission", "enrollment", "application", "requirements",
        "international student", "exchange student", "language course",
        "language school", "language test", "toefl", "ielts",
        "kindergarten", "preschool", "elementary", "secondary",
        "high school", "private school", "public school",
        "international school", "curriculum", "semester", "academic year",
        "grades", "transcript", "credits", "exam", "test",
        "library", "campus", "dormitory", "student visa", "f-1",
        "vocational training", "apprenticeship", "qualification recognition"
    ],
    "GROUP F: BÜROKRATIE": [
        "registration", "register", "bureaucracy", "administration",
        "office", "authority", "government", "municipal", "city hall",
        "police registration", "residence registration", "anmeldung",
        "form", "document", "certificate", "notary", "notarization",
        "apostille", "authentication", "translation", "certified copy",
        "passport", "id card", "identity document", "national id",
        "driver license", "driving license", "international permit",
        "vehicle registration", "car registration", "insurance",
        "mandatory insurance", "social security number", "tax id",
        "tax number", "tax office", "filing", "deadline",
        "mobile phone", "sim card", "phone contract", "address",
        "change of address", "utility contract", "electricity contract",
        "shipping", "relocation", "moving", "customs", "import",
        "export", "flight", "ticket", "travel document", "visa application fee"
    ]
}

GENERAL_KEYWORDS = [
    "immigrant", "expat", "foreigner", "international", "foreign national",
    "non-citizen", "relocate", "relocation", "moving to", "living in",
    "information", "guide", "how to", "requirements", "process", "procedure"
]

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def is_internal(url: str, base_domain: str) -> bool:
    try:
        ext = tldextract.extract(url)
        current_domain = f"{ext.domain}.{ext.suffix}"
        return current_domain == base_domain
    except:
        return False

def score_url(url: str, text: str, target_group: str) -> int:
    score = 0
    url_lower = url.lower()
    text_lower = text.lower()
    
    general_matches = sum(1 for kw in GENERAL_KEYWORDS if kw in url_lower or kw in text_lower)
    score += min(general_matches, 2)
    
    if target_group in GROUP_KEYWORDS:
        group_kws = GROUP_KEYWORDS[target_group]
        url_matches = sum(1 for kw in group_kws if kw in url_lower)
        score += min(url_matches * 2, 6)
        text_matches = sum(1 for kw in group_kws if kw in text_lower)
        score += min(text_matches, 4)
    
    if len(text) > 3000:
        score += 2
    elif len(text) > 1500:
        score += 1
    if len(text) < 200:
        score = max(1, score - 2)
    
    return max(1, min(score, 10))

def extract_topics(text: str, url: str, target_group: str) -> List[str]:
    topics = []
    text_lower = text.lower()
    url_lower = url.lower()
    
    topic_maps = {
        "GROUP A: VISA & RECHT": {
            "tourist visa": ["tourist", "visitor", "b-2", "tourism"],
            "work visa": ["work permit", "employment authorization", "h-1b", "l-1"],
            "student visa": ["student", "f-1", "study"],
            "green card": ["green card", "permanent residence", "eb-1", "eb-2"],
            "citizenship": ["citizenship", "naturalization"],
            "visa application": ["application", "form", "filing"],
            "legal rights": ["legal", "rights", "law", "attorney"]
        },
        "GROUP B: FINANZEN": {
            "living costs": ["cost of living", "living expenses"],
            "housing costs": ["rent", "housing cost"],
            "taxes": ["tax", "income tax", "vat"],
            "banking": ["bank", "account", "credit card"]
        },
        "GROUP E: BILDUNG": {
            "universities": ["university", "college"],
            "schools": ["school", "kindergarten", "elementary"],
            "tuition fees": ["tuition", "fee", "scholarship"],
            "admission": ["admission", "application", "enrollment"]
        },
        "GROUP F: BÜROKRATIE": {
            "registration": ["registration", "anmeldung", "register"],
            "documents": ["document", "certificate", "form"],
            "id documents": ["passport", "id card"],
            "driver license": ["driver license", "driving permit"]
        }
    }
    
    if target_group in topic_maps:
        topic_map = topic_maps[target_group]
        for topic, keywords in topic_map.items():
            if any(kw in text_lower or kw in url_lower for kw in keywords):
                topics.append(topic)
    
    return topics[:5]

# =============================================================================
# MAIN DISCOVERY FUNCTION
# =============================================================================

async def discover_urls(rule: Dict) -> List[Dict]:
    start_url = rule['target_url']
    max_pages = rule['max_urls']
    max_depth = rule['max_depth']
    
    ext = tldextract.extract(start_url)
    base_domain = f"{ext.domain}.{ext.suffix}"
    
    visited = set()
    to_visit = [(start_url, 0)]
    discovered_urls = []
    
    logger.info(f"🚀 Starting discovery for: {rule['country_name']} ({rule['rule_id']})")
    logger.info(f"📊 Max URLs: {max_pages}, Max Depth: {max_depth}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        context = await browser.new_context()
        
        while to_visit and len(visited) < max_pages:
            current_url, depth = to_visit.pop(0)
            
            if current_url in visited or depth > max_depth:
                continue
            
            visited.add(current_url)
            logger.info(f"🔎 Crawling ({len(visited)}/{max_pages}, depth={depth}): {current_url}")
            
            try:
                page = await context.new_page()
                await page.goto(current_url, timeout=30000, wait_until="domcontentloaded")
                
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                
                title_tag = soup.find("title")
                page_title = title_tag.get_text(strip=True) if title_tag else ""
                
                relevance = score_url(current_url, text, rule['target_group'])
                topics = extract_topics(text, current_url, rule['target_group'])
                
                if relevance >= 3:
                    discovered_urls.append({
                        "url": current_url,
                        "page_title": page_title[:500],
                        "relevance_score": relevance,
                        "topics": topics,
                        "discovered_depth": depth,
                        "text_length": len(text),
                        "rule_id": rule['rule_id'],
                        "country_code": rule['country_iso'],
                        "country_name": rule['country_name'],
                        "target_group": rule['target_group']
                    })
                
                if depth < max_depth:
                    for a_tag in soup.select("a[href]"):
                        href = a_tag.get("href", "").strip()
                        if not href:
                            continue
                        
                        full_url = urljoin(current_url, href)
                        if not full_url.startswith("http"):
                            continue
                        
                        if is_internal(full_url, base_domain):
                            if full_url not in visited:
                                if full_url not in [u for u, d in to_visit]:
                                    to_visit.append((full_url, depth + 1))
                
                await page.close()
                
            except Exception as e:
                logger.error(f"⚠️ Error crawling {current_url}: {str(e)}")
                continue
        
        await browser.close()
    
    logger.info(f"✅ Discovery complete: {len(discovered_urls)} URLs found")
    return discovered_urls

# =============================================================================
# SUPABASE FUNCTIONS
# =============================================================================

def save_urls_to_supabase(discovered_urls: List[Dict]) -> int:
    if not discovered_urls:
        logger.warning("⚠️ No URLs to save")
        return 0
    
    logger.info(f"💾 Saving {len(discovered_urls)} URLs to Supabase...")
    
    insert_data = []
    for url_data in discovered_urls:
        insert_data.append({
            "url": url_data["url"],
            "page_title": url_data["page_title"],
            "relevance_score": url_data["relevance_score"],
            "topics": url_data["topics"],
            "discovered_depth": url_data["discovered_depth"],
            "rule_id": url_data["rule_id"],
            "country_code": url_data["country_code"],
            "country_name": url_data["country_name"],
            "target_group": url_data["target_group"],
            "status": "pending"
        })
    
    try:
        response = supabase.table("discovered_urls").upsert(
            insert_data,
            on_conflict="url"
        ).execute()
        
        inserted_count = len(response.data) if response.data else 0
        logger.info(f"✅ {inserted_count} URLs saved successfully")
        return inserted_count
        
    except Exception as e:
        logger.error(f"❌ Error saving to Supabase: {str(e)}")
        return 0

def update_last_crawled(rule_id: str):
    try:
        supabase.table("config_rules").update({
            "last_crawled_at": "now()"
        }).eq("rule_id", rule_id).execute()
        logger.info(f"✅ Updated last_crawled_at for {rule_id}")
    except Exception as e:
        logger.warning(f"⚠️ Could not update last_crawled_at: {str(e)}")

# =============================================================================
# API ENDPOINTS
# =============================================================================

class DiscoveryRequest(BaseModel):
    trigger: str = "manual"
    rule_ids: Optional[List[str]] = None
    filter: Optional[Dict[str, str]] = None
    max_urls: Optional[int] = None

class DiscoveryResponse(BaseModel):
    success: bool
    total_rules_processed: int
    total_urls_found: int
    successful_rules: int
    failed_rules: int
    results_per_rule: List[Dict]

@app.get("/")
async def root():
    return {
        "service": "Visa Scraper Discovery API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "discover": "/discover",
            "health": "/health"
        }
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "supabase_connected": bool(SUPABASE_URL and SUPABASE_KEY)
    }

@app.post("/discover", response_model=DiscoveryResponse)
async def run_discovery(request: DiscoveryRequest):
    """
    Main endpoint: Runs URL discovery for all active rules
    Called by n8n HTTP Request Node
    
    Supports:
    - rule_ids: List of specific rule IDs to process
    - filter: Dict with country_iso and/or target_group
    - max_urls: Override max_urls from config_rules
    """
    
    logger.info("="*80)
    logger.info("🚀 DISCOVERY API STARTED")
    logger.info(f"📋 Request: {request.dict()}")
    logger.info("="*80)
    
    try:
        # Load active rules from Supabase
        query = supabase.table("config_rules").select("*").eq("active", True)
        
        # Filter by rule_ids if provided
        if request.rule_ids:
            query = query.in_("rule_id", request.rule_ids)
            logger.info(f"🔍 Filtering by rule_ids: {request.rule_ids}")
        
        # Filter by country_iso and/or target_group if provided
        if request.filter:
            if "country_iso" in request.filter:
                query = query.eq("country_iso", request.filter["country_iso"])
                logger.info(f"🔍 Filtering by country_iso: {request.filter['country_iso']}")
            if "target_group" in request.filter:
                query = query.eq("target_group", request.filter["target_group"])
                logger.info(f"🔍 Filtering by target_group: {request.filter['target_group']}")
        
        response = query.execute()
        rules = response.data
        
        if not rules:
            logger.warning("⚠️ No active rules found matching criteria")
            return DiscoveryResponse(
                success=False,
                total_rules_processed=0,
                total_urls_found=0,
                successful_rules=0,
                failed_rules=0,
                results_per_rule=[]
            )
        
        logger.info(f"✅ Found {len(rules)} active rules")
        
        # Process each rule
        total_urls_found = 0
        results_per_rule = []
        
        for i, rule in enumerate(rules, 1):
            logger.info(f"\n{'='*80}")
            logger.info(f"📍 Rule {i}/{len(rules)}: {rule['rule_id']}")
            logger.info(f"🌍 Country: {rule['country_name']}")
            logger.info(f"📂 Group: {rule['target_group']}")
            logger.info(f"{'='*80}")
            
            try:
                # Override max_urls if provided in request
                if request.max_urls:
                    rule['max_urls'] = request.max_urls
                    logger.info(f"🔧 Overriding max_urls to {request.max_urls}")
                
                discovered_urls = await discover_urls(rule)
                saved_count = save_urls_to_supabase(discovered_urls)
                update_last_crawled(rule['rule_id'])
                
                total_urls_found += saved_count
                results_per_rule.append({
                    "rule_id": rule['rule_id'],
                    "country": rule['country_name'],
                    "target_group": rule['target_group'],
                    "urls_found": saved_count,
                    "success": True
                })
                
            except Exception as e:
                logger.error(f"❌ Error processing rule {rule['rule_id']}: {str(e)}")
                results_per_rule.append({
                    "rule_id": rule['rule_id'],
                    "country": rule['country_name'],
                    "target_group": rule.get('target_group', 'unknown'),
                    "urls_found": 0,
                    "success": False,
                    "error": str(e)
                })
        
        logger.info(f"\n{'='*80}")
        logger.info("✅ DISCOVERY API COMPLETED")
        logger.info(f"📊 Total URLs found: {total_urls_found}")
        logger.info(f"{'='*80}")
        
        return DiscoveryResponse(
            success=True,
            total_rules_processed=len(rules),
            total_urls_found=total_urls_found,
            successful_rules=sum(1 for r in results_per_rule if r['success']),
            failed_rules=sum(1 for r in results_per_rule if not r['success']),
            results_per_rule=results_per_rule
        )
        
    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# =============================================================================
# STARTUP
# =============================================================================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting Visa Scraper Discovery API...")
    logger.info(f"Supabase URL: {SUPABASE_URL}")
    logger.info("✅ API is ready!")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
