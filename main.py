import uuid
import re
from datetime import datetime
from typing import List, Optional, Any, Dict, Set, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prisma import Prisma
from pydantic import BaseModel

app = FastAPI()
prisma = Prisma()
# cicd test0128

# ==========================
# CORS（前后端分离必需）
# ==========================
# ✅ 你可以按需改成自己的前端地址：
# - 本地开发：http://localhost:3000
# - 你集群 NodePort 前端：http://172.16.200.95:32030
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://172.16.200.95:32030",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await prisma.connect()


@app.on_event("shutdown")
async def shutdown():
    await prisma.disconnect()


# ==========================================
# 定义数据模型 (Pydantic)
# ==========================================

class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    birthday: Optional[str] = None  # 前端传 ISO 字符串


class PatientCreate(BaseModel):
    subject_label: str
    protocol_id: str


class CrfDataUpdate(BaseModel):
    table_name: str
    data: Dict[str, Any]


# ==========================================
# ✅ 脑疾病风险统计：辅助函数
# ==========================================

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")

def safe_ident(x: str) -> str:
    """仅允许 a-zA-Z0-9_，用于拼接表名/列名，避免 SQL 注入。"""
    if not x or not _IDENT_RE.match(x):
        raise ValueError(f"Unsafe identifier: {x}")
    return x

def has_any_keyword(text: str, keywords: List[str]) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)

def bool_positive_pred(col: str) -> str:
    # 适用于 “是/有/阳性/确诊/既往/√/1/true”等
    return f"""
    TRIM(COALESCE(`{col}`, '')) <> '' AND (
      LOWER(TRIM(`{col}`)) IN ('1','true','yes','y','positive') OR
      TRIM(`{col}`) IN ('是','有','阳性','存在','已确诊','确诊','既往','√') OR
      TRIM(`{col}`) REGEXP '(是|有|阳性|存在|确诊|既往|√)'
    )
    """

def numeric_gte_pred(col: str, thr: float) -> str:
    return f"""
    TRIM(COALESCE(`{col}`, '')) REGEXP '^-?[0-9]+(\\.[0-9]+)?$'
    AND CAST(`{col}` AS DECIMAL(10,3)) >= {thr}
    """

def numeric_lte_pred(col: str, thr: float) -> str:
    return f"""
    TRIM(COALESCE(`{col}`, '')) REGEXP '^-?[0-9]+(\\.[0-9]+)?$'
    AND CAST(`{col}` AS DECIMAL(10,3)) <= {thr}
    """

async def query_distinct_patient_ids(table_name: str, col: str, predicate_sql: str) -> Set[str]:
    """在某个动态表中，按 predicate 查 DISTINCT patient_id。表不存在时安全返回空集合。"""
    t = safe_ident(table_name)
    c = safe_ident(col)
    sql = f"SELECT DISTINCT patient_id FROM `{t}` WHERE {predicate_sql}"
    try:
        rows = await prisma.query_raw(sql)
    except Exception:
        return set()

    s: Set[str] = set()
    for r in rows or []:
        pid = r.get("patient_id")
        if pid is not None:
            s.add(str(pid))
    return s


# -------------------------------------------
# 1. 管理员端 API
# -------------------------------------------
@app.get("/api/admin/stats")
async def get_admin_stats():
    """获取系统统计数据 (含性别、年龄分布)"""

    user_count = await prisma.user.count()
    try:
        patients = await prisma.query_raw("SELECT COUNT(*) as count FROM patients")
        patient_count = patients[0]["count"]
    except:
        patient_count = 0

    gender_stats = []
    try:
        raw_genders = await prisma.query_raw("""
            SELECT gender, COUNT(*) as count
            FROM user
            WHERE gender IS NOT NULL
            GROUP BY gender
        """)

        gender_map = {'male': '男', 'female': '女', 'Male': '男', 'Female': '女'}
        for g in raw_genders:
            origin_val = g['gender']
            label = gender_map.get(origin_val, origin_val)
            gender_stats.append({"name": label, "value": int(g['count'])})
    except Exception as e:
        print(f"Gender stats error: {e}")

    year_stats = []
    try:
        raw_years = await prisma.query_raw("""
            SELECT DATE_FORMAT(birthday, '%Y') as year, COUNT(*) as count
            FROM user
            WHERE birthday IS NOT NULL
            GROUP BY year
            ORDER BY year ASC
        """)
        for y in raw_years:
            if y['year']:
                year_stats.append({"year": y['year'], "count": int(y['count'])})
    except Exception as e:
        print(f"Year stats error: {e}")

    return {
        "user_count": user_count,
        "patient_count": patient_count,
        "gender_stats": gender_stats,
        "year_stats": year_stats
    }


# -------------------------------------------
# ✅ 新增：脑疾病风险统计 API（前后端分离用）
# -------------------------------------------
@app.get("/api/risk/brain")
async def get_brain_risk_stats():
    """
    脑疾病相关风险统计（简单版）：
    - totalPatients：patients 表总数
    - vascularHighRiskPatients：血管高危（高血压/糖尿病/房颤/吸烟 >=2）
    - strokeHistoryPatients：卒中/TIA/脑梗/脑出血相关
    - cognitiveRiskPatients：认知风险（痴呆/认知障碍 or MMSE/MoCA 低分）
    说明：字段来源由 system_data_dictionary 自动匹配关键词定位，不写死某个 CRF 表
    """

    # 1) 总人数
    try:
        rows = await prisma.query_raw("SELECT COUNT(*) as count FROM patients")
        total_patients = int(rows[0]["count"]) if rows else 0
    except:
        total_patients = 0

    # 2) 读字典表
    dict_rows = await prisma.query_raw("""
        SELECT table_name, column_name, display_label
        FROM system_data_dictionary
    """) or []

    # 关键词（只用于“定位字段”，不是写进网页的汇报内容）
    KW = {
        "hypertension": ["高血压", "血压", "收缩压", "舒张压", "sbp", "dbp"],
        "diabetes": ["糖尿病", "血糖", "空腹血糖", "hba1c", "糖化血红蛋白"],
        "afib": ["房颤", "心房颤动", "心律失常", "af"],
        "smoking": ["吸烟", "抽烟", "烟草", "烟龄"],
        "stroke": ["卒中", "中风", "脑梗", "脑梗死", "脑出血", "tia", "短暂性脑缺血"],
        "cognitive": ["认知", "痴呆", "阿尔茨海默", "moca", "mmse", "认知障碍"],
    }

    def pick_cols(keywords: List[str]) -> List[Tuple[str, str, str]]:
        out = []
        for r in dict_rows:
            t = str(r.get("table_name") or "")
            c = str(r.get("column_name") or "")
            d = str(r.get("display_label") or "")
            hay = f"{d} {c}"
            if has_any_keyword(hay, keywords):
                out.append((t, c, d))
        return out

    cols_hy = pick_cols(KW["hypertension"])
    cols_dm = pick_cols(KW["diabetes"])
    cols_af = pick_cols(KW["afib"])
    cols_sm = pick_cols(KW["smoking"])
    cols_stroke = pick_cols(KW["stroke"])
    cols_cog = pick_cols(KW["cognitive"])

    # 3) 分别查集合
    hypertension_set: Set[str] = set()
    for t, c, d in cols_hy:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = bool_positive_pred(col)
        # SBP/DBP 阈值
        if ("收缩压" in d) or ("sbp" in col.lower()):
            pred = numeric_gte_pred(col, 140)
        if ("舒张压" in d) or ("dbp" in col.lower()):
            pred = numeric_gte_pred(col, 90)

        ids = await query_distinct_patient_ids(table, col, pred)
        hypertension_set |= ids

    diabetes_set: Set[str] = set()
    for t, c, d in cols_dm:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = bool_positive_pred(col)
        # HbA1c >= 6.5 或 血糖/空腹血糖 >= 7.0（简单阈值）
        if ("hba1c" in d.lower()) or ("糖化血红蛋白" in d):
            pred = numeric_gte_pred(col, 6.5)
        if ("空腹血糖" in d) or ("血糖" in d):
            pred = numeric_gte_pred(col, 7.0)

        ids = await query_distinct_patient_ids(table, col, pred)
        diabetes_set |= ids

    afib_set: Set[str] = set()
    for t, c, d in cols_af:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = f"""
        ({bool_positive_pred(col)})
        OR (TRIM(COALESCE(`{col}`, '')) REGEXP '(房颤|心房颤动|心律失常|AF)')
        """
        ids = await query_distinct_patient_ids(table, col, pred)
        afib_set |= ids

    smoking_set: Set[str] = set()
    for t, c, d in cols_sm:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = bool_positive_pred(col)
        if "烟龄" in d:
            pred = numeric_gte_pred(col, 1)

        ids = await query_distinct_patient_ids(table, col, pred)
        smoking_set |= ids

    stroke_set: Set[str] = set()
    for t, c, d in cols_stroke:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = f"""
        ({bool_positive_pred(col)})
        OR (TRIM(COALESCE(`{col}`, '')) REGEXP '(卒中|中风|脑梗|脑梗死|脑出血|TIA|短暂性脑缺血)')
        """
        ids = await query_distinct_patient_ids(table, col, pred)
        stroke_set |= ids

    cognitive_set: Set[str] = set()
    for t, c, d in cols_cog:
        try:
            col = safe_ident(c)
            table = safe_ident(t)
        except:
            continue

        pred = f"""
        ({bool_positive_pred(col)})
        OR (TRIM(COALESCE(`{col}`, '')) REGEXP '(痴呆|阿尔茨海默|认知障碍|认知)')
        """

        # 量表低分风险：MMSE<=23, MoCA<=25（简单阈值）
        if "mmse" in d.lower():
            pred = numeric_lte_pred(col, 23)
        if "moca" in d.lower():
            pred = numeric_lte_pred(col, 25)

        ids = await query_distinct_patient_ids(table, col, pred)
        cognitive_set |= ids

    # 4) 血管高危：>=2 个危险因素
    union = set().union(hypertension_set, diabetes_set, afib_set, smoking_set)
    vascular_high_risk_set: Set[str] = set()
    for pid in union:
        n = 0
        if pid in hypertension_set: n += 1
        if pid in diabetes_set: n += 1
        if pid in afib_set: n += 1
        if pid in smoking_set: n += 1
        if n >= 2:
            vascular_high_risk_set.add(pid)

    return {
        "totalPatients": total_patients,
        "vascularHighRiskPatients": len(vascular_high_risk_set),
        "strokeHistoryPatients": len(stroke_set),
        "cognitiveRiskPatients": len(cognitive_set),
        # 方便你调试：看看字典匹配到了多少列（不用于前端展示也行）
        "meta": {
            "matchedColumns": {
                "hypertension": len(cols_hy),
                "diabetes": len(cols_dm),
                "afib": len(cols_af),
                "smoking": len(cols_sm),
                "stroke": len(cols_stroke),
                "cognitive": len(cols_cog),
            }
        }
    }


# -------------------------------------------
# 2. 用户端/医生端 API (读取)
# -------------------------------------------

@app.get("/api/patients")
async def get_patients():
    """获取所有患者列表"""
    try:
        sql = """
              SELECT id,
                     subject_label,
                     protocol_id,
                     DATE_FORMAT(created_at, '%Y-%m-%d') as created_at
              FROM patients
              ORDER BY created_at DESC
              """
        data = await prisma.query_raw(sql)
        return data
    except Exception as e:
        print(f"Error fetching patients: {e}")
        return []


@app.get("/api/structure")
async def get_clinical_structure():
    """获取由 Event 和 CRF 组成的树状结构"""
    try:
        events = await prisma.query_raw("""
            SELECT code, name, ordinal
            FROM meta_study_structure
            WHERE type = 'EVENT'
            ORDER BY ordinal
        """)

        crfs = await prisma.query_raw("""
            SELECT code, name, parent_code, ordinal
            FROM meta_study_structure
            WHERE type = 'CRF'
            ORDER BY ordinal
        """)

        structure = []
        for e in events:
            e_code = e['code']
            children = [c for c in crfs if c['parent_code'] == e_code]
            structure.append({
                "event_code": e_code,
                "event_name": e['name'],
                "crfs": children
            })

        return structure
    except Exception as e:
        print(f"Error fetching structure: {e}")
        return []


@app.get("/api/crf/{patient_id}/{event_code}/{crf_code}")
async def get_crf_details(patient_id: str, event_code: str, crf_code: str):
    """获取动态表的具体数据"""
    table_name = f"crf_{event_code.lower()}_{crf_code.lower()}"

    dictionary = await prisma.query_raw("""
        SELECT column_name, display_label, ordinal
        FROM system_data_dictionary
        WHERE table_name = ?
        ORDER BY ordinal
    """, table_name)

    if not dictionary:
        return {"error": "未找到该表定义", "fields": []}

    try:
        data_rows = await prisma.query_raw(f"SELECT * FROM `{table_name}` WHERE patient_id = ?", patient_id)
    except Exception:
        return {"error": "该表尚未生成或无数据", "fields": []}

    row = data_rows[0] if data_rows else {}

    result_list = []
    for item in dictionary:
        col_key = item['column_name']
        label = item['display_label']
        value = row.get(col_key, None)
        result_list.append({"key": col_key, "label": label, "value": value})

    return {"table_name": table_name, "fields": result_list}


# -------------------------------------------
# 3. 用户个人中心 API
# -------------------------------------------

@app.put("/api/user/{user_id}/profile")
async def update_user_profile(user_id: str, profile: UserProfileUpdate):
    data = {}

    if profile.name is not None:
        data["name"] = profile.name
    if profile.gender is not None:
        data["gender"] = profile.gender
    if profile.birthday is not None:
        try:
            clean_date = profile.birthday.replace("Z", "")
            data["birthday"] = datetime.fromisoformat(clean_date)
        except Exception as e:
            print(f"Date parse error: {e}")

    if not data:
        return {"success": False, "error": "没有提交任何更改"}

    try:
        updated = await prisma.user.update(where={"id": user_id}, data=data)
        return {"success": True, "user": updated}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/user/{user_id}")
async def get_user_profile(user_id: str):
    user = await prisma.user.find_unique(where={"id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# -------------------------------------------
# 4. 患者管理 CRUD API (写操作)
# -------------------------------------------

@app.post("/api/patients")
async def create_patient(patient: PatientCreate):
    new_id = str(uuid.uuid4())
    try:
        # 1. 先插入 patients 主表
        await prisma.execute_raw(
            """INSERT INTO patients (id, subject_label, protocol_id)
               VALUES (?, ?, ?)""",
            new_id, patient.subject_label, patient.protocol_id
        )

        # 2. 自动初始化所有 CRF 子表（保证 subsequen update 有行可更新）
        try:
            # 查出所有定义的 CRF
            crfs = await prisma.query_raw("""
                SELECT code, parent_code
                FROM meta_study_structure
                WHERE type = 'CRF'
            """)
            
            for row in crfs:
                event_val = row.get('parent_code')
                crf_val = row.get('code')
                if not event_val or not crf_val:
                    continue
                
                # 拼表名：crf_{event_code}_{crf_code}
                table_name = f"crf_{event_val.lower()}_{crf_val.lower()}"
                
                # 尝试插入空行
                try:
                    await prisma.execute_raw(f"INSERT INTO `{table_name}` (patient_id) VALUES (?)", new_id)
                except Exception as inner_e:
                    # 可能表不存在，或者已经有数据(极少见)，忽略错误继续下一个
                    print(f"Init table {table_name} failed: {inner_e}")

        except Exception as e:
            print(f"Failed to fetch structure or init tables: {e}")
            # 注意：这里虽然子表初始化失败，但主表已经插入成功，
            # 也可以选择回滚，但简单起见先返回成功，只需日志记录。

        return {"success": True, "id": new_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.delete("/api/patients/{patient_id}")
async def delete_patient(patient_id: str):
    try:
        await prisma.execute_raw("DELETE FROM patients WHERE id = ?", patient_id)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.put("/api/patients/{patient_id}")
async def update_patient(patient_id: str, patient: PatientCreate):
    try:
        await prisma.execute_raw(
            "UPDATE patients SET subject_label = ?, protocol_id = ? WHERE id = ?",
            patient.subject_label, patient.protocol_id, patient_id
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# -------------------------------------------
# 5. CRF 数据保存 API (写操作)
# -------------------------------------------

@app.post("/api/crf/save/{patient_id}")
async def save_crf_data(patient_id: str, payload: CrfDataUpdate):
    if not payload.table_name.startswith("crf_"):
        return {"success": False, "error": "非法表名"}

    cols = []
    vals = []

    for key, val in payload.data.items():
        if not key.replace("_", "").isalnum():
            continue
        cols.append(f"`{key}` = ?")
        vals.append(val)

    if not cols:
        return {"success": False, "error": "没有数据提交"}

    vals.append(patient_id)
    sql = f"UPDATE `{payload.table_name}` SET {', '.join(cols)} WHERE patient_id = ?"

    try:
        await prisma.execute_raw(sql, *vals)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
