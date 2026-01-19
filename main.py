import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from prisma import Prisma
from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime

app = FastAPI()
prisma = Prisma()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["http://localhost:3000"],
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

# 1. 用户信息更新模型
class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    birthday: Optional[str] = None  # 前端传 ISO 字符串


# 2. 患者创建/更新模型
class PatientCreate(BaseModel):
    subject_label: str
    protocol_id: str


# 3. CRF 数据提交模型
class CrfDataUpdate(BaseModel):
    table_name: str
    data: Dict[str, Any]


# -------------------------------------------
# 1. 管理员端 API (含统计增强)
# -------------------------------------------
@app.get("/api/admin/stats")
async def get_admin_stats():
    """获取系统统计数据 (含性别、年龄分布)"""

    # 1. 基础计数
    user_count = await prisma.user.count()
    try:
        # 统计患者总数
        patients = await prisma.query_raw("SELECT COUNT(*) as count FROM patients")
        patient_count = patients[0]['count']
    except:
        patient_count = 0

    # 2. 用户性别统计 (针对 User 表)
    gender_stats = []
    try:
        # SQL 聚合查询性别
        raw_genders = await prisma.query_raw("""
                                             SELECT gender, COUNT(*) as count
                                             FROM user
                                             WHERE gender IS NOT NULL
                                             GROUP BY gender
                                             """)

        # 简单的汉化映射
        gender_map = {'male': '男', 'female': '女', 'Male': '男', 'Female': '女'}

        for g in raw_genders:
            origin_val = g['gender']
            label = gender_map.get(origin_val, origin_val)  # 如果字典里没有，就用原值
            gender_stats.append({"name": label, "value": int(g['count'])})

    except Exception as e:
        print(f"Gender stats error: {e}")

    # 3. 用户出生年份统计 (针对 User 表)
    year_stats = []
    try:
        # SQL 提取年份并聚合
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
# 2. 用户端/医生端 API (临床业务 - 读取)
# -------------------------------------------

# A. 获取患者列表
@app.get("/api/patients")
async def get_patients():
    """获取所有患者列表"""
    try:
        # 直接查询原生表
        sql = """
              SELECT id, \
                     subject_label, \
                     protocol_id,
                     DATE_FORMAT(created_at, '%Y-%m-%d') as created_at
              FROM patients
              ORDER BY created_at DESC \
              """
        data = await prisma.query_raw(sql)
        return data
    except Exception as e:
        print(f"Error fetching patients: {e}")
        return []


# B. 获取左侧目录结构 (Events -> CRFs)
@app.get("/api/structure")
async def get_clinical_structure():
    """获取由 Event 和 CRF 组成的树状结构"""
    try:
        # 1. 获取所有 Event
        events = await prisma.query_raw("""
                                        SELECT code, name, ordinal
                                        FROM meta_study_structure
                                        WHERE type = 'EVENT'
                                        ORDER BY ordinal
                                        """)

        # 2. 获取所有 CRF
        crfs = await prisma.query_raw("""
                                      SELECT code, name, parent_code, ordinal
                                      FROM meta_study_structure
                                      WHERE type = 'CRF'
                                      ORDER BY ordinal
                                      """)

        # 3. 组装树结构
        structure = []
        for e in events:
            e_code = e['code']
            # 找到属于该 Event 的 CRF
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


# C. 获取具体 CRF 数据
@app.get("/api/crf/{patient_id}/{event_code}/{crf_code}")
async def get_crf_details(patient_id: str, event_code: str, crf_code: str):
    """
    获取动态表的具体数据
    """
    table_name = f"crf_{event_code.lower()}_{crf_code.lower()}"

    # 1. 获取字典定义 (字段名 -> 显示名)
    dictionary = await prisma.query_raw("""
                                        SELECT column_name, display_label, ordinal
                                        FROM system_data_dictionary
                                        WHERE table_name = ?
                                        ORDER BY ordinal
                                        """, table_name)

    if not dictionary:
        return {"error": "未找到该表定义", "fields": []}

    # 2. 获取业务数据
    try:
        data_rows = await prisma.query_raw(f"""
            SELECT * FROM `{table_name}` WHERE patient_id = ?
        """, patient_id)
    except Exception as e:
        # 表可能不存在
        return {"error": "该表尚未生成或无数据", "fields": []}

    row = data_rows[0] if data_rows else {}

    # 3. 组装结果
    result_list = []
    for item in dictionary:
        col_key = item['column_name']
        label = item['display_label']
        value = row.get(col_key, None)

        result_list.append({
            "key": col_key,
            "label": label,
            "value": value
        })

    return {
        "table_name": table_name,
        "fields": result_list
    }


# -------------------------------------------
# 3. 用户个人中心 API
# -------------------------------------------

# 更新用户个人信息
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
            pass

    if not data:
        return {"success": False, "error": "没有提交任何更改"}

    try:
        updated = await prisma.user.update(
            where={"id": user_id},
            data=data
        )
        return {"success": True, "user": updated}
    except Exception as e:
        return {"success": False, "error": str(e)}


# 获取单个用户信息
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
    """创建新患者"""
    new_id = str(uuid.uuid4())  # 生成 UUID
    try:
        await prisma.execute_raw(
            """INSERT INTO patients (id, subject_label, protocol_id)
               VALUES (?, ?, ?)""",
            new_id, patient.subject_label, patient.protocol_id
        )
        return {"success": True, "id": new_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.delete("/api/patients/{patient_id}")
async def delete_patient(patient_id: str):
    """删除患者"""
    try:
        # 假设数据库已配置 ON DELETE CASCADE，否则需先删子表
        await prisma.execute_raw("DELETE FROM patients WHERE id = ?", patient_id)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.put("/api/patients/{patient_id}")
async def update_patient(patient_id: str, patient: PatientCreate):
    """修改患者基本信息"""
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
    """保存或更新 CRF 数据"""
    # 简单的安全校验
    if not payload.table_name.startswith("crf_"):
        return {"success": False, "error": "非法表名"}

    # 动态构建 SQL
    cols = []
    vals = []

    for key, val in payload.data.items():
        # 过滤 key，防止 SQL 注入
        if not key.replace("_", "").isalnum():
            continue

        cols.append(f"`{key}` = ?")
        vals.append(val)

    if not cols:
        return {"success": False, "error": "没有数据提交"}

    # 追加 WHERE 条件的参数
    vals.append(patient_id)

    # 逻辑：更新该患者在该表的数据
    # 前提：import_data.py 已经为每个患者预生成了空行
    sql = f"UPDATE `{payload.table_name}` SET {', '.join(cols)} WHERE patient_id = ?"

    try:
        await prisma.execute_raw(sql, *vals)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}