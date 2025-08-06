# main-ml.py  (v3.3.2 — Plan B: streaming aggregation + score normalization + model-specific months)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
from collections import defaultdict
import glob

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ควบคุม concurrency เบื้องต้น
MAX_WORKERS = min(4, os.cpu_count() or 4)
processing_queue = Queue(maxsize=10)
processing_lock = threading.Semaphore(MAX_WORKERS)

app = FastAPI(
    title="ML Prediction Service - CSV Only (Viewport-fast + Streaming Averages)",
    version="3.3.2"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# --------- Config (ปรับได้ผ่าน env) ----------
MODELS_DIR = os.getenv("MODELS_DIR", "models-cqr")

# แหล่งข้อมูล CSV:
CSV_PATH = os.getenv("CSV_PATH", "data/data.csv")
CSV_GLOB = os.getenv("CSV_GLOB", "data/data.csv")

CACHE_TTL = int(os.getenv("CACHE_TTL", "31536000"))
CACHE_PREFIX = os.getenv("CACHE_PREFIX", "ml_predict")
CACHE_DIR = os.getenv("CACHE_DIR", "cache")

# chunk ในการอ่าน CSV (pandas.read_csv)
CSV_READ_CHUNKSIZE = int(os.getenv("CSV_READ_CHUNKSIZE", "200000"))  # แนะนำ 200k แถว/ชังก์

# batch สำหรับพยากรณ์ใน XGBoost
BATCH_PREDICTION_SIZE = int(os.getenv("BATCH_PREDICTION_SIZE", "5000"))

# เกณฑ์ระดับ prediction
PREDICTION_THRESHOLDS = {
    "HIGH_MIN": float(os.getenv("HIGH_MIN", "12.0")),
    "MEDIUM_MIN": float(os.getenv("MEDIUM_MIN", "10.0")),
    "MEDIUM_MAX": float(os.getenv("MEDIUM_MAX", "12.0")),
    "LOW_MAX": float(os.getenv("LOW_MAX", "10.0"))
}

# ปรับค่า prediction ตามโซน
ZONE_PREDICTION_ADJUSTMENTS = {
    "MPK": 0.5,
    "MKS": 0.5,
    "MAC": 0.5,
    "MPDC": -0.5,
    "SB": -0.5
}

# แม็พ model -> ปี
MODEL_YEAR_CONFIG = {
    "m12": 2024,
    "m1": 2025,
    "m2": 2025,
    "m3": 2025
}

# 👉 ใหม่: เดือนที่ใช้สร้างฟีเจอร์ของแต่ละโมเดล (override ได้ผ่าน ENV)
MODEL_FEATURE_MONTHS = {
    # only m12 uses April→October
    "m12": [m.strip() for m in os.getenv("M12_FEATURE_MONTHS", "April,May,June,July,August,September,October").split(",") if m.strip()],
    # others default to February→August
    "_default": [m.strip() for m in os.getenv("DEFAULT_FEATURE_MONTHS", "February,March,April,May,June,July,August").split(",") if m.strip()],
}

ALL_ZONES = "MAC,MKB,MKS,MPDC,MPK,MPL,MPV,SB"
YEAR_ZONES: Dict[int, str] = {
    2024: ALL_ZONES,
    2025: ALL_ZONES,
}

def zones_for_year(year: int, fallback: Optional[str] = None) -> str:
    return YEAR_ZONES.get(year) or fallback or ALL_ZONES

# ----------------- Pydantic models -----------------
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

class AverageCalculationResponse(BaseModel):
    success: bool
    message: str
    individual_model_results: List[ModelAverageResult]
    combined_average_result: Dict[str, Any]
    processing_stats: Optional[Dict[str, Any]] = None
    cached: Optional[bool] = False
    cache_key: Optional[str] = None

# ----------------- ยูทิล cache -----------------

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

# ----------------- โหลดโมเดล (ไฟล์ .json + .pkl เมทาดาทา) -----------------
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
        # only m12 uses Apr–Oct; others Feb–Aug
        months = MODEL_FEATURE_MONTHS.get(model_name, MODEL_FEATURE_MONTHS["_default"])
        required_features = [
            f"{feat}_{m}" for feat in ['NDVI', 'GLI', 'NDWI', 'Precipitation'] for m in months
        ]

    loaded_models[model_name] = {
        'booster': booster,
        'metadata': metadata,
        'required_features': required_features
    }
    model_access_times[model_name] = time.time()

    # helpful log
    try:
        logger.info(f"Loaded {model_name} with {len(required_features)} features: {required_features}")
    except Exception:
        pass

    return loaded_models[model_name]

# ----------------- อ่านข้อมูลจาก CSV ทั้งหมด (สำหรับ /predict/grouped) -----------------
REQUIRED_COLUMNS = [
    "id","plant_id","geo","stx","sty","lat","lng","ndvi","gli","ndwi","cigreen","pvr",
    "soil_tempt","tempt","solar_radiation","soil_moisture","precipitation",
    "month","year","zone","cane_type"
]


def _csv_paths() -> List[str]:
    # 1) ถ้ามี CSV_GLOB: ลอง glob ก่อน
    if CSV_GLOB:
        matches = sorted(glob.glob(CSV_GLOB))
        if matches:
            return matches
        if os.path.isfile(CSV_GLOB):
            return [CSV_GLOB]
        logger.warning(f"CSV_GLOB '{CSV_GLOB}' ไม่พบไฟล์จาก glob และไม่ใช่ไฟล์บนดิสก์")
    # 2) ถ้ามี CSV_PATH และไฟล์มีจริง
    if CSV_PATH:
        if os.path.isfile(CSV_PATH):
            return [CSV_PATH]
        else:
            logger.warning(f"CSV_PATH '{CSV_PATH}' ไม่พบไฟล์บนดิสก์")
    # 3) สุดท้าย: error
    raise RuntimeError(
        "ไม่พบไฟล์ CSV — โปรดตั้งค่าอย่างน้อยหนึ่งรายการ: "
        "CSV_GLOB (เช่น 'data/*.csv' หรือ 'data/data.csv') "
        "หรือ CSV_PATH (เช่น 'data/data.csv')"
    )


def _chunk_filter(df: pd.DataFrame,
                  year: int,
                  start_month: int,
                  end_month: int,
                  zones_csv: str) -> pd.DataFrame:
    df = df.rename(columns={c: c.lower() for c in df.columns})
    use_cols = set(REQUIRED_COLUMNS)
    exist_cols = [c for c in df.columns if c in use_cols]
    df = df[exist_cols]

    for col in ["lat","lng","ndvi","gli","ndwi","precipitation"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "month" in df.columns:
        df["month"] = pd.to_numeric(df["month"], errors="coerce").astype("Int64")
    if "year" in df.columns:
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    m = pd.Series(True, index=df.index, dtype=bool)
    if "year" in df.columns:
        cond_year = (df["year"] == year)
        m &= cond_year.fillna(False)
    if "month" in df.columns:
        cond_month = (df["month"] >= start_month) & (df["month"] <= end_month)
        m &= cond_month.fillna(False)
    if "zone" in df.columns and zones_csv:
        allowed = {z.strip() for z in zones_csv.split(",") if z.strip()}
        cond_zone = df["zone"].astype(str).isin(allowed)
        m &= cond_zone

    df = df.loc[m]

    if "plant_id" not in df.columns and {"lat","lng"}.issubset(df.columns):
        df["plant_id"] = df.apply(
            lambda r: f"{float(r['lat']):.6f}_{float(r['lng']):.6f}", axis=1
        )
    if "zone" in df.columns:
        df["zone"] = df["zone"].fillna("Unknown").replace("", "Unknown")
    else:
        df["zone"] = "Unknown"
    if "cane_type" in df.columns:
        df["cane_type"] = df["cane_type"].fillna("Unknown").replace("", "Unknown")
    else:
        df["cane_type"] = "Unknown"

    return df


def read_raw_data_from_csv(year: int,
                           start_month: int,
                           end_month: int,
                           zones: str,
                           limit: int) -> List[Dict[str, Any]]:
    files = _csv_paths()
    out_parts: List[pd.DataFrame] = []
    total = 0

    for path in files:
        logger.info(f"อ่าน CSV: {path}")
        try:
            if CSV_READ_CHUNKSIZE > 0:
                for chunk in pd.read_csv(path, chunksize=CSV_READ_CHUNKSIZE):
                    fchunk = _chunk_filter(chunk, year, start_month, end_month, zones)
                    if not fchunk.empty:
                        out_parts.append(fchunk)
                        total += len(fchunk)
                        if total >= limit:
                            break
                if total >= limit:
                    break
            else:
                df = pd.read_csv(path)
                fchunk = _chunk_filter(df, year, start_month, end_month, zones)
                if not fchunk.empty:
                    out_parts.append(fchunk)
                    total += len(fchunk)
                    if total >= limit:
                        break
        except Exception as e:
            logger.error(f"อ่านไฟล์ {path} ล้มเหลว: {e}")

        if total >= limit:
            break

    if not out_parts:
        return []

    data = pd.concat(out_parts, ignore_index=True)
    if len(data) > limit:
        data = data.iloc[:limit].copy()

    for col in ["lat","lng","month","year","zone","cane_type","plant_id","ndvi","ndwi","gli","precipitation"]:
        if col not in data.columns:
            data[col] = np.nan if col not in ["zone","cane_type","plant_id"] else ""

    return data.to_dict(orient="records")


RAW_BASE_USE_MEMORY = bool(int(os.getenv("RAW_BASE_USE_MEMORY", "1")))
RAW_BASE_MEM_CACHE: Dict[str, List[Dict[str, Any]]] = {}
RAW_BASE_MEM_KEYS: List[str] = []
RAW_BASE_MEM_MAX = int(os.getenv("RAW_BASE_MEM_MAX", "3"))


def _csv_signature(paths: List[str]) -> str:
    parts = []
    for p in paths:
        try:
            parts.append(f"{os.path.basename(p)}:{int(os.path.getmtime(p))}:{os.path.getsize(p)}")
        except Exception:
            parts.append(f"{os.path.basename(p)}:0:0")
    return "|".join(parts)


def _base_raw_key(year: int, start_month: int, end_month: int, zones: str, limit: int) -> str:
    files = _csv_paths()
    sig = _csv_signature(files)
    return generate_cache_key(
        "raw_base_csv_v1",
        year=year, start_month=start_month, end_month=end_month, zones=zones, limit=limit, csv_sig=sig
    )


async def get_base_raw_cached(year: int, start_month: int, end_month: int, zones: str, limit: int) -> List[Dict[str, Any]]:
    key = _base_raw_key(year, start_month, end_month, zones, limit)

    if RAW_BASE_USE_MEMORY and key in RAW_BASE_MEM_CACHE:
        return RAW_BASE_MEM_CACHE[key]

    cached = await get_from_cache(key)
    if cached and "data" in cached:
        data = cached["data"]
        if RAW_BASE_USE_MEMORY:
            RAW_BASE_MEM_CACHE[key] = data
            RAW_BASE_MEM_KEYS.append(key)
            if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
                old = RAW_BASE_MEM_KEYS.pop(0)
                RAW_BASE_MEM_CACHE.pop(old, None)
        return data

    data = read_raw_data_from_csv(
        year=year, start_month=start_month, end_month=end_month,
        zones=zones, limit=limit
    )
    await set_to_cache(key, data, ttl=min(CACHE_TTL, 12 * 3600))

    if RAW_BASE_USE_MEMORY:
        RAW_BASE_MEM_CACHE[key] = data
        RAW_BASE_MEM_KEYS.append(key)
        if len(RAW_BASE_MEM_KEYS) > RAW_BASE_MEM_MAX:
            old = RAW_BASE_MEM_KEYS.pop(0)
            RAW_BASE_MEM_CACHE.pop(old, None)

    return data


def filter_viewport_records(records: List[Dict[str, Any]], viewport: Optional[Dict[str, float]]) -> List[Dict[str, Any]]:
    if not viewport:
        return records
    min_la, max_la = viewport["min_lat"], viewport["max_lat"]
    min_lo, max_lo = viewport["min_lng"], viewport["max_lng"]
    out: List[Dict[str, Any]] = []
    for r in records:
        la = r.get("lat"); lo = r.get("lng")
        if la is None or lo is None:
            continue
        try:
            la = float(la); lo = float(lo)
        except Exception:
            continue
        if (min_la <= la <= max_la) and (min_lo <= lo <= max_lo):
            out.append(r)
    return out

# ----------------- ปรับโซน -----------------

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

# ----------------- ยูทิล: บังคับผล predict เป็น 1D เวกเตอร์ -----------------

def _normalize_scores_to_1d(scores: np.ndarray) -> np.ndarray:
    """
    บังคับผลลัพธ์จาก xgboost.predict ให้เป็นเวกเตอร์ 1 มิติ:
    - ถ้า 2D: เลือกคอลัมน์ที่ 1 เมื่อมี >=3 คอลัมน์ (multi-class เลือก prob ของคลาสบวก),
              มิฉะนั้นเลือกคอลัมน์แรก
    - ถ้า 0D: reshape เป็น (1,)
    - แปลง dtype เป็น float32 เพื่อลดหน่วยความจำ
    """
    scores = np.asarray(scores)
    if scores.ndim == 2:
        scores = scores[:, 1] if scores.shape[1] >= 3 else scores[:, 0]
    elif scores.ndim == 0:
        scores = scores.reshape(1,)
    return scores.astype(np.float32, copy=False)

# ----------------- เตรียมฟีเจอร์สำหรับโมเดล (non-stream; ใช้ใน /predict/grouped) -----------------

def prepare_data_for_model(raw_data: List[Dict], required_features: List[str]) -> pd.DataFrame:
    if not raw_data:
        return pd.DataFrame()

    df = pd.DataFrame(raw_data)
    if df.empty or not {"lat","lng"}.issubset(df.columns):
        return pd.DataFrame()

    if "plant_id" not in df.columns:
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

        for _, r in g.iterrows():
            mo = int(r.get("month") or 0)
            if 1 <= mo <= 12:
                mname = months[mo-1]
                fmap = {"NDVI":"ndvi","GLI":"gli","NDWI":"ndwi","Precipitation":"precipitation"}
                for F, col in fmap.items():
                    fname = f"{F}_{mname}"
                    if fname in required_features:
                        row[fname] = float(r.get(col, 0) or 0.0)
        processed.append(row)

    if not processed:
        return pd.DataFrame()

    res = pd.DataFrame(processed)
    for f in required_features:
        if f not in res.columns:
            res[f] = 0.0
    return res

# ----------------- พยากรณ์แบบ batch (non-stream; ใช้ใน /predict/grouped) -----------------

def predict_with_model_batched(model_info: Dict, data: pd.DataFrame, batch_size: int = BATCH_PREDICTION_SIZE) -> pd.DataFrame:
    required = model_info["required_features"]
    booster = model_info["booster"]
    feats = data[required].fillna(0)

    all_scores = []
    for i in range(0, len(feats), batch_size):
        batch = feats.iloc[i:i+batch_size]
        # Pin feature names to avoid order/name surprises
        dmat = xgb.DMatrix(batch, feature_names=required)
        s = booster.predict(dmat)
        del dmat
        # ใช้ normalizer ให้ได้ 1D เสมอ
        s1 = _normalize_scores_to_1d(s)
        all_scores.extend(s1)
        if i % (batch_size*5) == 0:
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

    # ใช้เวกเตอร์ 1D ที่ normalize แล้ว
    out["prediction"] = scores

    for F, s in avg_features.items():
        out[F.lower()] = s

    out = apply_zone_adjustments(out)
    return out

# ----------------- (A) โหมดสตรีมมิงสำหรับ /predict/grouped/averages -----------------

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


async def process_model_predictions_streaming(
    model_name: str,
    year: int,
    start_month: int,
    end_month: int,
    zones_str: str
) -> GroupedModelPredictionResult:
    start_t = time.time()
    model = load_model(model_name)
    required_features = model["required_features"]
    booster = model["booster"]

    files = _csv_paths()
    acc: Dict[str, Dict[str, Any]] = {}
    total_rows_seen = 0

    for path in files:
        logger.info(f"[stream] อ่าน CSV: {path}")
        try:
            for chunk in pd.read_csv(path, chunksize=CSV_READ_CHUNKSIZE, low_memory=True):
                fchunk = _chunk_filter(chunk, year, start_month, end_month, zones_str)
                if fchunk.empty:
                    continue

                for col in ["lat","lng","month","year","zone","cane_type","plant_id","ndvi","ndwi","gli","precipitation"]:
                    if col not in fchunk.columns:
                        fchunk[col] = np.nan if col not in ["zone","cane_type","plant_id"] else ""

                for rec in fchunk.to_dict(orient="records"):
                    pid = rec.get("plant_id")
                    if not pid and pd.notna(rec.get("lat")) and pd.notna(rec.get("lng")):
                        pid = f"{float(rec['lat']):.6f}_{float(rec['lng']):.6f}"
                        rec["plant_id"] = pid
                    if not pid:
                        continue

                    if pid not in acc:
                        row = _empty_feature_row(required_features)
                        acc[pid] = row
                    else:
                        row = acc[pid]

                    _update_feature_row(row, rec, required_features)
                    total_rows_seen += 1

                del chunk, fchunk
                gc.collect()
        except Exception as e:
            logger.error(f"[stream] อ่านไฟล์ {path} ล้มเหลว: {e}")
        finally:
            gc.collect()

    if not acc:
        return GroupedModelPredictionResult(
            model_name=model_name,
            prediction_groups=[],
            zone_statistics=[],
            overall_average=0.0,
            total_predictions=0
        )

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

        for i, z in enumerate(zones_arr):
            scores[i] += float(ZONE_PREDICTION_ADJUSTMENTS.get(z, 0.0))

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

    logger.info(f"[stream] {model_name}: plants={len(acc)} rows={total_rows_seen} preds={total_cnt} time={time.time()-start_t:.2f}s")

    return GroupedModelPredictionResult(
        model_name=model_name,
        prediction_groups=[],      # โหมด averages ไม่คืนรายจุด
        zone_statistics=zone_stats,
        overall_average=overall_avg,
        total_predictions=total_cnt
    )

# ----------------- (B) กลุ่ม/สถิติ (non-stream; ใช้ใน /predict/grouped) -----------------

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


def group_predictions_by_level(pred_df: pd.DataFrame, raw_data: List[Dict], display_month: int = 6) -> List[PredictionGroup]:
    res: List[PredictionGroup] = []
    total = len(pred_df)
    months = ['January','February','March','April','May','June','July','August','September','October','November','December']
    display_name = months[display_month-1] if 1 <= display_month <= 12 else f"Month_{display_month}"

    dfr = pd.DataFrame(raw_data)
    if "plant_id" not in dfr.columns and {"lat","lng"}.issubset(dfr.columns):
        dfr["plant_id"] = dfr.apply(lambda r: f"{float(r['lat']):.6f}_{float(r['lng']):.6f}", axis=1)

    show_cols = ["ndvi","ndwi","gli","precipitation"]
    disp = dfr[dfr["month"] == display_month].groupby("plant_id", as_index=True)[show_cols].mean() if not dfr.empty else pd.DataFrame()

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

            grouped.append(GroupedPlantPrediction(
                lat=float(getattr(row, "lat")),
                lon=float(getattr(row, "lon")),
                plant_id=str(pid),
                prediction=float(getattr(row, "prediction")),
                prediction_level=lvl,
                ndvi=ndvi, ndwi=ndwi, gli=gli, precipitation=prcp,
                zone=str(getattr(row, "zone")), cane_type=str(getattr(row, "cane_type")),
                display_month=display_month, display_month_name=display_name
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

# ----------------- ประมวลผลโมเดล (non-stream; ใช้ CSV) -----------------

async def process_model_predictions(
    model_name: str,
    raw_data: List[Dict],
    display_month: int,
    enable_chunked: bool = True,
    zones_str: Optional[str] = None,
    include_zone_stats: bool = True,   # สำหรับ /predict/grouped: จะส่ง False
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

        model = load_model(model_name)
        prepared = prepare_data_for_model(raw_data, model["required_features"])
        if prepared.empty:
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

        preds = predict_with_model_batched(model, prepared)
        if preds.empty:
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

        groups = group_predictions_by_level(preds, raw_data, display_month)
        req_zones = [z.strip() for z in (zones_str or "").split(",") if z.strip()] if zones_str else None

        if include_zone_stats:
            zone_stats = calculate_zone_statistics(preds, requested_zones=req_zones)
            total_preds_count = int(sum(z.total_plantations for z in zone_stats)) if zone_stats else len(preds)
        else:
            zone_stats = []
            total_preds_count = len(preds)

        overall_avg = round(float(preds["prediction"].mean()), 2)

        logger.info(f"Model {model_name}: {len(preds)} rows in {time.time()-start:.2f}s")
        del prepared, preds
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

# ----------------- แกนหลักของ grouped prediction (viewport endpoint) -----------------

async def _predict_grouped_core(request: GroupedPredictionRequest, raw_data: Optional[List[Dict]] = None) -> GroupedPredictionResponse:
    start_t = time.time()
    zones_used = zones_for_year(request.year, request.zones)

    cache_key = generate_cache_key(
        "predict_grouped_csv_v1",
        year=request.year, start_month=request.start_month, end_month=request.end_month,
        models=request.models, zones=zones_used, limit=request.limit,
        group_by_level=request.group_by_level, display_month=request.display_month,
        min_lat=request.min_lat, min_lng=request.min_lng, max_lat=request.max_lat, max_lng=request.max_lng
    )

    if raw_data is None:
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

    viewport = None
    if all(v is not None for v in [request.min_lat, request.min_lng, request.max_lat, request.max_lng]):
        viewport = {
            "min_lat": float(request.min_lat), "max_lat": float(request.max_lat),
            "min_lng": float(request.min_lng), "max_lng": float(request.max_lng)
        }

    if raw_data is None:
        base_raw = await get_base_raw_cached(
            year=request.year,
            start_month=request.start_month,
            end_month=request.end_month,
            zones=zones_used,
            limit=int(request.limit or 1000000)
        )
        raw_data = filter_viewport_records(base_raw, viewport)
        if not raw_data:
            raise HTTPException(status_code=404, detail="No data found from CSV with given filters")
    else:
        base_raw = raw_data

    sem = asyncio.Semaphore(max(1, int(request.max_concurrent_models or 2)))

    async def _run(m):
        async with sem:
            return await process_model_predictions(
                m, raw_data, display_month=request.display_month,
                enable_chunked=request.enable_chunked_processing, zones_str=zones_used,
                include_zone_stats=False
            )

    tasks = [asyncio.create_task(_run(m)) for m in request.models]
    model_results = await asyncio.gather(*tasks)
    results = [r for r in model_results if r is not None]

    if not results:
        raise HTTPException(status_code=500, detail="Failed to process all models")

    response = GroupedPredictionResponse(
        success=True,
        message=f"Processed {len(results)} models from CSV",
        results=results,
        cached=False,
        cache_key=cache_key,
        processing_stats={
            "total_time_sec": round(time.time()-start_t, 3),
            "zones_used": zones_used,
            "csv_files": _csv_paths(),
            "base_rows_loaded": len(base_raw),
            "rows_after_viewport": len(raw_data),
            "used_base_cache": True
        }
    )
    await set_to_cache(cache_key, response.dict(), ttl=min(CACHE_TTL, 3600))
    return response

# ----------------- คำนวณค่าเฉลี่ยหลายโมเดล (รองรับโหมดสตรีมมิง) -----------------

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
                zone_averages=zavg
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

# ----------------- Endpoints -----------------

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
        y = MODEL_YEAR_CONFIG.get(m, 2024)
        models_by_year.setdefault(y, []).append(m)

    zones_by_year = {y: zones_for_year(y, zones) for y in models_by_year.keys()}

    cache_key = generate_cache_key(
        "model_averages_stream_v1",
        start_month=start_month, end_month=end_month,
        models=models_list, zones_by_year=zones_by_year,
        display_month=display_month,
        model_year_config=MODEL_YEAR_CONFIG
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
            res = await process_model_predictions_streaming(
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
        "csv_files": _csv_paths(),
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


@app.delete("/cache/clear")
async def clear_cache():
    deleted = await delete_cache_pattern(f"{CACHE_PREFIX}_*")
    return {"success": True, "message": f"Cleared {deleted} cache files", "deleted_count": deleted}

# ----------------- Run -----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=1290, reload=False, workers=1, access_log=False)
