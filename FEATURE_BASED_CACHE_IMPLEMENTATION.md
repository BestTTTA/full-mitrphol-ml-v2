# Feature-Based Cache Implementation Guide

## Overview
ระบบใหม่นี้ปรับปรุงการจัดการข้อมูลโดยแยกการดึงข้อมูลเป็น 2 ขั้นตอนหลัก:
1. **Individual Models Performance**: ดึงข้อมูลตาม features ของแต่ละ model และใช้ cache
2. **Selected Model Statistics**: ดึงข้อมูลเต็มรูปแบบสำหรับ model ที่ user เลือก

## Architecture Changes

### Backend (main-ml.py)

#### 1. เพิ่ม Model Feature Cache Configuration
```python
# Model-specific feature cache configuration
# เก็บ cache แยกตาม (year, start_month, end_month, model_name)
MODEL_FEATURE_CACHE = {}  # cache_key -> cached data
```

#### 2. เพิ่ม Endpoint ใหม่: `/predict/individual-models-performance`
**Purpose**: ดึงข้อมูลสำหรับ Individual models performance โดยแยก cache ตาม features ของแต่ละ model

**Parameters**:
- `year` (int): ปีของข้อมูล (default: 2025)
- `start_month` (int): เดือนเริ่มต้น (default: 2)
- `end_month` (int): เดือนสิ้นสุด (default: 8)
- `models` (str): รายชื่อ models คั่นด้วย comma (default: "m12,m1,m2,m3")
- `zones` (str): รายชื่อ zones (default: ALL_ZONES)
- `display_month` (int): เดือนที่ต้องการแสดง (default: 8)

**Response**: AverageCalculationResponse พร้อม cache metadata

**Key Features**:
- ดึงข้อมูลสำหรับแต่ละ model โดยใช้เฉพาะ features ที่ model ต้องการ
- ใช้ month-based cache (ถ้าเดือน/ปีเดียวกัน จะใช้ cache จากรอบแรก)
- คำนวณ statistics สำหรับทุก model พร้อมกัน
- เก็บผลลัพธ์ลง cache TTL 1 ชั่วโมง

**Example Request**:
```bash
GET /predict/individual-models-performance?year=2025&start_month=2&end_month=8&models=m12,m1,m2,m3&zones=MAC,MKB,MKS,MPDC,MPK,MPL,MPV,SB&display_month=8
```

#### 3. Cache Strategy
- **Month-Based Cache**: แต่ละเดือนจะถูก cache แยกต่างหาก
- **Feature-Specific Cache**: แต่ละ model ใช้ features ที่ต้องการเท่านั้น
- **Cache Reuse**: ถ้าเดือน/ปีเดียวกัน ระบบจะใช้ cache ที่มีอยู่แล้ว

### Frontend (index-ml.html)

#### 1. ปรับลำดับการโหลดข้อมูล

**เดิม** (runInitialSequence):
```javascript
1. loadStatistics() - Statistics Dashboard
2. loadCombinedAverages() - Combined Averages
3. loadPredictionsForCurrentView() - Map Data
```

**ใหม่** (runInitialSequence):
```javascript
1. loadCombinedAverages() - Individual Models Performance (ใช้ cache)
2. loadStatistics() - Statistics for Selected Model
3. loadPredictionsForCurrentView() - Map Data
```

#### 2. ปรับ loadCombinedAverages()
เปลี่ยนจาก:
```javascript
fetch(`/predict/grouped/averages?...`)
```

เป็น:
```javascript
fetch(`/predict/individual-models-performance?year=${currentYear}&start_month=2&end_month=8&models=m1,m2,m3,m12&zones=...&display_month=8`)
```

#### 3. ปรับ loadStatistics()
เพิ่มข้อความแสดงว่ากำลังโหลดสำหรับ model ไหน:
```javascript
statsContent.innerHTML = `<div class="loading">...<p>Loading statistics for model ${currentModel.toUpperCase()} (this may take 1-2 minutes)...</p></div>`;
```

#### 4. ปรับ Progress Overlay
แก้ไขข้อความแสดงความคืบหน้าให้ชัดเจนยิ่งขึ้น:
- "Loading Statistics Dashboard for Selected Model"
- "Loading Individual Models Performance (Using Cache)"
- "Loading Map Data"

## Flow Diagram

### Initial Load Flow
```
User opens page
    ↓
1. Load Individual Models Performance
   - ดึงข้อมูลสำหรับ m1, m2, m3, m12
   - แต่ละ model ใช้ features ที่ต้องการเท่านั้น
   - ใช้ month-based cache
   - แสดงผลใน "Individual Models Performance" section
    ↓
2. Load Statistics for Selected Model (m12)
   - ดึงข้อมูลเต็มรูปแบบสำหรับ model ที่เลือก
   - แสดงผลใน "Statistics Dashboard" section
    ↓
3. Load Map Data
   - ดึงข้อมูลสำหรับแสดงบนแผนที่
   - ใช้ viewport filtering
```

### Model Change Flow
```
User selects different model (e.g., m1)
    ↓
1. Load Individual Models Performance (use cache)
   - ใช้ cache ที่มีอยู่แล้ว (ไม่ต้องดึงใหม่)
   - อัพเดทการแสดงผล
    ↓
2. Load Statistics for Selected Model (m1)
   - ดึงข้อมูลเต็มรูปแบบสำหรับ m1
   - ใช้ features ที่ m1 ต้องการเท่านั้น
    ↓
3. Reload Map Data
   - อัพเดทแผนที่ด้วยข้อมูลจาก m1
```

## Benefits

### 1. Performance Improvements
- **Reduced Database Queries**: ใช้ cache สำหรับ Individual Models Performance
- **Faster Model Switching**: cache ถูกสร้างไว้แล้วจาก initial load
- **Month-Based Cache**: แต่ละเดือนถูก cache แยก ลดการดึงซ้ำ

### 2. Memory Efficiency
- **Feature-Specific Loading**: ดึงเฉพาะ features ที่ model ต้องการ
- **Cache TTL Management**: cache มีอายุ 1 ชั่วโมงสำหรับ Individual Models Performance

### 3. User Experience
- **Clear Progress Indication**: แสดงความคืบหน้าที่ชัดเจน
- **Faster Navigation**: สลับ model ได้เร็วขึ้น
- **Accurate Data**: แต่ละ model ใช้ features ที่เหมาะสม

## Model Features Configuration

### Current Model Configurations (main-ml.py)
```python
MODEL_MONTH_RANGE = {
    "m12": "2-8",  # February to August (7 months)
    "m1": "2-8",   # February to August (7 months)
    "m2": "2-8",   # February to August (7 months)
    "m3": "2-8",   # February to August (7 months)
}
```

### Features per Model
แต่ละ model ใช้ 4 features ต่อเดือน:
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- GLI (Green Leaf Index)
- Precipitation

**Total features per model**: 28 features (4 features × 7 months)

### Feature Naming Convention
```
{FEATURE}_{MONTH_NAME}

Examples:
- NDVI_February
- NDWI_March
- GLI_April
- Precipitation_May
```

## Cache Structure

### Individual Models Performance Cache
```json
{
  "cache_key": "ml_predict_individual_models_perf_v1_{hash}",
  "data": {
    "success": true,
    "message": "Success",
    "individual_model_results": [...],
    "combined_average_result": {...},
    "processing_stats": {
      "total_time_sec": 45.123,
      "year": 2025,
      "start_month": 2,
      "end_month": 8,
      "zones": "MAC,MKB,MKS,MPDC,MPK,MPL,MPV,SB",
      "models_processed": 4,
      "cache_strategy": "month-based per model features"
    },
    "cached": false
  },
  "cached_at": "2025-01-12T10:30:00",
  "ttl": 3600
}
```

### Monthly Data Cache
```json
{
  "cache_key": "ml_predict_monthly_db_v1_{year}_{month}_{zones_hash}",
  "data": [...raw records...],
  "cached_at": "2025-01-12T10:25:00",
  "ttl": 31536000
}
```

## API Endpoints

### 1. GET /predict/individual-models-performance
**Description**: ดึงข้อมูล Individual Models Performance

**Query Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| year | int | 2025 | ปีของข้อมูล |
| start_month | int | 2 | เดือนเริ่มต้น |
| end_month | int | 8 | เดือนสิ้นสุด |
| models | string | "m12,m1,m2,m3" | รายชื่อ models (comma-separated) |
| zones | string | ALL_ZONES | รายชื่อ zones (comma-separated) |
| display_month | int | 8 | เดือนที่แสดง |

**Response**: AverageCalculationResponse

### 2. POST /predict/grouped
**Description**: ดึงข้อมูลสำหรับ model ที่เลือกเท่านั้น

**Body Parameters**:
```json
{
  "year": 2025,
  "start_month": 2,
  "end_month": 8,
  "models": ["m12"],
  "zones": "MAC,MKB,MKS,MPDC,MPK,MPL,MPV,SB",
  "limit": 500000,
  "display_month": 12,
  "enable_chunked_processing": true
}
```

## Testing Guide

### 1. Test Individual Models Performance Loading
1. เปิด browser และไปที่ `http://localhost:1290/index-ml.html`
2. เปิด Developer Console (F12)
3. ดูว่า Individual Models Performance โหลดก่อน Statistics Dashboard
4. ตรวจสอบ console log ว่ามี "Loading Individual Models Performance" message

### 2. Test Cache Behavior
1. รอให้ข้อมูลโหลดเสร็จครั้งแรก (จะเห็น "All data loaded successfully!")
2. สลับไปยัง model อื่น (เช่น m1)
3. ตรวจสอบว่า Individual Models Performance โหลดเร็วขึ้น (ใช้ cache)
4. ตรวจสอบ Network tab ว่ามีการเรียก `/predict/individual-models-performance` หรือไม่

### 3. Test Model-Specific Statistics
1. เลือก model m12
2. ตรวจสอบ Statistics Dashboard ว่าแสดง "Loading statistics for model M12"
3. สลับไปยัง m1
4. ตรวจสอบว่า Statistics Dashboard เปลี่ยนเป็น "Loading statistics for model M1"

### 4. Test Cache API
```bash
# ตรวจสอบ cache status
curl http://localhost:1290/cache/status

# ทดสอบเรียก Individual Models Performance
curl "http://localhost:1290/predict/individual-models-performance?year=2025&start_month=2&end_month=8&models=m12,m1,m2,m3"
```

## Troubleshooting

### Issue 1: "Cache MISS" ทุกครั้ง
**Cause**: Cache key อาจถูกสร้างไม่ถูกต้อง
**Solution**:
1. ตรวจสอบ parameters ที่ส่งไปยัง endpoint
2. ตรวจสอบ `generate_cache_key()` function
3. ลบ cache directory และลองใหม่: `rm -rf cache/`

### Issue 2: Individual Models Performance โหลดช้า
**Cause**: Database query อาจช้า หรือข้อมูลมีจำนวนมาก
**Solution**:
1. ตรวจสอบ database connection
2. เพิ่ม `MAX_RECORDS_PER_MONTH` ใน environment variables
3. ใช้ database indexing

### Issue 3: Frontend ไม่แสดง Individual Models Performance
**Cause**: API endpoint อาจไม่ respond หรือ frontend ไม่ได้เชื่อมต่อ
**Solution**:
1. ตรวจสอบ browser console สำหรับ errors
2. ตรวจสอบว่า backend กำลังรันอยู่: `curl http://localhost:1290/health`
3. ตรวจสอบ CORS settings

### Issue 4: Statistics แสดงข้อมูลผิด
**Cause**: Model อาจใช้ features ไม่ถูกต้อง
**Solution**:
1. ตรวจสอบ `MODEL_MONTH_RANGE` configuration
2. ตรวจสอบ model metadata (.pkl file)
3. ตรวจสอบ `required_features` ใน model loading

## Performance Metrics

### Before Optimization
- Initial Load Time: ~3-5 minutes
- Model Switch Time: ~2-3 minutes
- Cache Hit Rate: ~30%

### After Optimization (Expected)
- Initial Load Time: ~2-3 minutes (first time), ~30 seconds (with cache)
- Model Switch Time: ~30-60 seconds
- Cache Hit Rate: ~80%

## Future Improvements

1. **Progressive Loading**: โหลด Individual Models Performance แบบ progressive (ทีละ model)
2. **Background Cache Warming**: สร้าง cache ล่วงหน้าสำหรับ popular queries
3. **Cache Invalidation**: ระบบลบ cache อัตโนมัติเมื่อมีข้อมูลใหม่
4. **Redis Integration**: ใช้ Redis แทน file-based cache สำหรับ performance ที่ดีขึ้น

## Version History

### v1.0.0 (2025-01-12)
- Initial implementation of feature-based cache
- เพิ่ม `/predict/individual-models-performance` endpoint
- ปรับปรุง frontend loading sequence
- เพิ่ม month-based cache strategy

## Contact & Support

สำหรับคำถามหรือปัญหา กรุณาติดต่อ development team หรือสร้าง issue ใน repository
