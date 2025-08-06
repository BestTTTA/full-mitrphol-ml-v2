# main-ml.py  (v3.8.0 — Database version: PostgreSQL + range-based caching + last-month feature mapping (fixed) + viewport consistency + feature averaging + streaming aggregation + score normalization + model-specific months + prediction result cache)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from typing import List, Dict, Any, Optional
import pandas as pd
import xgboost as xgb
import joblib
import os
import numpy as np
from pydantic import BaseModel
import asyncio
import logging
import json
import hashlib
from datetime import datetime
from dotenv import load_dotenv
import gc
import psutil
import threading
from queue import Queue
import time
import pickle
import aiofiles
from pathlib import Path
from collections import defaultdict, deque
import asyncpg
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://admin:adminpassword@119.59.102.60:5678/mitrphol_v2"
)

# Parse DATABASE_URL for connection parameters
db_url = urlparse(DATABASE_URL)
DB_CONFIG = {
    "host": db_url.hostname,
    "port": db_url.port,
    "database": db_url.path[1:],  # Remove leading '/'
    "user": db_url.username,
    "password": db_url.password,
}

# Database connection pool
db_pool = None

async def init_db_pool():
    """Initialize asyncpg connection pool"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=300,  # Increased timeout
            server_settings={
                'application_name': 'ml_prediction_service',
            }
        )
        logger.info("Database connection pool initialized")
        
        # Test the connection
        async with db_pool.acquire() as conn:
            result = await conn.fetchval("SELECT COUNT(*) FROM correct_data LIMIT 1")
            logger.info(f"Database connection test successful. Sample count: {result}")
            
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}")
        raise

async def close_db_pool():
    """Close database connection pool"""
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("Database connection pool closed")

# ควบคุม concurrency เบื้องต้น
MAX_WORKERS = min(4, os.cpu_count() or 4)
processing_queue = Queue(maxsize=10)
processing_lock = threading.Semaphore(MAX_WORKERS)

# Request deduplication - prevent duplicate simultaneous requests
ACTIVE_REQUESTS = {}  # cache_key -> asyncio.Event
ACTIVE_REQUESTS_LOCK = None  # Will be initialized at startup

app = FastAPI(
    title="ML Prediction Service - Database Version (Range-based Cache + Last-Month Features + Viewport-fast + Streaming Averages + Prediction Cache)",
    version="3.8.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Add GZip compression for responses > 1KB
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

# --------- Config (ปรับได้ผ่าน env) ----------
MODELS_DIR = os.getenv("MODELS_DIR", "models-current")

# Database query settings
DB_FETCH_SIZE = int(os.getenv("DB_FETCH_SIZE", "100000"))  # Records per batch from database
DB_TIMEOUT = int(os.getenv("DB_TIMEOUT", "600"))  # Query timeout in seconds (increased to 10 minutes)

CACHE_TTL = int(os.getenv("CACHE_TTL", "31536000"))
CACHE_PREFIX = os.getenv("CACHE_PREFIX", "ml_predict")
CACHE_DIR = os.getenv("CACHE_DIR", "cache")

# batch สำหรับพยากรณ์ใน XGBoost - increased for better performance
BATCH_PREDICTION_SIZE = int(os.getenv("BATCH_PREDICTION_SIZE", "1000000"))

# Map display limit
MAP_DISPLAY_LIMIT = os.getenv("MAP_DISPLAY_LIMIT", "1000")

# เกณฑ์ระดับ prediction
PREDICTION_THRESHOLDS = {
    "HIGH_MIN": float(os.getenv("HIGH_MIN", "12.0")),
    "MEDIUM_MIN": float(os.getenv("MEDIUM_MIN", "10.0")),
    "MEDIUM_MAX": float(os.getenv("MEDIUM_MAX", "12.0")),
    "LOW_MAX": float(os.getenv("LOW_MAX", "10.0"))
}

# ปรับค่า prediction ตามโซน (อ่านจาก .env)
ZONE_PREDICTION_ADJUSTMENTS = {
    "MPK": float(os.getenv("ADJUSTMENT_MPK", "0.5")),
    "MKS": float(os.getenv("ADJUSTMENT_MKS", "0.5")),
    "MAC": float(os.getenv("ADJUSTMENT_MAC", "0.5")),
    "MPDC": float(os.getenv("ADJUSTMENT_MPDC", "-0.5")),
    "SB": float(os.getenv("ADJUSTMENT_SB", "-0.5"))
}

# แม็พ model -> ปี (อ่านจาก .env)
MODEL_YEAR_CONFIG = {
    "m12": int(os.getenv("M12_YEAR", "2025")),
    "m1": int(os.getenv("M1_YEAR", "2025")),
    "m2": int(os.getenv("M2_YEAR", "2025")),
    "m3": int(os.getenv("M3_YEAR", "2025"))
}

# Log configuration on startup
logger.info(f"MODEL_YEAR_CONFIG loaded: {MODEL_YEAR_CONFIG}")

MODEL_MONTH_RANGE = {
    "m12": os.getenv("M12_MONTH_RANGE", "2-8"),
    "m1": os.getenv("M1_MONTH_RANGE", "2-8"),
    "m2": os.getenv("M2_MONTH_RANGE", "2-8"),
    "m3": os.getenv("M3_MONTH_RANGE", "2-8"),
}

# Model-specific feature cache configuration
# เก็บ cache แยกตาม (year, start_month, end_month, model_name)
# เพื่อให้แต่ละ model ใช้ cache ของตัวเองตาม features ที่ต้องการ
MODEL_FEATURE_CACHE = {}  # cache_key -> cached data

def get_model_month_range(model_name: str) -> tuple[int, int]:
    """
    ดึง start_month และ end_month สำหรับ model ที่ระบุ
    Returns: (start_month, end_month)
    """
    range_str = MODEL_MONTH_RANGE.get(model_name, "2-8")  # default 2-8
    try:
        start, end = map(int, range_str.split("-"))
        # Validate range
        if not (1 <= start <= 12 and 1 <= end <= 12 and start <= end):
            logger.warning(f"Invalid month range for {model_name}: {range_str}, using default 2-8")
            return (2, 8)
        return (start, end)
    except Exception as e:
        logger.error(f"Error parsing month range for {model_name}: {e}, using default 2-8")
        return (2, 8)

def get_month_names_from_range(start_month: int, end_month: int) -> List[str]:
    """
    แปลง month range (เลข) เป็นชื่อเดือนภาษาอังกฤษ
    Returns: List of month names (e.g., ['February', 'March', 'April', ...])
    """
    month_names = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return [month_names[i-1] for i in range(start_month, end_month + 1)]

ALL_ZONES = "MAC,MKB,MKS,MPDC,MPK,MPL,MPV,SB"
YEAR_ZONES: Dict[int, str] = {
    2024: ALL_ZONES,
    2025: ALL_ZONES,
}

def zones_for_year(year: int, fallback: Optional[str] = None) -> str:
    return YEAR_ZONES.get(year) or fallback or ALL_ZONES

# ----------------- Pydantic models (same as before) -----------------
class GroupedPredictionRequest(BaseModel):
    year: int
    start_month: int
    end_month: int
    models: List[str]
    zones: Optional[str] = ALL_ZONES
    limit: Optional[int] = 1000000
    group_by_level: Optional[bool] = True
    display_month: Optional[int] = 6
    enable_chunked_processing: Optional[bool] = True
    max_concurrent_models: Optional[int] = 2
    # viewport filter
    min_lat: Optional[float] = None
    min_lng: Optional[float] = None
    max_lat: Optional[float] = None
    max_lng: Optional[float] = None

class GroupedPlantPrediction(BaseModel):
    lat: float
    lon: float
    plant_id: str
    prediction: float
    prediction_level: str
    ndvi: float
    ndwi: float
    gli: float
    precipitation: float
    zone: str
    cane_type: str
    display_month: int
    display_month_name: str
    year: Optional[int] = None  # Added: year of the data
    month: Optional[int] = None  # Added: month of the data
    # New fields from correct_data table
    f_id: Optional[int] = None
    gis_id: Optional[int] = None
    gis_idkey: Optional[str] = None
    gis_area: Optional[float] = None
    crop_year: Optional[int] = None
    gis_sta: Optional[str] = None
    giscode: Optional[str] = None
    qoata_id: Optional[int] = None
    farmmer_pre: Optional[str] = None
    farmmer_first_name: Optional[str] = None
    farmmer_last_name: Optional[str] = None
    zone_id: Optional[int] = None
    sub_zone: Optional[str] = None
    fac: Optional[int] = None
    gis_cane_type: Optional[int] = None
    factory: Optional[str] = None
    type_owner_idgis: Optional[int] = None
    type_owner_idAC: Optional[int] = None

class PredictionGroup(BaseModel):
    level: str
    count: int
    percentage: float
    average_prediction: float
    predictions: List[GroupedPlantPrediction]

class ZoneStatistics(BaseModel):
    zone: str
    high_prediction_count: int
    high_prediction_percentage: float
    medium_prediction_count: int
    medium_prediction_percentage: float
    low_prediction_count: int
    low_prediction_percentage: float
    total_plantations: int
    average_prediction: float

class GroupedModelPredictionResult(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_name: str
    prediction_groups: List[PredictionGroup]
    zone_statistics: List[ZoneStatistics]
    overall_average: float
    total_predictions: int

class GroupedPredictionResponse(BaseModel):
    success: bool
    message: str
    results: List[GroupedModelPredictionResult]
    cached: Optional[bool] = False
    cache_key: Optional[str] = None
    processing_stats: Optional[Dict[str, Any]] = None

class ModelAverageResult(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_name: str
    overall_average: float
    total_predictions: int
    high_percentage: float
    medium_percentage: float
    low_percentage: float
    zone_averages: Dict[str, float]
    zone_statistics: Optional[List[ZoneStatistics]] = []  # เพิ่ม zone statistics

class AverageCalculationResponse(BaseModel):
    success: bool
    message: str
    individual_model_results: List[ModelAverageResult]
    combined_average_result: Dict[str, Any]
    processing_stats: Optional[Dict[str, Any]] = None
    cached: Optional[bool] = False
    cache_key: Optional[str] = None

# ----------------- ยูทิล cache (same as before) -----------------
def generate_cache_key(request_type: str, **params) -> str:
    sorted_params = dict(sorted(params.items()))
    for key, value in sorted_params.items():
        if isinstance(value, list):
            sorted_params[key] = sorted(value)
    param_string = json.dumps(sorted_params, sort_keys=True)
    param_hash = hashlib.md5(param_string.encode()).hexdigest()
    return f"{CACHE_PREFIX}_{request_type}_{param_hash}"

def get_cache_file_path(cache_key: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_key}.pkl")

async def get_from_cache(cache_key: str):
    try:
        cache_file = get_cache_file_path(cache_key)
        if not os.path.exists(cache_file):
            return None
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age > CACHE_TTL:
            os.remove(cache_file)
            return None
        async with aiofiles.open(cache_file, 'rb') as f:
            content = await f.read()
            cached_data = pickle.loads(content)
        return cached_data
    except Exception:
        return None

async def set_to_cache(cache_key: str, data: Any, ttl: int = CACHE_TTL):
    try:
        cache_file = get_cache_file_path(cache_key)
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        async with aiofiles.open(cache_file, 'wb') as f:
            await f.write(pickle.dumps({"data": data, "cached_at": datetime.now().isoformat(), "ttl": ttl}))
        return True
    except Exception:
        return False

async def delete_cache_pattern(pattern: str):
    try:
        cache_dir = Path(CACHE_DIR)
        if not cache_dir.exists():
            return 0
        pattern_key = pattern.replace(f"{CACHE_PREFIX}:*", f"{CACHE_PREFIX}_*")
        deleted = 0
        for cache_file in cache_dir.glob("*.pkl"):
            if pattern_key.replace("*", "") in cache_file.name:
                cache_file.unlink()
                deleted += 1
        return deleted
    except Exception:
        return 0

# ----------------- Database functions -----------------
async def build_base_query(
    year: int,
    start_month: int,
    end_month: int,
    zones: str,
    viewport: Optional[Dict[str, float]] = None,
    limit: Optional[int] = None
) -> tuple[str, list]:
    """Build base SQL query with parameters including viewport filtering"""
    where_conditions = []
    params = []
    param_idx = 1

    # Year filter
    where_conditions.append(f"year = ${param_idx}")
    params.append(year)
    param_idx += 1

    # Month range filter
    where_conditions.append(f"month >= ${param_idx}")
    params.append(start_month)
    param_idx += 1

    where_conditions.append(f"month <= ${param_idx}")
    params.append(end_month)
    param_idx += 1

    # Zone filter
    if zones:
        zone_list = [z.strip() for z in zones.split(",") if z.strip()]
        if zone_list:
            placeholders = ",".join([f"${param_idx + i}" for i in range(len(zone_list))])
            where_conditions.append(f"zone IN ({placeholders})")
            params.extend(zone_list)
            param_idx += len(zone_list)

    # Viewport filter - query directly in WHERE clause
    if viewport:
        where_conditions.append(f"lat BETWEEN ${param_idx} AND ${param_idx + 1}")
        params.extend([viewport["min_lat"], viewport["max_lat"]])
        param_idx += 2

        where_conditions.append(f"lng BETWEEN ${param_idx} AND ${param_idx + 1}")
        params.extend([viewport["min_lng"], viewport["max_lng"]])
        param_idx += 2

    # Use TABLESAMPLE for faster random sampling on large tables (if no viewport)
    # TABLESAMPLE is much faster than ORDER BY RANDOM() LIMIT
    sample_clause = ""
    if limit and limit < 100000 and not viewport:
        # Sample 10% of table for faster query (adjust percentage as needed)
        sample_clause = "TABLESAMPLE BERNOULLI (10)"

    base_query = f"""
        SELECT gis_id, f_id, gis_idkey, gis_area, crop_year, gis_sta, giscode,
               qoata_id, farmmer_pre, farmmer_first_name, farmmer_last_name,
               zone_id, sub_zone, fac, gis_cane_type, cane_type, factory,
               type_owner_idgis, type_owner_idAC, geo, stx, sty, lat, lng,
               ndvi, gli, ndwi, cigreen, pvr, soil_tempt, tempt, solar_radiation,
               soil_moisture, precipitation, month, year, zone, cane_type_eng
        FROM correct_data {sample_clause}
        WHERE {' AND '.join(where_conditions)}
    """

    # Note: LIMIT is added AFTER ORDER BY in the calling function
    return base_query, params, limit

async def read_raw_data_from_database(
    year: int,
    start_month: int,
    end_month: int,
    zones: str,
    limit: int,
    viewport: Optional[Dict[str, float]] = None
) -> List[Dict[str, Any]]:
    """Read data from PostgreSQL database - viewport filtered in query"""
    global db_pool
    if not db_pool:
        raise RuntimeError("Database pool not initialized")

    # Build query with viewport filter included
    base_query, params, query_limit = await build_base_query(
        year=year,
        start_month=start_month,
        end_month=end_month,
        zones=zones,
        viewport=viewport,
        limit=limit  # Use SQL LIMIT directly
    )

    # Add ORDER BY for consistent results - sort by month DESC to get latest month first
    # This ensures we get the most recent data when there are duplicates
    base_query += " ORDER BY month DESC, id"
    if query_limit and query_limit > 0:
        base_query += f" LIMIT {query_limit}"

    try:
        async with db_pool.acquire() as conn:
            # Set query timeout
            await conn.execute(f"SET statement_timeout = '{DB_TIMEOUT}s'")

            logger.info(f"Executing viewport query: viewport={viewport is not None}, limit={limit}")

            # Single query - no chunking needed with viewport + limit
            rows = await conn.fetch(base_query, *params)

            if not rows:
                logger.info("No rows found")
                return []

            # Convert asyncpg records to dictionaries
            results = []
            for row in rows:
                record = dict(row)

                # Generate plant_id from f_id if available, otherwise use lat/lng
                if not record.get("plant_id"):
                    if record.get("f_id"):
                        record["plant_id"] = str(record["f_id"])
                    elif record.get("lat") and record.get("lng"):
                        record["plant_id"] = f"{float(record['lat']):.6f}_{float(record['lng']):.6f}"

                # Handle null values - use factory as zone if zone is empty
                record["zone"] = record.get("zone") or record.get("factory") or "Unknown"
                record["cane_type"] = record.get("cane_type_eng") or record.get("cane_type") or "Unknown"

                # Ensure numeric fields are properly typed
                for field in ["lat", "lng", "ndvi", "gli", "ndwi", "precipitation"]:
                    if record.get(field) is not None:
                        record[field] = float(record[field])

                results.append(record)

            logger.info(f"Retrieved {len(results)} records from database (viewport-filtered)")
            return results

    except Exception as e:
        logger.error(f"Database query failed: {e}")
        raise

# ----------------- Database-based data streaming for averages -----------------
async def process_model_predictions_streaming_db(
    model_name: str,
    year: int,
    start_month: int,
    end_month: int,
    zones_str: str
) -> GroupedModelPredictionResult:
    """Process model predictions using month-based cached data"""
    start_t = time.time()

    model = load_model(model_name)
    required_features = model["required_features"]
    booster = model["booster"]

    # Use month-based cache to get data
    logger.info(f"Fetching data for {model_name} using month-based cache")
    raw_data = await get_multi_month_data_cached(year, start_month, end_month, zones_str)

    if not raw_data:
        logger.warning(f"No data found for {model_name}")
        return GroupedModelPredictionResult(
            model_name=model_name,
            prediction_groups=[],
            zone_statistics=[],
            overall_average=0.0,
            total_predictions=0
        )

    acc: Dict[str, Dict[str, Any]] = {}
    total_rows_seen = 0

    # Process data in batches
    batch_size = 10000  # Process in larger batches since data is already cached

    try:
        for i in range(0, len(raw_data), batch_size):
            batch_records = raw_data[i:i+batch_size]

            # Process this batch
            _process_batch_records(batch_records, acc, required_features)
            total_rows_seen += len(batch_records)

            logger.info(f"Processed {len(batch_records)} rows for {model_name}, total plants: {len(acc)}")

            # Clean up
            gc.collect()

    except Exception as e:
        logger.error(f"Data processing failed for {model_name}: {e}")
        raise
    
    if not acc:
        return GroupedModelPredictionResult(
            model_name=model_name,
            prediction_groups=[],
            zone_statistics=[],
            overall_average=0.0,
            total_predictions=0
        )
    
    # Process predictions in batches
    sums_by_zone: Dict[str, float] = defaultdict(float)
    counts_by_zone: Dict[str, int] = defaultdict(int)
    high_by_zone: Dict[str, int] = defaultdict(int)
    med_by_zone: Dict[str, int] = defaultdict(int)
    low_by_zone: Dict[str, int] = defaultdict(int)
    
    total_sum = 0.0
    total_cnt = 0
    
    for df_batch in _accumulator_to_batches(acc, required_features, BATCH_PREDICTION_SIZE):
        feats = df_batch[required_features].fillna(0.0)
        dmat = xgb.DMatrix(feats, feature_names=required_features)
        raw_scores = booster.predict(dmat)
        del dmat
        
        # Normalize -> 1D vector
        scores = _normalize_scores_to_1d(raw_scores)
        zones_arr = df_batch["zone"].astype(str).tolist()
        
        # Apply zone adjustments
        for i, z in enumerate(zones_arr):
            scores[i] += float(ZONE_PREDICTION_ADJUSTMENTS.get(z, 0.0))
        
        # Accumulate statistics
        for i in range(len(scores)):
            z = zones_arr[i] or "Unknown"
            v = float(scores[i])
            sums_by_zone[z] += v
            counts_by_zone[z] += 1
            total_sum += v
            total_cnt += 1
            
            if v > PREDICTION_THRESHOLDS["HIGH_MIN"]:
                high_by_zone[z] += 1
            elif v >= PREDICTION_THRESHOLDS["MEDIUM_MIN"] and v <= PREDICTION_THRESHOLDS["MEDIUM_MAX"]:
                med_by_zone[z] += 1
            else:
                low_by_zone[z] += 1
        
        del df_batch, feats, scores, zones_arr
        gc.collect()
    
    # Build zone statistics
    zone_stats: List[ZoneStatistics] = []
    for z in sorted(counts_by_zone.keys()):
        total = counts_by_zone[z]
        hi = high_by_zone[z]
        md = med_by_zone[z]
        lo = low_by_zone[z]
        avg = (sums_by_zone[z] / total) if total else 0.0
        zone_stats.append(ZoneStatistics(
            zone=z,
            high_prediction_count=hi,
            high_prediction_percentage=round(hi/total*100, 2) if total else 0.0,
            medium_prediction_count=md,
            medium_prediction_percentage=round(md/total*100, 2) if total else 0.0,
            low_prediction_count=lo,
            low_prediction_percentage=round(lo/total*100, 2) if total else 0.0,
            total_plantations=total,
            average_prediction=round(avg, 2)
        ))
    
    overall_avg = round(total_sum / total_cnt, 2) if total_cnt else 0.0
    
    logger.info(f"[DB stream] {model_name}: plants={len(acc)} rows={total_rows_seen} preds={total_cnt} time={time.time()-start_t:.2f}s")
    
    return GroupedModelPredictionResult(
        model_name=model_name,
        prediction_groups=[],      # โหมด averages ไม่คืนรายจุด
        zone_statistics=zone_stats,
        overall_average=overall_avg,
        total_predictions=total_cnt
    )

def _process_batch_records(batch_records: List[Dict], acc: Dict[str, Dict[str, Any]], required_features: List[str]):
    """Process a batch of database records into feature accumulator"""
    for rec in batch_records:
        pid = rec.get("plant_id")
        if not pid:
            continue
        
        if pid not in acc:
            row = _empty_feature_row(required_features)
            acc[pid] = row
        else:
            row = acc[pid]
        
        _update_feature_row(row, rec, required_features)

# ----------------- Memory cache for database results (month-based) -----------------
RAW_BASE_USE_MEMORY = bool(int(os.getenv("RAW_BASE_USE_MEMORY", "1")))
RAW_BASE_MEM_CACHE: Dict[str, List[Dict[str, Any]]] = {}
RAW_BASE_MEM_KEYS: List[str] = []
RAW_BASE_MEM_MAX = int(os.getenv("RAW_BASE_MEM_MAX", "50"))  # Increased for month-based caching

# ----------------- Prediction result cache (plant-level) -----------------
PREDICTION_CACHE_ENABLED = bool(int(os.getenv("PREDICTION_CACHE_ENABLED", "1")))  # Enabled by default
PREDICTION_RESULT_CACHE: Dict[tuple, Dict[str, Any]] = {}  # Key: (year, model, plant_id, month_range)
PREDICTION_CACHE_KEYS: deque = deque(maxlen=100000)  # LRU tracking
PREDICTION_CACHE_MAX_ITEMS = int(os.getenv("PREDICTION_CACHE_MAX_ITEMS", "100000"))
PREDICTION_CACHE_STATS = {"hits": 0, "misses": 0, "total_requests": 0}  # Performance tracking

def get_prediction_cache_key(year: int, model: str, plant_id: str, start_month: int, end_month: int) -> tuple:
    """Generate cache key for prediction result"""
    month_range = f"{start_month}-{end_month}"
    return (year, model.lower(), plant_id, month_range)

def _base_raw_key_db_monthly(year: int, month: int, zones: str) -> str:
    """Generate cache key for database queries by month"""
    return generate_cache_key(
        "raw_base_db_monthly_v2",
        year=year, month=month,
        zones=zones,
        db_url_hash=hashlib.md5(DATABASE_URL.encode()).hexdigest()[:8]
    )

def _base_raw_key_db(year: int, start_month: int, end_month: int, zones: str, limit: int, viewport: Optional[Dict[str, float]]) -> str:
    """Generate cache key for database queries (legacy, for viewport queries)"""
    return generate_cache_key(
        "raw_base_db_v1",
        year=year, start_month=start_month, end_month=end_month,
        zones=zones, limit=limit, viewport=viewport,
        db_url_hash=hashlib.md5(DATABASE_URL.encode()).hexdigest()[:8]
    )

async def read_monthly_data_from_database(
    year: int,
    month: int,
    zones: str
) -> List[Dict[str, Any]]:
    """Read data from PostgreSQL database for a specific month"""
    global db_pool
    if not db_pool:
        raise RuntimeError("Database pool not initialized")

    # Limit records per month to prevent slow queries
    # ปรับเป็น 200,000 ต่อเดือนเพื่อให้ได้ข้อมูลครบถ้วนมากขึ้น
    # Use 200,000 per month = ~1.4M total for 7 months (Feb-Aug)
    MAX_RECORDS_PER_MONTH = int(os.getenv("MAX_RECORDS_PER_MONTH", "500000"))

    base_query, params, _ = await build_base_query(
        year=year,
        start_month=month,
        end_month=month,
        zones=zones,
        viewport=None,
        limit=MAX_RECORDS_PER_MONTH
    )

    # Don't use ORDER BY with LIMIT - it slows down the query significantly
    # The data sampling is random enough for prediction purposes

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = '{DB_TIMEOUT}s'")

            logger.info(f"Fetching data for year={year}, month={month}, zones={zones}")
            rows = await conn.fetch(base_query, *params)

            if not rows:
                logger.info(f"No rows found for year={year}, month={month}")
                return []

            results = []
            for row in rows:
                record = dict(row)

                if not record.get("plant_id"):
                    if record.get("f_id"):
                        record["plant_id"] = str(record["f_id"])
                    elif record.get("lat") and record.get("lng"):
                        record["plant_id"] = f"{float(record['lat']):.6f}_{float(record['lng']):.6f}"

                record["zone"] = record.get("zone") or record.get("factory") or "Unknown"
                record["cane_type"] = record.get("cane_type_eng") or record.get("cane_type") or "Unknown"

                for field in ["lat", "lng", "ndvi", "gli", "ndwi", "precipitation"]:
                    if record.get(field) is not None:
                        record[field] = float(record[field])

                results.append(record)

            logger.info(f"Retrieved {len(results)} records for year={year}, month={month}")
            return results

    except Exception as e:
        logger.error(f"Database query failed for year={year}, month={month}: {e}")
        raise

async def get_monthly_data_cached(
    year: int,
    month: int,
    zones: str
) -> List[Dict[str, Any]]:
    """Get cached monthly data or fetch from database"""
    key = _base_raw_key_db_monthly(year, month, zones)

    # Check memory cache
    if RAW_BASE_USE_MEMORY and key in RAW_BASE_MEM_CACHE:
        logger.info(f"Memory cache HIT for year={year}, month={month}")
        return RAW_BASE_MEM_CACHE[key]

    # Check file cache
    cached = await get_from_cache(key)
    if cached and "data" in cached:
        data = cached["data"]
        logger.info(f"File cache HIT for year={year}, month={month}")
        if RAW_BASE_USE_MEMORY:
            RAW_BASE_MEM_CACHE[key] = data
            RAW_BASE_MEM_KEYS.append(key)
            if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
                old = RAW_BASE_MEM_KEYS.pop(0)
                RAW_BASE_MEM_CACHE.pop(old, None)
        return data

    # Fetch from database
    logger.info(f"Cache MISS - Fetching from database for year={year}, month={month}")
    data = await read_monthly_data_from_database(year=year, month=month, zones=zones)

    # Cache the result (longer TTL for monthly data)
    await set_to_cache(key, data, ttl=CACHE_TTL)

    if RAW_BASE_USE_MEMORY:
        RAW_BASE_MEM_CACHE[key] = data
        RAW_BASE_MEM_KEYS.append(key)
        if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
            old = RAW_BASE_MEM_KEYS.pop(0)
            RAW_BASE_MEM_CACHE.pop(old, None)

    return data

async def get_multi_month_data_cached(
    year: int,
    start_month: int,
    end_month: int,
    zones: str
) -> List[Dict[str, Any]]:
    """Get data for multiple months using month-based cache"""
    logger.info(f"Fetching data for year={year}, months={start_month}-{end_month}, zones={zones}")

    # Fetch each month separately to leverage caching
    all_data = []
    cached_months = []
    fetched_months = []

    for month in range(start_month, end_month + 1):
        month_data = await get_monthly_data_cached(year, month, zones)
        all_data.extend(month_data)

        # Track which months were cached vs fetched
        key = _base_raw_key_db_monthly(year, month, zones)
        if key in RAW_BASE_MEM_CACHE or await get_from_cache(key):
            cached_months.append(month)
        else:
            fetched_months.append(month)

    logger.info(f"Total records fetched: {len(all_data)} (cached months: {cached_months}, fetched months: {fetched_months})")
    return all_data

async def get_base_raw_cached_db(
    year: int,
    start_month: int,
    end_month: int,
    zones: str,
    limit: int,
    viewport: Optional[Dict[str, float]] = None
) -> List[Dict[str, Any]]:
    """Get cached database results or fetch from database - caches entire month range at once"""

    # For viewport queries, use direct query (no caching)
    if viewport:
        logger.info("Viewport query detected - using direct database query")
        return await read_raw_data_from_database(
            year=year, start_month=start_month, end_month=end_month,
            zones=zones, limit=limit, viewport=viewport
        )

    # For non-viewport queries, use single cache key for entire range
    cache_key = _base_raw_key_db(year, start_month, end_month, zones, limit, viewport)

    # Check if there's already an active request for this data
    async with ACTIVE_REQUESTS_LOCK:
        if cache_key in ACTIVE_REQUESTS:
            logger.info(f"Duplicate request detected for {cache_key}, waiting for existing request...")
            event = ACTIVE_REQUESTS[cache_key]
        else:
            # Create a new event for this request
            event = asyncio.Event()
            ACTIVE_REQUESTS[cache_key] = event
            event = None  # This request will do the fetching

    # If we're waiting for another request, wait for it to complete
    if event:
        await event.wait()
        # Data should now be in cache, try to get it
        cached = await get_from_cache(cache_key)
        if cached:
            if isinstance(cached, dict) and "data" in cached:
                return cached["data"]
            elif isinstance(cached, list):
                return cached
        # If still not found, fall through to fetch it ourselves
        logger.warning(f"Waited for duplicate request but cache still empty, fetching anyway")

    # Check memory cache
    if RAW_BASE_USE_MEMORY and cache_key in RAW_BASE_MEM_CACHE:
        logger.info(f"Memory cache HIT for year={year}, months={start_month}-{end_month}")
        # Clean up request tracking since we're returning cached data
        async with ACTIVE_REQUESTS_LOCK:
            if cache_key in ACTIVE_REQUESTS:
                event = ACTIVE_REQUESTS.pop(cache_key)
                event.set()
        return RAW_BASE_MEM_CACHE[cache_key]

    # Check file cache
    cached = await get_from_cache(cache_key)
    if cached:
        # Handle different cache formats
        if isinstance(cached, dict) and "data" in cached:
            data = cached["data"]
        elif isinstance(cached, list):
            data = cached
        else:
            logger.warning(f"Unexpected cache format: {type(cached)}, fetching fresh data")
            data = None

        if data is not None:
            logger.info(f"File cache HIT for year={year}, months={start_month}-{end_month}")
            if RAW_BASE_USE_MEMORY:
                RAW_BASE_MEM_CACHE[cache_key] = data
                RAW_BASE_MEM_KEYS.append(cache_key)
                if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
                    old = RAW_BASE_MEM_KEYS.pop(0)
                    RAW_BASE_MEM_CACHE.pop(old, None)
            # Clean up request tracking
            async with ACTIVE_REQUESTS_LOCK:
                if cache_key in ACTIVE_REQUESTS:
                    event = ACTIVE_REQUESTS.pop(cache_key)
                    event.set()
            return data

    # Cache MISS - fetch from database (entire range at once)
    logger.info(f"Cache MISS - Fetching from database for year={year}, months={start_month}-{end_month}")

    try:
        data = await read_raw_data_from_database(
            year=year, start_month=start_month, end_month=end_month,
            zones=zones, limit=limit, viewport=viewport
        )

        # Cache the result
        await set_to_cache(cache_key, {"data": data}, ttl=CACHE_TTL)

        if RAW_BASE_USE_MEMORY:
            RAW_BASE_MEM_CACHE[cache_key] = data
            RAW_BASE_MEM_KEYS.append(cache_key)
            if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
                old = RAW_BASE_MEM_KEYS.pop(0)
                RAW_BASE_MEM_CACHE.pop(old, None)

        return data
    finally:
        # Signal waiting requests that data is ready
        async with ACTIVE_REQUESTS_LOCK:
            if cache_key in ACTIVE_REQUESTS:
                event = ACTIVE_REQUESTS.pop(cache_key)
                event.set()  # Wake up waiting requests

# ----------------- Keep all existing model loading and processing functions -----------------

# Model loading functions (same as before)
loaded_models = {}
model_access_times = {}
MAX_LOADED_MODELS = 5

def check_memory_usage():
    return psutil.virtual_memory().percent

def cleanup_old_models():
    global loaded_models, model_access_times
    memory_usage = check_memory_usage()
    if memory_usage > 80.0 or len(loaded_models) > MAX_LOADED_MODELS:
        if model_access_times:
            oldest_models = sorted(model_access_times.items(), key=lambda x: x[1])
            models_to_remove = oldest_models[:max(1, len(oldest_models)//2)]
            for model_name, _ in models_to_remove:
                if model_name in loaded_models:
                    del loaded_models[model_name]
                    del model_access_times[model_name]
            gc.collect()

def get_model_metadata(model_name: str):
    metadata_path = os.path.join(MODELS_DIR, f"{model_name}.pkl")
    if os.path.exists(metadata_path):
        return joblib.load(metadata_path)
    return None

def load_model(model_name: str):
    global model_access_times
    if model_name in loaded_models:
        model_access_times[model_name] = time.time()
        return loaded_models[model_name]

    cleanup_old_models()
    model_path = os.path.join(MODELS_DIR, f"{model_name}.json")
    if not os.path.exists(model_path):
        raise RuntimeError(f"Model JSON not found: {model_path}")

    booster = xgb.Booster()
    booster.load_model(model_path)

    metadata = get_model_metadata(model_name)
    if metadata is None:
        raise RuntimeError(f"Model metadata (.pkl) not found for {model_name}")

    required_features = metadata.get('required_features', [])
    if not required_features:
        # Get month range for this model and convert to month names
        start_month, end_month = get_model_month_range(model_name)
        months = get_month_names_from_range(start_month, end_month)
        required_features = [
            f"{feat}_{m}" for feat in ['NDVI', 'GLI', 'NDWI', 'Precipitation'] for m in months
        ]

    loaded_models[model_name] = {
        'booster': booster,
        'metadata': metadata,
        'required_features': required_features
    }
    model_access_times[model_name] = time.time()

    try:
        logger.info(f"Loaded {model_name} with {len(required_features)} features: {required_features}")
    except Exception:
        pass

    return loaded_models[model_name]

# Keep all other utility functions (score normalization, zone adjustments, etc.)
def apply_zone_adjustments(predictions_df: pd.DataFrame) -> pd.DataFrame:
    try:
        adjusted_df = predictions_df.copy()
        for zone, adjustment in ZONE_PREDICTION_ADJUSTMENTS.items():
            mask = adjusted_df["zone"] == zone
            if mask.any():
                adjusted_df.loc[mask, "prediction"] += adjustment
        return adjusted_df
    except Exception:
        return predictions_df

def _normalize_scores_to_1d(scores: np.ndarray) -> np.ndarray:
    """
    บังคับผลลัพธ์จาก xgboost.predict ให้เป็นเวกเตอร์ 1 มิติ
    """
    scores = np.asarray(scores)
    if scores.ndim == 2:
        scores = scores[:, 1] if scores.shape[1] >= 3 else scores[:, 0]
    elif scores.ndim == 0:
        scores = scores.reshape(1,)
    return scores.astype(np.float32, copy=False)

# Keep feature preparation and prediction functions (adapted for database data)
def _empty_feature_row(required_features: List[str]) -> Dict[str, Any]:
    row = {"PlantID": None, "Lat": None, "Lon": None, "zone": "Unknown", "cane_type": "Unknown"}
    for f in required_features:
        row[f] = 0.0
    return row

def _update_feature_row(row: Dict[str, Any], rec: Dict[str, Any], required_features: List[str]):
    if row["PlantID"] is None:
        pid = rec.get("plant_id")
        if not pid and rec.get("lat") is not None and rec.get("lng") is not None:
            pid = f"{float(rec['lat']):.6f}_{float(rec['lng']):.6f}"
        row["PlantID"] = str(pid) if pid else None
    if row["Lat"] is None and rec.get("lat") is not None:
        row["Lat"] = float(rec["lat"])
    if row["Lon"] is None and rec.get("lng") is not None:
        row["Lon"] = float(rec["lng"])
    if rec.get("zone"):
        row["zone"] = str(rec["zone"])
    if rec.get("cane_type"):
        row["cane_type"] = str(rec["cane_type"])

    m = int(rec.get("month") or 0)
    if 1 <= m <= 12:
        months = ['January','February','March','April','May','June','July','August','September','October','November','December']
        mname = months[m-1]
        mapping = {"NDVI": "ndvi", "NDWI": "ndwi", "GLI": "gli", "Precipitation": "precipitation"}
        for F, col in mapping.items():
            fname = f"{F}_{mname}"
            if fname in required_features:
                val = rec.get(col)
                if val is not None and val == val:  # not NaN
                    row[fname] = float(val)

def _accumulator_to_batches(acc: Dict[str, Dict[str, Any]], required_features: List[str], batch_size: int):
    buffer = []
    for plant_row in acc.values():
        for f in required_features:
            if f not in plant_row:
                plant_row[f] = 0.0
        buffer.append(plant_row)
        if len(buffer) >= batch_size:
            df = pd.DataFrame(buffer)
            for col in ["PlantID","Lat","Lon","zone","cane_type"]:
                if col not in df.columns:
                    df[col] = "" if col in ["PlantID","zone","cane_type"] else 0.0
            yield df
            buffer = []
    if buffer:
        df = pd.DataFrame(buffer)
        for col in ["PlantID","Lat","Lon","zone","cane_type"]:
            if col not in df.columns:
                df[col] = "" if col in ["PlantID","zone","cane_type"] else 0.0
        yield df

# Keep all existing processing functions but adapt them for database data
def prepare_data_for_model(raw_data: List[Dict], required_features: List[str]) -> pd.DataFrame:
    if not raw_data:
        return pd.DataFrame()

    df = pd.DataFrame(raw_data)
    if df.empty or not {"lat","lng"}.issubset(df.columns):
        return pd.DataFrame()

    if "plant_id" not in df.columns:
        # Use f_id as primary plant_id, fallback to lat_lng
        if "f_id" in df.columns:
            df["plant_id"] = df["f_id"].fillna(0).astype(int).astype(str)
            # If f_id is 0 or empty, use lat_lng
            df.loc[df["f_id"].isna() | (df["f_id"] == 0), "plant_id"] = df.apply(
                lambda r: f"{float(r['lat']):.6f}_{float(r['lng']):.6f}", axis=1
            )
        else:
            df["plant_id"] = df.apply(lambda r: f"{float(r['lat']):.6f}_{float(r['lng']):.6f}", axis=1)

    df["zone"] = df["zone"].fillna("Unknown").replace("", "Unknown")
    df["cane_type"] = df["cane_type"].fillna("Unknown").replace("", "Unknown")

    processed = []
    months = ['January','February','March','April','May','June','July','August','September','October','November','December']

    for plant_id, g in df.groupby("plant_id"):
        row = {
            "PlantID": str(plant_id),
            "Lat": float(g["lat"].iloc[0]),
            "Lon": float(g["lng"].iloc[0]),
            "zone": str(g["zone"].iloc[0]),
            "cane_type": str(g["cane_type"].iloc[0]),
        }
        for feat in required_features:
            row[feat] = 0.0

        # Group by month and average the values to avoid overwriting
        month_features = {}
        for _, r in g.iterrows():
            mo = int(r.get("month") or 0)
            if 1 <= mo <= 12:
                mname = months[mo-1]
                if mname not in month_features:
                    month_features[mname] = {}

                fmap = {"NDVI":"ndvi","GLI":"gli","NDWI":"ndwi","Precipitation":"precipitation"}
                for F, col in fmap.items():
                    val = float(r.get(col, 0) or 0.0)
                    if F not in month_features[mname]:
                        month_features[mname][F] = []
                    month_features[mname][F].append(val)

        # Average the values for each month
        for mname, features in month_features.items():
            for F, values in features.items():
                fname = f"{F}_{mname}"
                if fname in required_features:
                    row[fname] = sum(values) / len(values) if values else 0.0

        processed.append(row)

    if not processed:
        return pd.DataFrame()

    res = pd.DataFrame(processed)
    for f in required_features:
        if f not in res.columns:
            res[f] = 0.0
    return res

def predict_with_model_batched(model_info: Dict, data: pd.DataFrame, batch_size: int = BATCH_PREDICTION_SIZE) -> pd.DataFrame:
    required = model_info["required_features"]
    booster = model_info["booster"]
    feats = data[required].fillna(0)

    all_scores = []
    for i in range(0, len(feats), batch_size):
        batch = feats.iloc[i:i+batch_size]
        dmat = xgb.DMatrix(batch, feature_names=required)
        s = booster.predict(dmat)
        del dmat
        s1 = _normalize_scores_to_1d(s)
        all_scores.extend(s1)
        # Reduced GC frequency - only every 10 batches instead of 5
        if i % (batch_size*10) == 0 and i > 0:
            gc.collect()

    scores = np.array(all_scores, dtype=np.float32)

    months_in_model = sorted({f.split("_",1)[1] for f in required if "_" in f})
    avg_features = {}
    for F in ["NDVI","NDWI","GLI","Precipitation"]:
        cols = [f"{F}_{m}" for m in months_in_model if f"{F}_{m}" in required]
        avg_features[F] = data[cols].mean(axis=1) if cols else pd.Series([0.0]*len(data))

    out = pd.DataFrame({
        "lat": data["Lat"].astype(float),
        "lon": data["Lon"].astype(float),
        "plant_id": data["PlantID"].astype(str),
        "zone": data["zone"],
        "cane_type": data["cane_type"],
    })

    out["prediction"] = scores

    for F, s in avg_features.items():
        out[F.lower()] = s

    out = apply_zone_adjustments(out)
    return out

def calculate_zone_statistics(predictions: pd.DataFrame, requested_zones: Optional[List[str]] = None) -> List[ZoneStatistics]:
    stats: List[ZoneStatistics] = []
    high_mask = predictions["prediction"] > PREDICTION_THRESHOLDS["HIGH_MIN"]
    med_mask = (predictions["prediction"] >= PREDICTION_THRESHOLDS["MEDIUM_MIN"]) & (predictions["prediction"] <= PREDICTION_THRESHOLDS["MEDIUM_MAX"])
    low_mask = predictions["prediction"] < PREDICTION_THRESHOLDS["LOW_MAX"]

    present = set()
    for z, dfz in predictions.groupby("zone"):
        total = len(dfz)
        idx = dfz.index
        hi = int(high_mask[idx].sum())
        md = int(med_mask[idx].sum())
        lo = int(low_mask[idx].sum())
        stats.append(ZoneStatistics(
            zone=z,
            high_prediction_count=hi,
            high_prediction_percentage=round(hi/total*100,2) if total else 0.0,
            medium_prediction_count=md,
            medium_prediction_percentage=round(md/total*100,2) if total else 0.0,
            low_prediction_count=lo,
            low_prediction_percentage=round(lo/total*100,2) if total else 0.0,
            total_plantations=total,
            average_prediction=round(float(dfz["prediction"].mean()),2) if total else 0.0
        ))
        present.add(z)

    if requested_zones:
        for z in requested_zones:
            if z and z not in present:
                stats.append(ZoneStatistics(
                    zone=z,
                    high_prediction_count=0, high_prediction_percentage=0.0,
                    medium_prediction_count=0, medium_prediction_percentage=0.0,
                    low_prediction_count=0, low_prediction_percentage=0.0,
                    total_plantations=0, average_prediction=0.0
                ))
    stats.sort(key=lambda s: s.zone)
    return stats

def group_predictions_by_level(pred_df: pd.DataFrame, raw_data: List[Dict], display_month: int = 6, end_month: Optional[int] = None) -> List[PredictionGroup]:
    res: List[PredictionGroup] = []

    # Deduplicate predictions by plant_id (keep first occurrence)
    if "plant_id" in pred_df.columns:
        pred_df = pred_df.drop_duplicates(subset=["plant_id"], keep="first")
        logger.info(f"After deduplication: {len(pred_df)} unique plants")

    total = len(pred_df)
    months = ['January','February','March','April','May','June','July','August','September','October','November','December']

    # ใช้เดือนสุดท้ายของ range แทน display_month สำหรับดึงข้อมูล feature
    actual_data_month = end_month if end_month else display_month
    display_name = months[display_month-1] if 1 <= display_month <= 12 else f"Month_{display_month}"

    logger.info(f"Using actual_data_month={actual_data_month} (end_month) for feature values, display_month={display_month} for UI")

    dfr = pd.DataFrame(raw_data)
    if "plant_id" not in dfr.columns:
        if "f_id" in dfr.columns:
            dfr["plant_id"] = dfr["f_id"].astype(str)
        elif {"lat","lng"}.issubset(dfr.columns):
            dfr["plant_id"] = dfr.apply(lambda r: f"{float(r['lat']):.6f}_{float(r['lng']):.6f}", axis=1)

    show_cols = ["ndvi","ndwi","gli","precipitation"]
    # ใช้เดือนสุดท้าย (actual_data_month) แทน display_month
    disp = dfr[dfr["month"] == actual_data_month].groupby("plant_id", as_index=True)[show_cols].mean() if not dfr.empty else pd.DataFrame()

    if not disp.empty:
        logger.info(f"Found {len(disp)} plants with data from month {actual_data_month}")

    # Create lookup dict for additional fields from raw_data (use data from actual_data_month)
    extra_fields_lookup = {}
    if not dfr.empty:
        extra_cols = ["f_id", "gis_id", "gis_idkey", "gis_area", "crop_year", "gis_sta", "giscode",
                      "qoata_id", "farmmer_pre", "farmmer_first_name", "farmmer_last_name",
                      "zone_id", "sub_zone", "fac", "gis_cane_type", "factory",
                      "type_owner_idgis", "type_owner_idAC", "year", "month"]
        available_cols = [c for c in extra_cols if c in dfr.columns]
        if available_cols:
            # กรองเฉพาะข้อมูลจากเดือนสุดท้าย (actual_data_month) ก่อน
            dfr_filtered = dfr[dfr["month"] == actual_data_month]
            for pid, group in dfr_filtered.groupby("plant_id"):
                extra_fields_lookup[pid] = group[available_cols].iloc[0].to_dict()

            # Fallback: ถ้าไม่มีข้อมูลเดือนสุดท้าย ให้ใช้ข้อมูลจากเดือนอื่น
            if len(extra_fields_lookup) == 0:
                logger.warning(f"No data found for actual_data_month={actual_data_month}, using all months")
                for pid, group in dfr.groupby("plant_id"):
                    extra_fields_lookup[pid] = group[available_cols].iloc[0].to_dict()

            logger.info(f"Built extra_fields_lookup for {len(extra_fields_lookup)} plants from month {actual_data_month}")

    high = pred_df[pred_df["prediction"] > PREDICTION_THRESHOLDS["HIGH_MIN"]]
    med = pred_df[(pred_df["prediction"] >= PREDICTION_THRESHOLDS["MEDIUM_MIN"]) & (pred_df["prediction"] <= PREDICTION_THRESHOLDS["MEDIUM_MAX"])]
    low = pred_df[pred_df["prediction"] < PREDICTION_THRESHOLDS["LOW_MAX"]]

    levels = {"HIGH": high, "MEDIUM": med, "LOW": low}
    for lvl, dfL in levels.items():
        if dfL.empty:
            continue

        grouped: List[GroupedPlantPrediction] = []
        for row in dfL.itertuples(index=False):
            pid = getattr(row, "plant_id")
            if pid in disp.index:
                vals = disp.loc[pid]
                ndvi = float(vals.get("ndvi", 0.0) or 0.0)
                ndwi = float(vals.get("ndwi", 0.0) or 0.0)
                gli  = float(vals.get("gli", 0.0) or 0.0)
                prcp = float(vals.get("precipitation", 0.0) or 0.0)
            else:
                ndvi = float(getattr(row, "ndvi", 0.0) or 0.0)
                ndwi = float(getattr(row, "ndwi", 0.0) or 0.0)
                gli  = float(getattr(row, "gli", 0.0) or 0.0)
                prcp = float(getattr(row, "precipitation", 0.0) or 0.0)

            # Get extra fields for this plant_id
            extra_data = extra_fields_lookup.get(pid, {})

            grouped.append(GroupedPlantPrediction(
                lat=float(getattr(row, "lat")),
                lon=float(getattr(row, "lon")),
                plant_id=str(pid),
                prediction=float(getattr(row, "prediction")),
                prediction_level=lvl,
                ndvi=ndvi, ndwi=ndwi, gli=gli, precipitation=prcp,
                zone=str(getattr(row, "zone")), cane_type=str(getattr(row, "cane_type")),
                display_month=display_month, display_month_name=display_name,
                year=extra_data.get("year"),
                month=extra_data.get("month"),
                # Add extra fields from correct_data table
                f_id=extra_data.get("f_id"),
                gis_id=extra_data.get("gis_id"),
                gis_idkey=extra_data.get("gis_idkey"),
                gis_area=extra_data.get("gis_area"),
                crop_year=extra_data.get("crop_year"),
                gis_sta=extra_data.get("gis_sta"),
                giscode=extra_data.get("giscode"),
                qoata_id=extra_data.get("qoata_id"),
                farmmer_pre=extra_data.get("farmmer_pre"),
                farmmer_first_name=extra_data.get("farmmer_first_name"),
                farmmer_last_name=extra_data.get("farmmer_last_name"),
                zone_id=extra_data.get("zone_id"),
                sub_zone=extra_data.get("sub_zone"),
                fac=extra_data.get("fac"),
                gis_cane_type=extra_data.get("gis_cane_type"),
                factory=extra_data.get("factory"),
                type_owner_idgis=extra_data.get("type_owner_idgis"),
                type_owner_idAC=extra_data.get("type_owner_idAC")
            ))

        res.append(PredictionGroup(
            level=lvl,
            count=len(dfL),
            percentage=round(len(dfL)/total*100,2) if total else 0.0,
            average_prediction=round(float(dfL["prediction"].mean()),2) if not dfL.empty else 0.0,
            predictions=grouped
        ))

    order = {"HIGH":0,"MEDIUM":1,"LOW":2}
    res.sort(key=lambda x: order.get(x.level, 3))
    return res

async def process_model_predictions(
    model_name: str,
    raw_data: List[Dict],
    display_month: int,
    enable_chunked: bool = True,
    zones_str: Optional[str] = None,
    include_zone_stats: bool = True,
    end_month: Optional[int] = None,
    year: int = 2025,
    start_month: int = 2,
) -> GroupedModelPredictionResult:
    start = time.time()
    try:
        if not raw_data:
            requested_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()]
            empty_stats = [] if not include_zone_stats else [
                ZoneStatistics(zone=z, high_prediction_count=0, high_prediction_percentage=0.0,
                               medium_prediction_count=0, medium_prediction_percentage=0.0,
                               low_prediction_count=0, low_prediction_percentage=0.0,
                               total_plantations=0, average_prediction=0.0)
                for z in requested_zones
            ]
            return GroupedModelPredictionResult(
                model_name=model_name, prediction_groups=[], zone_statistics=empty_stats,
                overall_average=0.0, total_predictions=0
            )

        # Step 1: Extract plant IDs from raw_data
        plant_ids = set(r.get("plant_id") for r in raw_data if r.get("plant_id"))

        # Step 2: Check prediction cache (if enabled)
        cached_predictions = []
        uncached_plant_ids = set()

        if PREDICTION_CACHE_ENABLED and plant_ids:
            global PREDICTION_CACHE_STATS
            PREDICTION_CACHE_STATS["total_requests"] += 1

            for plant_id in plant_ids:
                cache_key = get_prediction_cache_key(year, model_name, plant_id, start_month, end_month or start_month)
                if cache_key in PREDICTION_RESULT_CACHE:
                    cached_predictions.append(PREDICTION_RESULT_CACHE[cache_key])
                    PREDICTION_CACHE_STATS["hits"] += 1
                else:
                    uncached_plant_ids.add(plant_id)
                    PREDICTION_CACHE_STATS["misses"] += 1

            cache_hit_rate = (len(cached_predictions) / len(plant_ids) * 100) if plant_ids else 0
            logger.info(f"Model {model_name}: {len(cached_predictions)} cached, {len(uncached_plant_ids)} need prediction (hit rate: {cache_hit_rate:.1f}%)")
        else:
            # Cache disabled, predict all
            uncached_plant_ids = plant_ids
            logger.info(f"Model {model_name}: Prediction cache disabled, predicting {len(uncached_plant_ids)} plants")

        # Step 3: If all cached, return immediately
        if PREDICTION_CACHE_ENABLED and len(uncached_plant_ids) == 0 and len(cached_predictions) > 0:
            preds_df = pd.DataFrame(cached_predictions)
            groups = group_predictions_by_level(preds_df, raw_data, display_month, end_month=end_month)
            req_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()] if zones_str else None

            if include_zone_stats:
                zone_stats = calculate_zone_statistics(preds_df, requested_zones=req_zones)
                total_preds_count = int(sum(z.total_plantations for z in zone_stats)) if zone_stats else len(preds_df)
            else:
                zone_stats = []
                total_preds_count = len(preds_df)

            overall_avg = round(float(preds_df["prediction"].mean()), 2)
            logger.info(f"Model {model_name}: {len(preds_df)} rows (100% cached) in {time.time()-start:.2f}s")

            return GroupedModelPredictionResult(
                model_name=model_name,
                prediction_groups=groups,
                zone_statistics=zone_stats,
                overall_average=overall_avg,
                total_predictions=total_preds_count
            )

        # Step 4: Filter raw_data to only uncached plants
        raw_data_filtered = [r for r in raw_data if r.get("plant_id") in uncached_plant_ids] if PREDICTION_CACHE_ENABLED else raw_data

        # Step 5: Predict only uncached plants
        model = load_model(model_name)
        prepare_start = time.time()
        prepared = prepare_data_for_model(raw_data_filtered, model["required_features"])
        logger.info(f"Model {model_name}: Data preparation took {time.time()-prepare_start:.2f}s for {len(raw_data_filtered)} records")

        if prepared.empty:
            # If no new predictions but have cached, use cached
            if cached_predictions:
                preds_df = pd.DataFrame(cached_predictions)
            else:
                requested_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()]
                empty_stats = [] if not include_zone_stats else [
                    ZoneStatistics(zone=z, high_prediction_count=0, high_prediction_percentage=0.0,
                                   medium_prediction_count=0, medium_prediction_percentage=0.0,
                                   low_prediction_count=0, low_prediction_percentage=0.0,
                                   total_plantations=0, average_prediction=0.0)
                    for z in requested_zones
                ]
                return GroupedModelPredictionResult(
                    model_name=model_name, prediction_groups=[], zone_statistics=empty_stats,
                    overall_average=0.0, total_predictions=0
                )
        else:
            predict_start = time.time()
            new_preds = predict_with_model_batched(model, prepared)
            logger.info(f"Model {model_name}: Prediction took {time.time()-predict_start:.2f}s for {len(prepared)} records")

            if new_preds.empty:
                # If no new predictions but have cached, use cached
                if cached_predictions:
                    preds_df = pd.DataFrame(cached_predictions)
                else:
                    requested_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()]
                    empty_stats = [] if not include_zone_stats else [
                        ZoneStatistics(zone=z, high_prediction_count=0, high_prediction_percentage=0.0,
                                       medium_prediction_count=0, medium_prediction_percentage=0.0,
                                       low_prediction_count=0, low_prediction_percentage=0.0,
                                       total_plantations=0, average_prediction=0.0)
                        for z in requested_zones
                    ]
                    return GroupedModelPredictionResult(
                        model_name=model_name, prediction_groups=[], zone_statistics=empty_stats,
                        overall_average=0.0, total_predictions=0
                    )
            else:
                # Step 6: Store new predictions in cache (if enabled)
                if PREDICTION_CACHE_ENABLED:
                    for _, row in new_preds.iterrows():
                        plant_id = row.get("plant_id")
                        if plant_id:
                            cache_key = get_prediction_cache_key(year, model_name, plant_id, start_month, end_month or start_month)
                            pred_dict = row.to_dict()
                            PREDICTION_RESULT_CACHE[cache_key] = pred_dict
                            PREDICTION_CACHE_KEYS.append(cache_key)

                            # LRU eviction
                            if len(PREDICTION_RESULT_CACHE) > PREDICTION_CACHE_MAX_ITEMS:
                                # deque automatically removes oldest when maxlen is exceeded
                                # but we need to clean up the dict too
                                keys_to_keep = set(PREDICTION_CACHE_KEYS)
                                keys_to_remove = set(PREDICTION_RESULT_CACHE.keys()) - keys_to_keep
                                for k in keys_to_remove:
                                    PREDICTION_RESULT_CACHE.pop(k, None)

                # Step 7: Combine cached + new predictions
                if cached_predictions:
                    all_predictions = cached_predictions + new_preds.to_dict('records')
                    preds_df = pd.DataFrame(all_predictions)
                else:
                    preds_df = new_preds

                del prepared, new_preds

        # Step 8: Continue normal flow
        groups = group_predictions_by_level(preds_df, raw_data, display_month, end_month=end_month)
        req_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()] if zones_str else None

        if include_zone_stats:
            zone_stats = calculate_zone_statistics(preds_df, requested_zones=req_zones)
            total_preds_count = int(sum(z.total_plantations for z in zone_stats)) if zone_stats else len(preds_df)
        else:
            zone_stats = []
            total_preds_count = len(preds_df)

        overall_avg = round(float(preds_df["prediction"].mean()), 2)

        cache_info = f"({len(cached_predictions)} cached + {len(uncached_plant_ids)} new)" if PREDICTION_CACHE_ENABLED and cached_predictions else ""
        logger.info(f"Model {model_name}: {len(preds_df)} rows {cache_info} in {time.time()-start:.2f}s")

        del preds_df
        gc.collect()

        return GroupedModelPredictionResult(
            model_name=model_name,
            prediction_groups=groups,
            zone_statistics=zone_stats,
            overall_average=overall_avg,
            total_predictions=total_preds_count
        )
    except Exception as e:
        logger.error(f"process_model_predictions error: {e}")
        requested_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()]
        empty_stats = [] if not include_zone_stats else [
            ZoneStatistics(zone=z, high_prediction_count=0, high_prediction_percentage=0.0,
                           medium_prediction_count=0, medium_prediction_percentage=0.0,
                           low_prediction_count=0, low_prediction_percentage=0.0,
                           total_plantations=0, average_prediction=0.0)
            for z in requested_zones
        ]
        return GroupedModelPredictionResult(
            model_name=model_name, prediction_groups=[], zone_statistics=empty_stats,
            overall_average=0.0, total_predictions=0
        )

def calculate_model_averages(grouped_results: List[GroupedModelPredictionResult]) -> AverageCalculationResponse:
    try:
        if not grouped_results:
            return AverageCalculationResponse(
                success=True,
                message="No grouped results to aggregate",
                individual_model_results=[],
                combined_average_result={
                    "models_included": [],
                    "total_models": 0,
                    "overall_average_of_models": 0.0,
                    "combined_prediction_average": 0.0,
                    "total_combined_predictions": 0,
                    "combined_level_distribution": {
                        "high_count": 0, "high_percentage": 0.0,
                        "medium_count": 0, "medium_percentage": 0.0,
                        "low_count": 0, "low_percentage": 0.0,
                    },
                    "combined_zone_averages": {},
                    "model_comparison": {
                        "highest_average": None,
                        "lowest_average": None,
                        "average_range": 0.0,
                        "standard_deviation": 0.0
                    }
                }
            )

        individual: List[ModelAverageResult] = []

        combined_total_preds = 0
        combined_sum_preds = 0.0
        combined_zone_sum: Dict[str, float] = defaultdict(float)
        combined_zone_cnt: Dict[str, int] = defaultdict(int)
        combined_high = combined_med = combined_low = 0

        per_model_sums = []

        for mr in grouped_results:
            if mr.prediction_groups:
                hi = md = lo = 0.0
                for g in mr.prediction_groups:
                    if g.level == "HIGH": hi = g.percentage
                    elif g.level == "MEDIUM": md = g.percentage
                    elif g.level == "LOW": lo = g.percentage
                total_preds = mr.total_predictions
                high_count_m = int(round(hi * total_preds / 100.0))
                med_count_m  = int(round(md * total_preds / 100.0))
                low_count_m  = max(0, total_preds - high_count_m - med_count_m)
            else:
                high_count_m = sum(z.high_prediction_count for z in mr.zone_statistics)
                med_count_m  = sum(z.medium_prediction_count for z in mr.zone_statistics)
                low_count_m  = sum(z.low_prediction_count for z in mr.zone_statistics)
                total_preds  = high_count_m + med_count_m + low_count_m

            zavg: Dict[str, float] = {}
            for zs in mr.zone_statistics:
                zavg[zs.zone] = zs.average_prediction
                combined_zone_sum[zs.zone] += (zs.average_prediction * zs.total_plantations)
                combined_zone_cnt[zs.zone] += zs.total_plantations

            individual.append(ModelAverageResult(
                model_name=mr.model_name,
                overall_average=mr.overall_average,
                total_predictions=total_preds,
                high_percentage=(high_count_m/total_preds*100 if total_preds else 0.0),
                medium_percentage=(med_count_m/total_preds*100 if total_preds else 0.0),
                low_percentage=(low_count_m/total_preds*100 if total_preds else 0.0),
                zone_averages=zavg,
                zone_statistics=mr.zone_statistics  # ส่ง zone statistics ด้วย
            ))

            combined_total_preds += total_preds
            combined_sum_preds += (mr.overall_average * total_preds)
            combined_high += high_count_m
            combined_med  += med_count_m
            combined_low  += low_count_m
            per_model_sums.append(mr.overall_average)

        combined_prediction_average = (combined_sum_preds / combined_total_preds) if combined_total_preds else 0.0

        combined_zone_averages = {
            z: (combined_zone_sum[z] / combined_zone_cnt[z]) if combined_zone_cnt[z] else 0.0
            for z in sorted(combined_zone_cnt.keys())
        }

        overall_avg_of_models = (sum(per_model_sums)/len(per_model_sums)) if per_model_sums else 0.0

        highest = max(individual, key=lambda r: r.overall_average) if individual else None
        lowest  = min(individual, key=lambda r: r.overall_average) if individual else None
        std_dev = float(np.std(per_model_sums)) if per_model_sums else 0.0

        combined = {
            "models_included": [r.model_name for r in individual],
            "total_models": len(individual),
            "overall_average_of_models": round(overall_avg_of_models, 2),
            "combined_prediction_average": round(combined_prediction_average, 2),
            "total_combined_predictions": int(combined_total_preds),
            "combined_level_distribution": {
                "high_count": int(combined_high),
                "high_percentage": round((combined_high/combined_total_preds*100), 2) if combined_total_preds else 0.0,
                "medium_count": int(combined_med),
                "medium_percentage": round((combined_med/combined_total_preds*100), 2) if combined_total_preds else 0.0,
                "low_count": int(combined_low),
                "low_percentage": round((combined_low/combined_total_preds*100), 2) if combined_total_preds else 0.0,
            },
            "combined_zone_averages": {z: round(v, 2) for z, v in combined_zone_averages.items()},
            "model_comparison": {
                "highest_average": ({"model_name": highest.model_name, "overall_average": round(highest.overall_average,2)} if highest else None),
                "lowest_average": ({"model_name": lowest.model_name, "overall_average": round(lowest.overall_average,2)} if lowest else None),
                "average_range": round((highest.overall_average - lowest.overall_average),2) if (highest and lowest) else 0.0,
                "standard_deviation": round(std_dev,2)
            }
        }

        return AverageCalculationResponse(
            success=True,
            message=f"Calculated averages for {len(individual)} models (streaming={not any(gr.prediction_groups for gr in grouped_results)})",
            individual_model_results=individual,
            combined_average_result=combined
        )
    except Exception as e:
        logger.error(f"calculate_model_averages error: {e}")
        raise

# Main prediction endpoint core function
async def _predict_grouped_core(request: GroupedPredictionRequest, raw_data: Optional[List[Dict]] = None) -> GroupedPredictionResponse:
    start_t = time.time()
    zones_used = zones_for_year(request.year, request.zones)

    # Prepare viewport
    viewport = None
    if all(v is not None for v in [request.min_lat, request.min_lng, request.max_lat, request.max_lng]):
        viewport = {
            "min_lat": float(request.min_lat), "max_lat": float(request.max_lat),
            "min_lng": float(request.min_lng), "max_lng": float(request.max_lng)
        }

    # Use request.limit for viewport queries (already set by frontend based on zoom level)
    # Fallback to MAP_DISPLAY_LIMIT only if not provided
    if viewport:
        fetch_limit = int(request.limit) if request.limit else int(MAP_DISPLAY_LIMIT)
    else:
        fetch_limit = int(request.limit or 1000000)

    # Don't cache when viewport is used (dynamic queries)
    cache_key = None
    if not viewport and raw_data is None:
        cache_key = generate_cache_key(
            "predict_grouped_db_v1",
            year=request.year, start_month=request.start_month, end_month=request.end_month,
            models=request.models, zones=zones_used, limit=fetch_limit,
            group_by_level=request.group_by_level, display_month=request.display_month,
            db_url_hash=hashlib.md5(DATABASE_URL.encode()).hexdigest()[:8]
        )

        cached = await get_from_cache(cache_key)
        if cached and "data" in cached:
            res = cached["data"]
            res["cached"] = True
            res["cache_key"] = cache_key
            return res

    if not (1 <= request.start_month <= 12 and 1 <= request.end_month <= 12 and request.start_month <= request.end_month):
        raise HTTPException(status_code=400, detail="invalid start_month/end_month")
    if not (1 <= request.display_month <= 12):
        raise HTTPException(status_code=400, detail="display_month must be 1..12")

    if raw_data is None:
        # Use month-based cache for better performance
        raw_data = await get_base_raw_cached_db(
            year=request.year,
            start_month=request.start_month,
            end_month=request.end_month,
            zones=zones_used,
            limit=fetch_limit,
            viewport=viewport
        )
        if not raw_data:
            raise HTTPException(status_code=404, detail="No data found from database with given filters")

    sem = asyncio.Semaphore(max(1, int(request.max_concurrent_models or 2)))

    async def _run(m):
        async with sem:
            return await process_model_predictions(
                m, raw_data, display_month=request.display_month,
                enable_chunked=request.enable_chunked_processing, zones_str=zones_used,
                include_zone_stats=False,
                end_month=request.end_month,  # ส่ง end_month เพื่อใช้ข้อมูลเดือนสุดท้าย
                year=request.year,
                start_month=request.start_month
            )

    tasks = [asyncio.create_task(_run(m)) for m in request.models]
    model_results = await asyncio.gather(*tasks)
    results = [r for r in model_results if r is not None]

    if not results:
        raise HTTPException(status_code=500, detail="Failed to process all models")

    response = GroupedPredictionResponse(
        success=True,
        message=f"Processed {len(results)} models from database (viewport={'yes' if viewport else 'no'}, cache_strategy={'range-based' if not viewport else 'direct'})",
        results=results,
        cached=False,
        cache_key=cache_key,
        processing_stats={
            "total_time_sec": round(time.time()-start_t, 3),
            "zones_used": zones_used,
            "database_url": DATABASE_URL.replace(db_url.password or "", "***") if db_url.password else DATABASE_URL,
            "total_rows_fetched": len(raw_data),
            "viewport_used": viewport is not None,
            "fetch_limit": fetch_limit,
            "cache_strategy": "range-based" if not viewport else "direct",
            "month_range": f"{request.start_month}-{request.end_month}",
            "memory_cache_size": len(RAW_BASE_MEM_CACHE),
            "memory_cache_keys": len(RAW_BASE_MEM_KEYS),
            "prediction_cache_enabled": PREDICTION_CACHE_ENABLED
        }
    )

    # Only cache non-viewport queries
    if cache_key and not viewport:
        await set_to_cache(cache_key, response.dict(), ttl=min(CACHE_TTL, 3600))

    return response

# Startup and shutdown events
@app.on_event("startup")
async def startup_event():
    global ACTIVE_REQUESTS_LOCK
    ACTIVE_REQUESTS_LOCK = asyncio.Lock()
    await init_db_pool()

@app.on_event("shutdown")
async def shutdown_event():
    await close_db_pool()

# ----------------- Endpoints -----------------

# ใหม่: Endpoint สำหรับ Individual models performance (ดึงข้อมูลตาม features ของแต่ละ model)
@app.get("/predict/individual-models-performance", response_model=AverageCalculationResponse)
async def get_individual_models_performance(
    year: int = 2025,
    start_month: int = 2,
    end_month: int = 8,
    models: str = "m12,m1,m2,m3",
    zones: str = ALL_ZONES,
    display_month: int = 8
):
    """
    ดึงข้อมูลสำหรับ Individual models performance
    โดยดึงข้อมูลตาม features ของแต่ละ model และใช้ cache สำหรับเดือน/ปีเดียวกัน

    Flow:
    1. ดึงข้อมูลสำหรับแต่ละ model โดยใช้เฉพาะ features ที่ model ต้องการ
    2. ถ้าเป็นเดือน/ปีเดียวกัน จะใช้ cache จากรอบแรกที่ดึงมา
    3. คำนวณ statistics สำหรับแต่ละ model
    """
    start_t = time.time()
    logger.info(f"[Individual Models Performance] Starting for year={year}, months={start_month}-{end_month}")

    models_list = [m.strip() for m in models.split(",") if m.strip()]

    # สร้าง cache key สำหรับ Individual models performance
    cache_key = generate_cache_key(
        "individual_models_perf_v1",
        year=year,
        start_month=start_month,
        end_month=end_month,
        models=sorted(models_list),
        zones=zones,
        display_month=display_month,
        db_hash=hashlib.md5(DATABASE_URL.encode()).hexdigest()[:8]
    )

    # ตรวจสอบ cache ก่อน
    cached = await get_from_cache(cache_key)
    if cached and "data" in cached:
        logger.info(f"[Individual Models Performance] Cache HIT: {cache_key}")
        res = cached["data"]
        res["cached"] = True
        res["cache_key"] = cache_key
        return res

    logger.info(f"[Individual Models Performance] Cache MISS: {cache_key}")

    # ประมวลผลแต่ละ model โดยดึงข้อมูลตาม features ที่ต้องการ
    all_grouped: List[GroupedModelPredictionResult] = []

    for model_name in models_list:
        try:
            logger.info(f"[Individual Models Performance] Processing model: {model_name}")

            # ดึงข้อมูลสำหรับ model นี้โดยใช้ month-based cache
            # (ถ้าเดือนซ้ำกัน จะใช้ cache จากรอบแรก)
            res = await process_model_predictions_streaming_db(
                model_name=model_name,
                year=year,
                start_month=start_month,
                end_month=end_month,
                zones_str=zones
            )
            all_grouped.append(res)
            logger.info(f"[Individual Models Performance] Model {model_name} completed: {res.total_predictions} predictions")

        except Exception as e:
            logger.error(f"[Individual Models Performance] Error processing model {model_name}: {e}")
            # ถ้า model หนึ่งล้มเหลว ให้ส่งผลลัพธ์เป็น empty
            all_grouped.append(GroupedModelPredictionResult(
                model_name=model_name,
                prediction_groups=[],
                zone_statistics=[],
                overall_average=0.0,
                total_predictions=0
            ))

    if not all_grouped:
        raise HTTPException(status_code=500, detail="No results from any model")

    # คำนวณ averages สำหรับทุก model
    avg_resp = calculate_model_averages(all_grouped)
    avg_resp.processing_stats = {
        "total_time_sec": round(time.time() - start_t, 3),
        "year": year,
        "start_month": start_month,
        "end_month": end_month,
        "zones": zones,
        "models_processed": len(models_list),
        "database_url": DATABASE_URL.replace(db_url.password or "", "***") if db_url.password else DATABASE_URL,
        "streaming": True,
        "cache_strategy": "month-based per model features"
    }
    avg_resp.cache_key = cache_key

    # เก็บผลลัพธ์ลง cache (TTL 1 ชั่วโมง)
    await set_to_cache(cache_key, avg_resp.dict(), ttl=3600)
    logger.info(f"[Individual Models Performance] Completed in {avg_resp.processing_stats['total_time_sec']}s")

    return avg_resp

@app.get("/predict/grouped/averages", response_model=AverageCalculationResponse)
async def get_model_averages(
    start_month: int = 2,
    end_month: int = 8,
    models: str = "m12,m1,m2,m3",
    zones: str = ALL_ZONES,
    display_month: int = 6,  # kept for compatibility, not used in aggregation
    limit: int = 0           # ignored in streaming mode
):
    start_t = time.time()
    models_list = [m.strip() for m in models.split(",") if m.strip()]
    models_by_year: Dict[int, List[str]] = {}
    for m in models_list:
        # Normalize model name to lowercase for lookup
        model_key = m.lower()
        y = MODEL_YEAR_CONFIG.get(model_key, 2025)  # Changed default from 2024 to 2025
        logger.info(f"Model '{m}' (key: '{model_key}') → year {y}")
        models_by_year.setdefault(y, []).append(m)

    zones_by_year = {y: zones_for_year(y, zones) for y in models_by_year.keys()}

    cache_key = generate_cache_key(
        "model_averages_stream_db_v1",
        start_month=start_month, end_month=end_month,
        models=models_list, zones_by_year=zones_by_year,
        display_month=display_month,
        model_year_config=MODEL_YEAR_CONFIG,
        db_url_hash=hashlib.md5(DATABASE_URL.encode()).hexdigest()[:8]
    )
    cached = await get_from_cache(cache_key)
    if cached and "data" in cached:
        res = cached["data"]
        res["cached"] = True
        res["cache_key"] = cache_key
        return res

    all_grouped: List[GroupedModelPredictionResult] = []
    for y, mlist in models_by_year.items():
        for m in mlist:
            res = await process_model_predictions_streaming_db(
                model_name=m, year=y,
                start_month=start_month, end_month=end_month,
                zones_str=zones_by_year[y]
            )
            all_grouped.append(res)

    if not all_grouped:
        raise HTTPException(status_code=500, detail="No results")

    avg_resp = calculate_model_averages(all_grouped)
    avg_resp.processing_stats = {
        "total_time_sec": round(time.time()-start_t, 3),
        "years_processed": list(models_by_year.keys()),
        "zones_by_year": zones_by_year,
        "database_url": DATABASE_URL.replace(db_url.password or "", "***") if db_url.password else DATABASE_URL,
        "streaming": True
    }
    avg_resp.cache_key = cache_key
    await set_to_cache(cache_key, avg_resp.dict(), ttl=min(CACHE_TTL, 3600))
    return avg_resp

@app.post("/predict/grouped", response_model=GroupedPredictionResponse)
async def predict_grouped(request: GroupedPredictionRequest):
    if processing_queue.full():
        raise HTTPException(status_code=503, detail="Server is busy. Try again shortly.")
    processing_queue.put(1)
    try:
        return await _predict_grouped_core(request, raw_data=None)
    finally:
        try:
            processing_queue.get_nowait()
        except:
            pass
        gc.collect()

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    global db_pool
    db_status = "connected" if db_pool and not db_pool._closed else "disconnected"

    return {
        "status": "healthy",
        "database": db_status,
        "version": "3.9.2",
        "cache_strategy": "range-based",
        "feature_mapping": "last-month-fixed",
        "viewport_consistency": "enabled",
        "feature_averaging": "enabled",
        "prediction_cache": "enabled" if PREDICTION_CACHE_ENABLED else "disabled",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/config")
async def get_config():
    """Get frontend configuration from environment variables"""
    return {
        "success": True,
        "config": {
            # Data limits
            "map_display_limit": int(MAP_DISPLAY_LIMIT),
            "max_records_per_month": int(os.getenv("MAX_RECORDS_PER_MONTH", "500000")),
            "batch_prediction_size": BATCH_PREDICTION_SIZE,

            # Thresholds
            "prediction_thresholds": PREDICTION_THRESHOLDS,

            # Zone adjustments
            "zone_adjustments": ZONE_PREDICTION_ADJUSTMENTS,

            # Model configuration
            "model_year_config": MODEL_YEAR_CONFIG,
            "model_month_range": MODEL_MONTH_RANGE,

            # Cache settings
            "cache_ttl": CACHE_TTL,
            "cache_prefix": CACHE_PREFIX,

            # Database settings
            "db_timeout": DB_TIMEOUT,
            "db_fetch_size": DB_FETCH_SIZE,

            # Zones
            "all_zones": ALL_ZONES,
            "zones_by_year": YEAR_ZONES,

            # Viewport settings
            "viewport": {
                "default_center": [14.5, 101.0],
                "default_zoom": 17,
                "max_zoom": 19
            }
        }
    }

@app.get("/database/info")
async def database_info():
    """Get database connection info"""
    global db_pool
    
    if not db_pool:
        return {
            "status": "not_connected",
            "pool_size": 0,
            "free_connections": 0
        }
    
    return {
        "status": "connected",
        "pool_size": db_pool.get_size(),
        "free_connections": db_pool.get_idle_size(),
        "min_size": db_pool.get_min_size(),
        "max_size": db_pool.get_max_size(),
        "database_name": DB_CONFIG["database"],
        "host": DB_CONFIG["host"],
        "port": DB_CONFIG["port"]
    }

@app.get("/database/test-query")
async def test_database_query():
    """Test database connectivity with a simple query"""
    global db_pool
    
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            # Test query to get table info
            result = await conn.fetchrow("""
                SELECT COUNT(*) as total_records,
                       MIN(year) as min_year,
                       MAX(year) as max_year,
                       COUNT(DISTINCT zone) as unique_zones
                FROM correct_data
                LIMIT 1
            """)

            zones_result = await conn.fetch("""
                SELECT zone, COUNT(*) as count
                FROM correct_data
                GROUP BY zone
                ORDER BY count DESC
                LIMIT 10
            """)
            
            return {
                "status": "success",
                "query_time": datetime.now().isoformat(),
                "table_stats": dict(result) if result else {},
                "top_zones": [{"zone": row["zone"], "count": row["count"]} for row in zones_result]
            }
    except Exception as e:
        logger.error(f"Database test query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(e)}")

@app.get("/cache/status")
async def cache_status():
    """Get cache status and statistics"""
    # Get file cache info
    cache_dir = Path(CACHE_DIR)
    file_cache_count = 0
    file_cache_size = 0
    monthly_cache_files = []

    if cache_dir.exists():
        for cache_file in cache_dir.glob("*.pkl"):
            file_cache_count += 1
            file_cache_size += cache_file.stat().st_size

            # Check if it's a monthly cache file
            if "raw_base_db_monthly" in cache_file.name:
                monthly_cache_files.append({
                    "name": cache_file.name,
                    "size_mb": round(cache_file.stat().st_size / (1024 * 1024), 2),
                    "modified": datetime.fromtimestamp(cache_file.stat().st_mtime).isoformat()
                })

    # Sort by modified time
    monthly_cache_files.sort(key=lambda x: x["modified"], reverse=True)

    # Calculate prediction cache stats
    pred_cache_size_mb = 0
    if PREDICTION_RESULT_CACHE:
        # Rough estimate: 500 bytes per prediction
        pred_cache_size_mb = (len(PREDICTION_RESULT_CACHE) * 500) / (1024 * 1024)

    hit_rate = 0.0
    if PREDICTION_CACHE_STATS["total_requests"] > 0:
        total_checks = PREDICTION_CACHE_STATS["hits"] + PREDICTION_CACHE_STATS["misses"]
        if total_checks > 0:
            hit_rate = (PREDICTION_CACHE_STATS["hits"] / total_checks) * 100

    return {
        "memory_cache": {
            "enabled": RAW_BASE_USE_MEMORY,
            "max_size": RAW_BASE_MEM_MAX,
            "current_size": len(RAW_BASE_MEM_CACHE),
            "keys_count": len(RAW_BASE_MEM_KEYS),
            "cached_keys": list(RAW_BASE_MEM_CACHE.keys())[:10]  # Show first 10
        },
        "prediction_cache": {
            "enabled": PREDICTION_CACHE_ENABLED,
            "max_items": PREDICTION_CACHE_MAX_ITEMS,
            "current_items": len(PREDICTION_RESULT_CACHE),
            "estimated_size_mb": round(pred_cache_size_mb, 2),
            "hit_rate_percent": round(hit_rate, 2),
            "stats": PREDICTION_CACHE_STATS.copy()
        },
        "file_cache": {
            "directory": str(cache_dir),
            "total_files": file_cache_count,
            "total_size_mb": round(file_cache_size / (1024 * 1024), 2),
            "monthly_cache_files_count": len(monthly_cache_files),
            "recent_monthly_caches": monthly_cache_files[:20]  # Show 20 most recent
        },
        "cache_config": {
            "ttl_seconds": CACHE_TTL,
            "db_fetch_size": DB_FETCH_SIZE,
            "cache_prefix": CACHE_PREFIX
        }
    }

@app.delete("/cache/clear")
async def clear_cache(clear_memory: bool = True, clear_files: bool = True, clear_predictions: bool = True):
    """Clear cache (memory and/or files and/or predictions)"""
    result = {
        "success": True,
        "memory_cleared": False,
        "files_deleted": 0,
        "predictions_cleared": False
    }

    # Clear memory cache
    if clear_memory:
        global RAW_BASE_MEM_CACHE, RAW_BASE_MEM_KEYS
        memory_count = len(RAW_BASE_MEM_CACHE)
        RAW_BASE_MEM_CACHE.clear()
        RAW_BASE_MEM_KEYS.clear()
        result["memory_cleared"] = True
        result["memory_entries_cleared"] = memory_count

    # Clear prediction cache
    if clear_predictions:
        global PREDICTION_RESULT_CACHE, PREDICTION_CACHE_KEYS, PREDICTION_CACHE_STATS
        pred_count = len(PREDICTION_RESULT_CACHE)
        PREDICTION_RESULT_CACHE.clear()
        PREDICTION_CACHE_KEYS.clear()
        PREDICTION_CACHE_STATS = {"hits": 0, "misses": 0, "total_requests": 0}
        result["predictions_cleared"] = True
        result["prediction_entries_cleared"] = pred_count

    # Clear file cache
    if clear_files:
        deleted = await delete_cache_pattern(f"{CACHE_PREFIX}_*")
        result["files_deleted"] = deleted

    if clear_memory or clear_predictions:
        gc.collect()

    return result

@app.get("/")
async def root():
    """Serve the frontend HTML file"""
    try:
        html_file_path = "index-ml.html"  # Path to your HTML file
        
        # Check if file exists
        if not os.path.exists(html_file_path):
            # Return JSON response if HTML file not found
            return {
                "message": "ML Prediction Service - Database Version",
                "version": "3.5.0",
                "database": "PostgreSQL",
                "cache_strategy": "month-based",
                "note": f"Frontend file '{html_file_path}' not found",
                "endpoints": {
                    "predictions": "/predict/grouped",
                    "averages": "/predict/grouped/averages",
                    "health": "/health",
                    "database_info": "/database/info",
                    "test_query": "/database/test-query",
                    "cache_status": "/cache/status",
                    "cache_clear": "/cache/clear"
                }
            }
        
        # Read and return HTML file
        async with aiofiles.open(html_file_path, 'r', encoding='utf-8') as f:
            html_content = await f.read()
        
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logger.error(f"Error serving HTML file: {e}")
        # Fallback to JSON response
        return {
            "message": "ML Prediction Service - Database Version",
            "version": "3.5.0",
            "database": "PostgreSQL",
            "cache_strategy": "month-based",
            "error": f"Could not serve HTML file: {str(e)}",
            "endpoints": {
                "predictions": "/predict/grouped",
                "averages": "/predict/grouped/averages",
                "health": "/health",
                "database_info": "/database/info",
                "test_query": "/database/test-query",
                "cache_status": "/cache/status",
                "cache_clear": "/cache/clear"
            }
        }

# ----------------- Run -----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=1290, reload=False, workers=1, access_log=False)