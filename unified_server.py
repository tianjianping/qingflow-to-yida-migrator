# -*- coding: utf-8 -*-
"""轻流 -> 宜搭 统一迁移控制台（Flask 后端）

合并 app.py（数据迁移 / 四阶段管线）与 migration_server.py（附件迁移），
前端提供表单中心式布局：左侧选应用/表单，右侧数据迁移 + 附件迁移面板 + 共享控制台。

启动:
  python unified_server.py                  # 默认 http://127.0.0.1:8766
  python unified_server.py --port 9000      # 指定端口
"""
import csv, json, os, sys, time, uuid, hashlib
import threading, subprocess, traceback
import urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import OrderedDict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from flask import Flask, request, jsonify, send_from_directory

# ---------- 路径 ----------
BASE = Path(__file__).resolve().parent
SCRIPTS = BASE / "scripts"
WEB = BASE / "web"
DATA = BASE / "data"
CONFIG = BASE / "config"
MAPPINGS = BASE / "mappings"
REGISTRY = CONFIG / "表单对照表.csv"
SETTINGS_PATH = CONFIG / "settings.json"
LOGS = DATA / "logs"
# 子进程解释器：优先取启动本服务的解释器（保证与主进程环境一致）；
# 可通过环境变量 MIGRATION_PY 覆盖（如指定其他 Python）。
PY = os.environ.get("MIGRATION_PY", sys.executable)

LOGS.mkdir(parents=True, exist_ok=True)

# ---------- 日志保留策略（P2-3） ----------
LOG_KEEP_DAYS = 14      # 超过该天数的日志文件删除
LOG_KEEP_MAX = 100      # 无论天数，最多保留最近 N 个日志文件


def prune_logs(keep_days=LOG_KEEP_DAYS, keep_max=LOG_KEEP_MAX):
    """清理 data/logs 下的历史日志：先按天数淘汰，再按数量上限保留最近的。
    返回删除的文件数。任何单文件删除失败都忽略（可能正被占用）。"""
    try:
        files = sorted((p for p in LOGS.glob("*.log") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for idx, p in enumerate(files):
        try:
            too_old = p.stat().st_mtime < cutoff
            over_cap = idx >= keep_max
            if too_old or over_cap:
                p.unlink()
                removed += 1
        except Exception:
            pass
    return removed

# 注入 scripts 目录以导入 common
sys.path.insert(0, str(SCRIPTS))
from common import (load_credentials, load_form_config, http_request,
                    get_dingtalk_token, yida_context, load_json, save_json,
                    load_attachment_config, file_md5, load_content_index,
                    save_content_index, DATA_DIR, DINGTALK_API, BASE_DIR,
                    save_credentials, credential_summary, list_yida_forms,
                    list_qingflow_apps, CREDENTIAL_FIELDS,
                    raw_cache_fresh, fetch_qingflow_records_by_ids,
                    iter_json_array, load_raw_stats, save_raw_stats)

# ================================================================
#  常量定义
# ================================================================
INSERT_UPDATE_URL = f"{DINGTALK_API}/v2.0/yida/forms/instances/insertOrUpdate"
DEDUP_QUE_ID = "-17"
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".doc", ".docx", ".png", ".jpg",
               ".jpeg", ".zip", ".csv", ".txt", ".ppt", ".pptx", ".rar",
               ".7z", ".bmp", ".gif"}
MIN_EXPIRE_SEC = 120

STEP_DEFS = {
    "00":  {"script": "00_gen_form_configs.py", "desc": "根据对照表生成/同步表单配置", "per_form": False},
    "01":  {"script": "01_fetch_qingflow.py",    "desc": "分页拉取轻流表单数据", "per_form": True},
    "02":  {"script": "02_fetch_yida_schema.py", "desc": "拉取宜搭表单组件定义并生成映射草稿", "per_form": True},
    "02b": {"script": "02b_automap.py",          "desc": "按字段名自动对齐，生成正式映射表", "per_form": True, "support_force": True},
    "02c": {"script": "02c_fetch_yida_instances.py", "desc": "拉取宜搭真实存量，作为存在性权威", "per_form": True},
    "02d": {"script": "02d_compare.py",           "desc": "三方对账，产出差异清单", "per_form": True, "support_force": True},
    "03":  {"script": "03_transform.py",          "desc": "将差异集按映射表转为宜搭裸值格式", "per_form": True},
    "04":  {"script": "04_batch_create.py",       "desc": "按差异清单写入宜搭", "per_form": True, "support_commit": True},
}
STEP_ORDER = ["00", "01", "02", "02b", "02c", "02d", "03", "04"]

STAGES = {
    "s1": {"title": "阶段一 · 拉取",   "steps": ["00", "01", "02", "02b", "02c"],
           "desc": "轻流数据 + 宜搭组件与字段对齐 + 宜搭存量"},
    "s2": {"title": "阶段二 · 对比",   "steps": ["02d"],
           "desc": "三方对账，产出差异清单（待新建/待更新/跳过/源已删除）"},
    "s3": {"title": "阶段三 · 格式化", "steps": ["03"],
           "desc": "只转换差异集为宜搭裸值格式"},
    "s4": {"title": "阶段四 · 写入",   "steps": ["04"],
           "desc": "新建 batchSave / 更新 insertOrUpdate"},
}
STAGE_ORDER = ["s1", "s2", "s3", "s4"]

# ================================================================
#  全局状态
# ================================================================
data_jobs = {}
data_jobs_lock = threading.Lock()
_data_job_counter = 0
_data_job_counter_lock = threading.Lock()

# 附件任务
class AttJob:
    def __init__(self, job_id, form_name, mode, limit=0, commit=False):
        self.id = job_id
        self.form = form_name
        self.mode = mode
        self.limit = limit
        self.commit = commit
        self.status = "pending"
        self.events = []
        self.stats = {}
        self.result = None
        self._cancel = False
        self._thread = None
        self._emit_lock = threading.Lock()

    def emit(self, etype, text, data=None):
        # C2: 附件任务并发后多条记录并行调用 emit，事件追加需加锁
        ev = {"type": etype, "time": time.time(), "text": text}
        if data is not None:
            ev["data"] = data
        with self._emit_lock:
            self.events.append(ev)
            if len(self.events) > 2000:
                self.events = self.events[-1000:]

    def info(self, text, data=None): self.emit("info", text, data)
    def success(self, text, data=None): self.emit("success", text, data)
    def warn(self, text, data=None): self.emit("warn", text, data)
    def error(self, text, data=None): self.emit("error", text, data)
    def cancel(self): self._cancel = True

    @property
    def cancelled(self): return self._cancel

att_jobs = OrderedDict()
att_jobs_max = 20

# ================================================================
#  工具函数
# ================================================================
def sanitize_filename(name):
    name = name.strip()
    for ch in '<>:"/\\|?*\r\n\t':
        name = name.replace(ch, "_")
    if len(name) > 200:
        base_n, ext = os.path.splitext(name)
        name = base_n[:180] + ext
    return name if name else "unnamed"

def check_expire(url):
    try:
        q = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(q)
        exp_vals = params.get("qingflow-expire-time")
        if exp_vals:
            return int(exp_vals[0]) - int(time.time())
    except Exception:
        pass
    return None

def get_scalar(answer):
    if answer.get("value") not in (None, ""):
        return answer["value"]
    vals = answer.get("values") or []
    for v in vals:
        if isinstance(v, dict):
            if v.get("value") not in (None, ""):
                return v["value"]
            if v.get("dataValue") not in (None, ""):
                return v["dataValue"]
    return None

def _to_int(v, default=0):
    try: return int(v)
    except (TypeError, ValueError): return default

def _safe_len(path, form_name=None):
    """安全获取 JSON 数组长度。

    B1: form_name 提供时，raw 文件优先读统计快照（data/raw/<form>_stats.json），
    命中直接返回 count，避免每次全量解析 390MB；快照缺失/过期才回退解析。"""
    if form_name is not None:
        snap = load_raw_stats(form_name)
        if snap is not None and snap.get("count") is not None:
            return snap["count"]
    try: return len(json.loads(path.read_text(encoding="utf-8")))
    except Exception: return None

def _file_fp_simple(p):
    """文件轻量指纹 [mtime_ns, size]；不存在/异常返回 None。JSON 序列化安全（list）。"""
    try:
        st = p.stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None

# ================================================================
#  对照表读写（来自 app.py）
# ================================================================
def read_registry():
    if not REGISTRY.exists():
        return []
    with open(REGISTRY, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":
        return []
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(REGISTRY, encoding=enc, newline="") as f:
                return [{k.strip(): (v or "").strip() for k, v in r.items()}
                        for r in csv.DictReader(f)]
        except UnicodeDecodeError:
            continue
    return []

def add_form_to_registry(name, app_key, form_uuid, note, app_id=0):
    name = (name or "").strip()
    if not name: return False, "表单名不能为空"
    if not app_key: return False, "轻流 appKey 不能为空"
    rows = read_registry()
    if any(r.get("表单名") == name for r in rows):
        return False, "已存在同名表单"
    header = ["表单名", "轻流appKey", "宜搭formUuid", "启用", "备注", "宜搭应用"]
    norm = []
    for r in rows:
        row = {k: (r.get(k) or "").strip() for k in header}
        if not row.get("宜搭应用"): row["宜搭应用"] = "0"
        norm.append(row)
    norm.append({
        "表单名": name, "轻流appKey": app_key,
        "宜搭formUuid": (form_uuid or "").strip(),
        "启用": "Y", "备注": (note or "").strip(),
        "宜搭应用": str(int(app_id or 0)),
    })
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader(); w.writerows(norm)
    return True, "已添加"

def update_form_in_registry(old_name, name, app_key, form_uuid, note, app_id=0):
    old_name = (old_name or "").strip()
    name = (name or "").strip()
    if not old_name: return False, "原表单名不能为空"
    if not name: return False, "表单名不能为空"
    if not app_key: return False, "轻流 appKey 不能为空"
    rows = read_registry()
    if not any(r.get("表单名") == old_name for r in rows):
        return False, "表单不存在: " + old_name
    if name != old_name and any(r.get("表单名") == name for r in rows):
        return False, "已存在同名表单"
    header = ["表单名", "轻流appKey", "宜搭formUuid", "启用", "备注", "宜搭应用"]
    norm = []
    for r in rows:
        row = {k: (r.get(k) or "").strip() for k in header}
        if not row.get("宜搭应用"): row["宜搭应用"] = "0"
        if row.get("表单名") == old_name:
            row = {
                "表单名": name, "轻流appKey": app_key,
                "宜搭formUuid": (form_uuid or "").strip(),
                "启用": row.get("启用") or "Y",
                "备注": (note or "").strip(),
                "宜搭应用": str(int(app_id or 0)),
            }
        norm.append(row)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader(); w.writerows(norm)
    return True, "已更新"

def delete_form_from_registry(name):
    name = (name or "").strip()
    if not name: return False, "表单名不能为空"
    rows = read_registry()
    if not any(r.get("表单名") == name for r in rows):
        return False, "表单不存在: " + name
    header = ["表单名", "轻流appKey", "宜搭formUuid", "启用", "备注", "宜搭应用"]
    norm = []
    for r in rows:
        if r.get("表单名") == name: continue
        row = {k: (r.get(k) or "").strip() for k in header}
        if not row.get("宜搭应用"): row["宜搭应用"] = "0"
        norm.append(row)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader(); w.writerows(norm)
    cfg = CONFIG / "forms" / f"{name}.json"
    if cfg.exists():
        try: cfg.unlink()
        except Exception: pass
    return True, "已删除"

# ================================================================
#  表单状态 + 附件信息
# ================================================================
def parse_attachment_mapping(form_name):
    """解析映射表，返回 (att_que_ids, ded_cid, ded_cname)"""
    att_ids = []
    mp = MAPPINGS / f"{form_name}_mapping.csv"
    if mp.exists():
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                cn = row.get("componentName", "")
                qid = row.get("轻流queId", "").strip()
                if cn == "AttachmentField" and qid and qid != "0":
                    att_ids.append(qid)
    ded_cid = None
    ded_cname = "TextField"
    if mp.exists():
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row.get("轻流queId", "").strip() == DEDUP_QUE_ID:
                    ded_cid = row["componentId"]
                    ded_cname = row.get("componentName") or "TextField"
                    break
    return att_ids, ded_cid, ded_cname

_att_stats_cache = {}          # form -> (cache_key, expire_ts, result)
_att_stats_cache_lock = threading.Lock()
ATT_STATS_TTL = 300            # 秒；进程内统计缓存 TTL（B1: 60s 过短，频繁击穿触发全量解析）


def attachment_stats(form_name, refresh=False):
    """快速统计附件数据（不加载全量 raw JSON 进行深度分析，仅统计量级）

    P2-4: 每次刷新表单列表都重新解析几十 MB 的 raw JSON 代价过高。
    这里按 (文件 mtime, size) + 60s TTL 做缓存：raw 未变且未超时直接复用。
    refresh=True 强制绕过缓存重新统计（数据准备完成、宜搭存量更新后调用）。"""
    raw_path = DATA / "raw" / f"{form_name}_raw.json"
    fresh, expire_ts, raw_age = raw_cache_fresh(form_name)
    fresh_info = {
        "rawFresh": fresh,
        "rawExpireAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expire_ts)) if expire_ts else "",
        "rawAgeSec": int(raw_age) if raw_age is not None else None,
    }
    if not raw_path.exists():
        r = {"hasAttachment": False, "attFields": 0, "totalFiles": 0, "expiredUrls": 0}
        r.update(fresh_info)
        return r
    att_que_ids, _, _ = parse_attachment_mapping(form_name)
    if not att_que_ids:
        r = {"hasAttachment": False, "attFields": 0, "totalFiles": 0, "expiredUrls": 0}
        r.update(fresh_info)
        return r
    raw_fp = _file_fp_simple(raw_path)
    yida_fp = _file_fp_simple(DATA / "raw" / f"{form_name}_yida_instances.json")

    if not refresh:
        # 1) 文件快照（跨进程持久，raw/yida 未变则命中）
        snap = load_raw_stats(form_name)
        if snap is not None and snap.get("attQueIds") == att_que_ids \
                and snap.get("yidaFingerprint") == yida_fp:
            snap.pop("rawFingerprint", None)
            snap.pop("yidaFingerprint", None)
            snap.update(fresh_info)
            return snap
        # 2) 进程内 TTL 缓存
        key = (raw_fp, yida_fp, tuple(att_que_ids))
        with _att_stats_cache_lock:
            hit = _att_stats_cache.get(form_name)
        if hit and hit[0] == key and hit[1] > time.time():
            return hit[2]
    # 重算：流式遍历 raw（内存 O(单条)），异常时回退全量解析
    try:
        records_iter = iter_json_array(raw_path)
    except Exception:
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            records_iter = iter(raw if isinstance(raw, list) else raw.get("result", {}).get("result", []))
        except Exception:
            r = {"hasAttachment": True, "attFields": len(att_que_ids), "totalFiles": 0, "expiredUrls": 0}
            r.update(fresh_info)
            return r
    # 宜搭已有数据维度（用于附件迁移范围提示）：数据ID 命中宜搭存量即视为「宜搭已有」
    did_set = set()
    yida_path = DATA_DIR / "raw" / f"{form_name}_yida_instances.json"
    if yida_path.exists():
        try:
            yj = json.loads(yida_path.read_text(encoding="utf-8"))
            did_map = yj.get("didToInst") or {d: i for i, d in (yj.get("existing") or {}).items() if d}
            did_set = set(did_map)
        except Exception:
            did_set = set()
    has_att = 0; total_files = 0; expired = 0; yida_records = 0
    scope_att_records = 0; scope_att_files = 0
    migrated_records = 0; migrated_files = 0
    ledger = load_att_ledger(form_name)
    for rec in records_iter:
        answers = {str(a.get("queId")): a for a in rec.get("answers", [])}
        did = get_scalar(answers.get(DEDUP_QUE_ID, {}))
        in_scope = True
        if did_set:
            if did and did in did_set: yida_records += 1
            in_scope = bool(did and did in did_set)
        found = False
        rec_files = 0
        for aq in att_que_ids:
            ans = answers.get(aq)
            if not ans: continue
            vals = ans.get("values") or []
            if vals:
                if not found: has_att += 1; found = True
                rec_files += len(vals)
                total_files += len(vals)
                for v in vals:
                    url = v.get("value") or v.get("dataValue") or ""
                    if url:
                        exp = check_expire(url)
                        if exp is not None and exp < MIN_EXPIRE_SEC:
                            expired += 1
        if found and in_scope:
            scope_att_records += 1
            scope_att_files += rec_files
            m = ledger.get(did) if did else None
            if m and m["attachments"] >= rec_files:
                migrated_records += 1
                migrated_files += rec_files
    stats = {
        "hasAttachment": True,
        "attFields": len(att_que_ids),
        "attQueIds": att_que_ids,
        "hasAttRecords": has_att,
        "yidaRecords": yida_records if did_set else None,
        "totalFiles": total_files,
        "expiredUrls": expired,
        "migratedRecords": migrated_records,
        "migratedFiles": migrated_files,
        "pendingRecords": scope_att_records - migrated_records,
        "pendingFiles": scope_att_files - migrated_files,
    }
    stats.update(fresh_info)
    # 落盘快照：供跨进程/下次请求复用（指纹不匹配时自动失效重算）
    try:
        snap = dict(stats)
        snap["rawFingerprint"] = raw_fp
        snap["yidaFingerprint"] = yida_fp
        save_raw_stats(form_name, snap)
    except Exception:
        pass
    with _att_stats_cache_lock:
        _att_stats_cache[form_name] = ((raw_fp, yida_fp, tuple(att_que_ids)),
                                       time.time() + ATT_STATS_TTL, stats)
    return stats

def check_diff_fresh(form_name):
    """防重复写入校验：04 写入前检查差异清单是否「新鲜」。
    规则：diff.json 必须生成于 result.json（上次写入台账）之后，
    否则复用的旧清单会重复 batchSave 已写入记录。
    返回 (ok: bool, msg: str)。"""
    diff_p = DATA / "diff" / f"{form_name}_diff.json"
    result_p = DATA / "result" / f"{form_name}_result.json"
    if not diff_p.exists():
        return False, "差异清单不存在，请先运行「数据准备」生成差异清单(02d)"
    if not result_p.exists():
        return True, ""
    try:
        dm = diff_p.stat().st_mtime
        rm = result_p.stat().st_mtime
    except OSError:
        return True, ""
    if dm < rm - 1:  # 1s 容差
        return False, (f"差异清单({diff_p.name})早于上次写入台账，直接执行会重复创建已迁移记录。"
                       f"请先重新运行「数据准备」（02c 会重新核对宜搭存量，差异清单将只保留真正需要新建/更新的记录）。")
    return True, ""


def form_status_light(name):
    """轻量级表单状态：仅检查文件是否存在，不读取文件内容。
    用于表单列表加载，避免对每个表单读取 4-5 个 JSON 文件。"""
    info = {"name": name}
    cfg_path = CONFIG / "forms" / f"{name}.json"
    info["configExists"] = cfg_path.exists()
    raw = DATA / "raw" / f"{name}_raw.json"
    info["rawExists"] = raw.exists()
    info["rawCount"] = None
    schema = DATA / "raw" / f"{name}_宜搭组件.json"
    info["schemaExists"] = schema.exists()
    mp = MAPPINGS / f"{name}_mapping.csv"
    info["mappingExists"] = mp.exists()
    tf = DATA / "transformed" / f"{name}_formdata.json"
    info["transformedExists"] = tf.exists()
    info["transformedCount"] = None
    res = DATA / "result" / f"{name}_result.json"
    info["resultExists"] = res.exists()
    info["resultDone"] = 0; info["resultFailed"] = 0
    ym = DATA / "raw" / f"{name}_yida_instances.json"
    info["yidaExists"] = ym.exists()
    info["yidaCount"] = 0
    df = DATA / "diff" / f"{name}_diff.json"
    info["diffExists"] = df.exists()
    info["diffFresh"] = False
    info["diffCreate"] = info["diffUpdate"] = info["diffSkip"] = 0
    info["diffTime"] = ""
    info["hasAttachment"] = False
    info["attFields"] = 0
    info["totalFiles"] = 0
    info["expiredUrls"] = 0
    return info

def form_status(name):
    info = {"name": name}
    cfg_path = CONFIG / "forms" / f"{name}.json"
    info["configExists"] = cfg_path.exists()
    raw = DATA / "raw" / f"{name}_raw.json"
    info["rawExists"] = raw.exists()
    info["rawCount"] = _safe_len(raw, name)
    schema = DATA / "raw" / f"{name}_宜搭组件.json"
    info["schemaExists"] = schema.exists()
    mp = MAPPINGS / f"{name}_mapping.csv"
    info["mappingExists"] = mp.exists()
    tf = DATA / "transformed" / f"{name}_formdata.json"
    info["transformedExists"] = tf.exists()
    info["transformedCount"] = _safe_len(tf)
    res = DATA / "result" / f"{name}_result.json"
    info["resultExists"] = res.exists()
    info["resultDone"] = 0; info["resultFailed"] = 0
    if res.exists():
        try:
            d = json.loads(res.read_text(encoding="utf-8"))
            info["resultDone"] = len(d.get("done", {}))
            info["resultFailed"] = len(d.get("failed", {}))
        except Exception: pass
    ym = DATA / "raw" / f"{name}_yida_instances.json"
    info["yidaExists"] = ym.exists()
    info["yidaCount"] = 0
    if ym.exists():
        try:
            d = json.loads(ym.read_text(encoding="utf-8"))
            info["yidaCount"] = d.get("count", 0)
        except Exception: pass
    df = DATA / "diff" / f"{name}_diff.json"
    info["diffExists"] = df.exists()
    info["diffFresh"] = False
    info["diffCreate"] = info["diffUpdate"] = info["diffSkip"] = 0
    if df.exists():
        try:
            d = json.loads(df.read_text(encoding="utf-8"))
            info["diffCreate"] = len(d.get("create", []))
            info["diffUpdate"] = len(d.get("update", []))
            info["diffSkip"] = len(d.get("skip", []))
            info["diffTime"] = d.get("generatedAt", "")
        except Exception: pass
    info["diffFresh"] = check_diff_fresh(name)[0]
    # 附件信息
    info.update(attachment_stats(name))
    return info

def list_forms():
    """列表用轻量级状态（仅文件存在性），不读取 JSON 内容。"""
    rows = read_registry()
    out = []
    for r in rows:
        name = r.get("表单名")
        if not name: continue
        s = form_status_light(name)
        s.update({
            "appKey": r.get("轻流appKey", ""),
            "formUuid": r.get("宜搭formUuid", ""),
            "enabled": r.get("启用", "Y"),
            "note": r.get("备注", ""),
            "appId": _to_int(r.get("宜搭应用"), 0),
        })
        out.append(s)
    return out

def get_form_detail(name):
    """单表单详情：完整状态（含计数、附件统计等），选中表单时按需调用。"""
    rows = read_registry()
    reg = next((r for r in rows if r.get("表单名") == name), {})
    s = form_status(name)
    s.update({
        "appKey": reg.get("轻流appKey", ""),
        "formUuid": reg.get("宜搭formUuid", ""),
        "enabled": reg.get("启用", "Y"),
        "note": reg.get("备注", ""),
        "appId": _to_int(reg.get("宜搭应用"), 0),
    })
    # 表单类型徽标：config 显式配置 > 本地缓存 > 接口探测；探测失败回落普通表单
    try:
        from form_type import detect_form_type, FORM_TYPE_LABEL
        ft, src = detect_form_type(name, verbose=False)
        s["formType"] = ft
        s["formTypeLabel"] = FORM_TYPE_LABEL.get(ft, ft)
        s["formTypeSource"] = src
    except Exception:
        s["formType"] = "normal"
        s["formTypeLabel"] = "普通表单"
        s["formTypeSource"] = "fallback"
    return s

# ================================================================
#  设置（来自 app.py）
# ================================================================
def load_settings():
    opts = {"limit": 0, "attLimit": 0}
    if SETTINGS_PATH.exists():
        try:
            d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if "limit" in d: opts["limit"] = d["limit"]
            if "attLimit" in d: opts["attLimit"] = d["attLimit"]
        except Exception: pass
    yidaApps, activeApp, userId = [], 0, ""
    cred_path = CONFIG / "credentials.json"
    if cred_path.exists():
        try:
            c = json.loads(cred_path.read_text(encoding="utf-8"))
            yidaApps = c.get("yidaApps", []) or []
            activeApp = c.get("activeApp", 0) or 0
            if not isinstance(yidaApps, list): yidaApps = []
            yida = (c.get("yida", {}) or {})
            userId = yida.get("userId", "")
            if not yidaApps and (yida.get("systemToken") or yida.get("appType")):
                yidaApps = [{"name": "默认应用", "appType": yida.get("appType", ""),
                             "systemToken": yida.get("systemToken", ""),
                             "commitDefault": False, "force": False}]
                activeApp = 0
            yidaApps = [dict(a, commitDefault=bool(a.get("commitDefault", False)),
                             force=bool(a.get("force", False))) for a in yidaApps]
            if activeApp >= len(yidaApps): activeApp = 0
        except Exception: pass
    opts["yidaApps"] = yidaApps
    opts["activeApp"] = activeApp
    opts["userId"] = userId
    return opts

def save_settings(data):
    cred_path = CONFIG / "credentials.json"
    cred = {}
    if cred_path.exists():
        try: cred = json.loads(cred_path.read_text(encoding="utf-8"))
        except Exception: cred = {}
    old_apps = cred.get("yidaApps", []) or []
    if not isinstance(old_apps, list): old_apps = []
    if "yidaApps" in data and isinstance(data["yidaApps"], list):
        clean_apps = []
        for i, a in enumerate(data["yidaApps"]):
            old_app = old_apps[i] if i < len(old_apps) else {}
            clean_apps.append({
                "name": str(a.get("name", "")).strip() or ("应用" + str(i + 1)),
                "appType": str(a.get("appType", "")).strip(),
                "systemToken": _keep_if_masked(a.get("systemToken", ""), old_app.get("systemToken", "")),
                "commitDefault": bool(a.get("commitDefault", False)),
                "force": bool(a.get("force", False)),
            })
    else:
        clean_apps = [dict(a, commitDefault=bool(a.get("commitDefault", False)),
                           force=bool(a.get("force", False))) for a in old_apps]
    if not clean_apps:
        y = cred.get("yida", {}) or {}
        clean_apps = [{"name": "默认应用", "appType": y.get("appType", ""),
                       "systemToken": y.get("systemToken", ""), "commitDefault": False, "force": False}]
    cred["yidaApps"] = clean_apps
    activeApp = int(data["activeApp"]) if "activeApp" in data else int(cred.get("activeApp", 0) or 0)
    if activeApp < 0 or activeApp >= len(clean_apps): activeApp = 0
    cred["activeApp"] = activeApp
    yida = dict(cred.get("yida", {}) or {})
    if "userId" in data: yida["userId"] = str(data["userId"] or "").strip()
    yida["systemToken"] = clean_apps[activeApp]["systemToken"]
    yida["appType"] = clean_apps[activeApp]["appType"]
    cred["yida"] = yida
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(cred_path, cred, quiet=True)
    opt = {"limit": 0}
    if SETTINGS_PATH.exists():
        try: opt = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception: pass
    if "limit" in data: opt["limit"] = int((data["limit"] or 0) or 0)
    if "attLimit" in data: opt["attLimit"] = int((data["attLimit"] or 0) or 0)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_json(SETTINGS_PATH, opt, quiet=True)
    return opt

def active_app_opts(settings):
    apps = settings.get("yidaApps") or []
    idx = settings.get("activeApp") or 0
    if not apps: return {"commitDefault": False, "force": False, "limit": settings.get("limit", 0) or 0}
    if idx < 0 or idx >= len(apps): idx = 0
    app = apps[idx]
    return {"commitDefault": bool(app.get("commitDefault", False)),
            "force": bool(app.get("force", False)),
            "limit": settings.get("limit", 0) or 0}

# ================================================================
#  数据迁移 Job 系统（来自 app.py）
# ================================================================
def build_cmd(step_key, form, commit=False, limit=None, force=False,
              skip_fetch=False, force_full=False, refresh_yida=False):
    script = STEP_DEFS[step_key]["script"]
    base = [PY, "-X", "utf8", str(SCRIPTS / script)]
    if step_key == "00": return base
    base.append(form)
    if step_key == "01":
        # A3: 显式声明增量意图；01 内部仍会自判（无镜像/无水印时回退全量并提示）。
        # force_full=True 时强制 --full 全量重拉（忽略增量水印）。
        base.append("--full" if force_full else "--incremental")
    if step_key == "02" and refresh_yida:
        # 强制刷新宜搭结构（忽略本地缓存），修改宜搭表单后必须开启才能识别新字段
        base.append("--force")
    if step_key in ("02b", "02d") and force: base.append("--force")
    if step_key == "04":
        if commit: base.append("--commit")
        if limit: base += ["--limit", str(limit)]
    return base

def build_stage_cmds(stage_key, form, commit=False, limit=None, force=False,
                     skip_fetch=False, force_full=False, refresh_yida=False):
    cmds = []
    for s in STAGES[stage_key]["steps"]:
        # 跳过轻流拉取开关：01 直接不执行，复用本地镜像（调试时轻流数据未变化）
        if s == "01" and skip_fetch:
            continue
        c = build_cmd(s, form, commit=(s == "04") and commit,
                      limit=(s == "04") and limit or None, force=(s == "02d") and force,
                      skip_fetch=skip_fetch, force_full=force_full, refresh_yida=refresh_yida)
        cmds.append((s, c))
    return cmds

def build_prepare_cmds(form, mode="all", skip_fetch=False, force_full=False,
                       refresh_yida=False):
    """数据准备命令（拉取/格式化解耦）。

    mode:
      - "all"      : 00+01+02+02b+02c+02d+03（默认，完整准备）
      - "fetch"    : 00+01+02+02c+02d（仅拉取数据+三方对账，不改映射、不转换）
      - "transform": 02b+03（仅字段对齐+格式化，复用已拉取的 raw/映射产物）
    """
    if mode == "fetch":
        steps = ["00", "01", "02", "02c", "02d"]
    elif mode == "transform":
        steps = ["02b", "03"]
    else:
        steps = ["00", "01", "02", "02b", "02c", "02d", "03"]
    cmds = []
    for s in steps:
        if s == "01" and skip_fetch:
            print(f"[开关] --skip-fetch: 跳过「01 拉取轻流数据」，复用本地镜像")
            continue
        cmds.append((s, build_cmd(s, form, skip_fetch=skip_fetch,
                                  force_full=force_full, refresh_yida=refresh_yida)))
    return cmds

def _next_data_job_id():
    global _data_job_counter
    with _data_job_counter_lock:
        _data_job_counter += 1
        return f"data_{int(time.time()*1000)}_{_data_job_counter}"

DATA_JOB_OUTPUT_MAX_LINES = 800   # P1-3: 内存中最多保留的输出行数（完整内容在日志文件）


def _job_emit(job, text):
    """写入 job 输出环形缓冲：超出上限时丢弃最早的行，避免大批量迁移时内存/传输膨胀。
    完整输出始终落在 data/logs/<form>_<jid>.log。"""
    buf = job["output"]
    buf.append(text)
    if len(buf) > DATA_JOB_OUTPUT_MAX_LINES:
        drop = len(buf) - DATA_JOB_OUTPUT_MAX_LINES
        del buf[:drop]
        job["truncated"] = job.get("truncated", 0) + drop


def start_data_job(form, cmds):
    jid = _next_data_job_id()
    job = {
        "id": jid, "type": "data", "form": form, "status": "running",
        "output": [], "truncated": 0, "returncode": None, "started": time.time(),
        "finished": None, "currentStep": cmds[0][0] if cmds else "",
        "logPath": None,
    }
    with data_jobs_lock:
        data_jobs[jid] = job
    threading.Thread(target=_data_worker, args=(job, cmds), daemon=True).start()
    return jid

def _data_worker(job, cmds):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = LOGS / f"{job['form']}_{job['id']}.log"
    job["logPath"] = str(log_path)
    overall_rc = 0
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            for step_key, cmd in cmds:
                job["currentStep"] = step_key
                header = f"\n{'='*60}\n> 步骤 {step_key}  {STEP_DEFS[step_key]['desc']}\n{'='*60}\n"
                _job_emit(job, header); lf.write(header)
                try:
                    proc = subprocess.Popen(
                        cmd, cwd=str(SCRIPTS), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1)
                except Exception as e:
                    msg = f"[后端异常] 无法启动进程: {e}\n"
                    _job_emit(job, msg); lf.write(msg)
                    overall_rc = -1; break
                for line in proc.stdout:
                    _job_emit(job, line); lf.write(line); lf.flush()
                rc = proc.wait()
                if rc != 0:
                    overall_rc = rc
                    fail_msg = f"\n[步骤 {step_key} 失败，退出码 {rc}]\n"
                    _job_emit(job, fail_msg); lf.write(fail_msg); break
    except Exception as e:
        _job_emit(job, f"[后端异常] {e}\n")
        overall_rc = -1
    job["returncode"] = overall_rc
    job["status"] = "failed" if overall_rc not in (0, None) else "success"
    job["finished"] = time.time()
    prune_logs()  # P2-3: 每次任务结束顺手清理历史日志

def get_data_job(jid):
    with data_jobs_lock:
        job = data_jobs.get(jid)
        if not job: return {"id": jid, "status": "not_found", "output": "", "returncode": None}
        dropped = job.get("truncated", 0)
        body = "".join(job["output"])
        if dropped:
            body = (f"[已省略前 {dropped} 行，完整日志见 {job.get('logPath') or '日志文件'}]\n") + body
        return {
            "id": jid, "type": "data", "form": job["form"],
            "step": job["currentStep"], "status": job["status"],
            "output": body, "truncated": dropped, "returncode": job["returncode"],
            "started": job["started"], "finished": job["finished"],
            "logPath": job.get("logPath"),
        }

# ================================================================
#  附件迁移 Job 系统（来自 migration_server.py）
# ================================================================
def load_att_ledger(form_name):
    """读附件写入台账，返回 {dataID: {"attachments": n, "urls": [...]}}（仅成功条目）。

    兼容旧格式：同一 dataID 多条 = 多个附件字段分别写入，文件数求和；
    新格式单条 merged 汇总条目直接采用。
    """
    path = DATA_DIR / "result" / f"{form_name}_attachment_result.json"
    ledger = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ledger
    items = data if isinstance(data, list) else []
    if not items and isinstance(data, dict):
        items = [{"dataID": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("success"):
            continue
        did = str(item.get("dataID") or "").strip()
        if not did:
            continue
        n = int(item.get("attachments") or 0)
        urls = list(item.get("urls") or [])
        if item.get("merged"):
            ledger[did] = {"attachments": n, "urls": urls}
        else:
            cur = ledger.get(did)
            if cur is None:
                cur = {"attachments": 0, "urls": []}
                ledger[did] = cur
            cur["attachments"] += n
            cur["urls"] = cur["urls"] + urls
    return ledger


def _flush_att_result(form_name, result_map):
    """增量落盘附件台账（dataID -> 汇总条目，原子写入，静默）。"""
    try:
        flat = []
        for did, item in result_map.items():
            if isinstance(item, list):
                flat.extend(item)
            else:
                flat.append(item)
        save_json(DATA_DIR / "result" / f"{form_name}_attachment_result.json",
                  flat, quiet=True)
    except Exception:
        pass


def vps_file_exists(endpoint, vps_rel, expect_size=None):
    """HEAD 预检：VPS 上已有同名(且同大小)文件则无需再上传，节省上行流量。
    返回 (exists: bool, url: str)。任何异常都返回 False（回退为正常上传）。"""
    if not endpoint:
        return False, ""
    url = f"{endpoint}/files/{urllib.parse.quote(vps_rel)}"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False, ""
            if expect_size is not None:
                remote_size = resp.headers.get("Content-Length")
                if remote_size is None or int(remote_size) != int(expect_size):
                    return False, ""
            return True, url
    except Exception:
        return False, ""


def download_stream(url, cache_path, timeout=60, chunk=64 * 1024):
    """流式下载到临时文件后原子改名，避免大文件全量读入内存 + 半截文件被当成缓存命中。"""
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + f".part{os.getpid()}"
    total = 0
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                total += len(buf)
        if total == 0:
            raise RuntimeError("empty response")
        os.replace(tmp, cache_path)
        return total
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def _ledger_url_stale(m):
    """台账条目的附件 URL 含轻流签名（expire 参数）且已过期 -> 陈旧，需强制重迁。

    D1: 新迁移写入的是 VPS 自建 URL（无 expire 参数，恒有效）；历史版本可能
    写入轻流签名 URL（约 7 天有效），这类记录若不刷新会永久失效，因此跳过
    逻辑必须放行它们（不跳过 = 进入重迁）。"""
    for u in (m.get("urls") or []):
        su = str(u)
        if "expire=" in su or "expires=" in su:
            exp = check_expire(su)
            if exp is not None and exp < MIN_EXPIRE_SEC:
                return True
    return False


def _writeback_mirror(form_name, refresh_records, job):
    """D2: 定向重拉获得的轻流记录写回 raw 镜像（按 applyId 覆盖对应条目）。

    流式读改写：不把整个 raw 加载进内存（与 B 阶段目标一致），只替换发生
    重拉的记录，临时文件 + os.replace 原子替换。返回更新条数。
    refresh_records 可能为空（无过期记录），此时直接返回 0。"""
    if not refresh_records:
        return 0
    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"
    if not raw_path.exists():
        return 0
    by_id = {str(r.get("applyId")): r for r in refresh_records}
    tmp_path = raw_path.with_name(raw_path.name + ".d2tmp")
    updated = 0
    try:
        # 优先流式；镜像结构异常时回退全量加载（低频收尾操作，可接受）
        try:
            rec_iter = iter_json_array(raw_path)
        except Exception:
            raw = load_json(raw_path)
            rec_iter = iter(raw if isinstance(raw, list) else raw.get("result", {}).get("result", []))
        with open(tmp_path, "w", encoding="utf-8") as out:
            out.write("[")
            first = True
            for rec in rec_iter:
                if not first:
                    out.write(",\n")
                first = False
                rid = str(rec.get("applyId"))
                if rid in by_id:
                    json.dump(by_id[rid], out, ensure_ascii=False)
                    updated += 1
                else:
                    json.dump(rec, out, ensure_ascii=False)
            out.write("\n]")
            out.flush()
            os.fsync(out.fileno())
        if tmp_path.exists():
            try:
                os.chmod(str(tmp_path), 0o666)
            except OSError:
                pass
        os.replace(str(tmp_path), str(raw_path))
    except Exception as e:
        job.warn(f"D2 镜像回写失败: {e}")
        try:
            if tmp_path.exists():
                os.remove(str(tmp_path))
        except OSError:
            pass
        return 0
    if updated:
        job.info(f"D2 已把 {updated} 条定向重拉记录回写镜像 {raw_path.name}")
    return updated


def _att_record_worker(job, rec, data_id, total_files, fields, shared):
    """并发处理单条记录（附件下载/上传/写入宜搭），返回主线程汇总的增量结果。

    C2: 原逐记录串行逻辑抽为独立 worker，由 ThreadPoolExecutor 并行执行。
    shared 为只读共享上下文（storage_cfg/ctx/cfg/去重键/附件映射/content_index
    快照/本地缓存目录/token）；本函数不直接修改全局 stats 与台账，全部以
    返回增量 (data_id, 台账条目, stats增量, content_index新条目) 汇总，
    避免多线程锁竞争。http_request 限速已在 common 内加锁，全局请求间隔不变。
    """
    delta = {k: 0 for k in ("downloaded", "cached", "errors", "content_hit", "vps_hit",
                            "uploaded", "written", "skipped_bad_url", "skipped_expired",
                            "refreshed_urls")}
    delta["refresh_records"] = []  # D2: 定向重拉获得的新记录，主线程统一回写镜像
    content_adds = {}
    entry = None
    field_payloads = []
    record_ok = True
    refresh_tried = set()
    storage_cfg = shared["storage_cfg"]
    while True:
        if job.cancelled:
            return (data_id, None, delta, content_adds)
        stale_aid = None
        for att_que_id, att_vals in fields:
            att_cid = shared["att_cid_map"].get(att_que_id)
            payload_items = []
            for v in att_vals:
                qf_url = v.get("value") or v.get("dataValue") or ""
                if not qf_url: delta["skipped_bad_url"] += 1; record_ok = False; continue
                raw_name = (v.get("otherInfo") or "").strip()
                if not raw_name: raw_name = qf_url.rsplit("/", 1)[-1].split("?")[0]
                name = sanitize_filename(raw_name)
                ext = os.path.splitext(name)[1].lower()
                exp_left = check_expire(qf_url)
                if exp_left is not None and exp_left < MIN_EXPIRE_SEC:
                    # P1-8: URL 过期时优先定向重拉该记录刷新，而不是直接跳过
                    aid = str(rec.get("applyId") or "")
                    if aid and job.mode in ("prefetch", "migrate") and aid not in refresh_tried:
                        stale_aid = aid
                        break
                    job.warn(f"[{data_id}] {name} URL已过期 跳过"); delta["skipped_expired"] += 1; record_ok = False
                    continue
                cache_rel = f"{job.form}/{data_id}/{att_que_id}/{name}"
                cache_path = os.path.join(str(shared["local_cache_root"]), cache_rel)
                if not os.path.exists(cache_path) or os.path.getsize(cache_path) == 0:
                    try:
                        # P1-7: 流式下载，不把整个文件读进内存
                        download_stream(qf_url, cache_path)
                        delta["downloaded"] += 1
                    except Exception as e:
                        job.error(f"[{data_id}] 下载失败 {name}: {e}"); delta["errors"] += 1; record_ok = False; continue
                else:
                    delta["cached"] += 1
                if storage_cfg.get("upload_token"):
                    vps_rel = cache_rel.replace("\\", "/")
                    local_size = os.path.getsize(cache_path)
                    # P1-5: 内容 md5 命中则复用已有 URL（跨记录/跨文件名去重，零请求）
                    digest = None
                    try:
                        digest = file_md5(cache_path)
                    except Exception:
                        digest = None
                    if digest and digest in shared["content_index"]:
                        delta["content_hit"] += 1
                        payload_items.append((shared["content_index"][digest], name, ext))
                        continue
                    # P1-4: HEAD 预检，VPS 已有同名同大小文件则跳过上传，省上行流量
                    hit, hit_url = vps_file_exists(storage_cfg.get("endpoint"), vps_rel, local_size)
                    if hit:
                        delta["vps_hit"] += 1
                        if digest:
                            content_adds[digest] = hit_url
                        payload_items.append((hit_url, name, ext))
                        continue
                    try:
                        import requests as reqs
                        with open(cache_path, "rb") as fh:
                            resp = reqs.post(storage_cfg["upload_url"],
                                files={"file": (name, fh)}, data={"path": vps_rel},
                                headers={"X-Upload-Token": storage_cfg["upload_token"]}, timeout=120)
                        if resp.status_code == 200:
                            vps_url = resp.json().get("url", "")
                            delta["uploaded"] += 1; payload_items.append((vps_url, name, ext))
                            if digest and vps_url:
                                content_adds[digest] = vps_url
                        else:
                            job.error(f"[{data_id}] 上传失败 HTTP {resp.status_code}: {resp.text[:200]}"); delta["errors"] += 1; record_ok = False
                    except Exception as e:
                        job.error(f"[{data_id}] 上传异常 {name}: {e}"); delta["errors"] += 1; record_ok = False
                else:
                    final_url = f"file://{cache_path.replace(os.sep, '/')}"
                    payload_items.append((final_url, name, ext))
            if stale_aid:
                break
            if payload_items:
                field_payloads.append((att_cid, payload_items))
        if not stale_aid:
            break
        refresh_tried.add(stale_aid)
        try:
            fresh_rows = fetch_qingflow_records_by_ids(job.form, [stale_aid])
            if not fresh_rows:
                raise RuntimeError("接口未返回该记录")
            rec = fresh_rows[0]
            delta["refreshed_urls"] += 1
            delta["refresh_records"].append(rec)  # D2: 收尾统一回写镜像
            job.info(f"[{data_id}] URL已过期，定向重拉刷新成功")
            answers = {str(a.get("queId")): a for a in rec.get("answers", [])}
            fields = []
            for aq2 in shared["att_que_ids"]:
                if aq2 not in shared["att_cid_map"]:
                    continue
                ans2 = answers.get(aq2)
                vals2 = (ans2.get("values") or []) if ans2 else []
                if vals2:
                    fields.append((aq2, vals2))
            total_files = sum(len(x[1]) for x in fields)
        except Exception as e:
            job.error(f"[{data_id}] 定向重拉失败: {e}"); delta["errors"] += 1
            record_ok = False
            break
    if not field_payloads or not record_ok:
        return (data_id, None, delta, content_adds)
    if job.mode == "migrate" and job.commit:
        write_ok = True
        for att_cid, payload_items in field_payloads:
            att_payload = []
            for vps_url, fname, fext in payload_items:
                att_payload.append({"downloadUrl": vps_url, "name": fname,
                    "previewUrl": vps_url, "url": vps_url, "ext": fext})
            # P0-2: formDataJson 不得包含去重键；P0-3: 必须带 searchCondition 定位
            body = {
                "appType": shared["ctx"]["appType"], "systemToken": shared["ctx"]["systemToken"],
                "userId": shared["ctx"]["userId"], "formUuid": shared["ctx"]["formUuid"],
                "noExecuteExpression": shared["cfg"].get("noExecuteExpression", True),
                "searchCondition": json.dumps([{
                    "key": shared["ded_cid"],
                    "value": str(data_id),
                    "type": "TEXT",
                    "operator": "eq",
                    "componentName": shared["ded_cname"],
                }], ensure_ascii=False),
                "formDataJson": json.dumps({att_cid: att_payload}, ensure_ascii=False),
                "useAlias": False,
            }
            try:
                # P0-1: http_request(url, method=..., headers=..., body=...)，返回已解析 dict
                resp = http_request(INSERT_UPDATE_URL,
                    headers={"x-acs-dingtalk-access-token": shared["token"]},
                    body=body, min_interval=0.3)
                if resp and resp.get("success") is False:
                    job.error(f"[{data_id}] 宜搭返回错误: {json.dumps(resp, ensure_ascii=False)[:300]}")
                    delta["errors"] += 1; write_ok = False
                else:
                    delta["written"] += 1
            except Exception as e:
                job.error(f"[{data_id}] 写入异常: {e}"); delta["errors"] += 1; write_ok = False
        # 台账汇总条目（merged：整条记录的文件数/URL 一览），由主线程统一落盘
        all_urls = [u for _, pl in field_payloads for u, _, _ in pl]
        # D1: urlSource 标记 URL 来源（vps=自建永久 / local=本地文件 / qingflow=轻流签名）
        url_source = "vps" if storage_cfg.get("upload_token") else "local"
        entry = {"dataID": str(data_id), "attachments": total_files,
                 "urls": all_urls, "success": write_ok, "merged": True,
                 "urlSource": url_source}
    return (data_id, entry, delta, content_adds)


def _run_att_job(job):
    try:
        job.status = "running"
        job.info(f"附件任务启动: mode={job.mode}  form={job.form}  commit={job.commit}")
        cred = load_credentials()
        cfg = load_form_config(job.form)
        ctx = yida_context(cred, cfg)
        for k in ("appType", "systemToken", "userId", "formUuid"):
            if not ctx.get(k):
                job.error(f"配置缺失: 宜搭 {k} 未填写"); return
        storage_cfg = load_attachment_config(cred)
        local_cache_root = BASE_DIR / storage_cfg.get("local_cache", "data/attachment_cache")
        att_que_ids, ded_cid, ded_cname = parse_attachment_mapping(job.form)
        if not att_que_ids: job.error(f"表单「{job.form}」无附件字段"); return
        if not ded_cid: job.error(f"表单「{job.form}」未找到去重键 (queId={DEDUP_QUE_ID})"); return
        job.info(f"附件字段数={len(att_que_ids)}  去重键={ded_cid}")
        raw_path = DATA_DIR / "raw" / f"{job.form}_raw.json"
        if not raw_path.exists(): job.error(f"原始数据不存在: {raw_path}"); return
        # P1-8: raw 附件 URL 缓存超过 24h 时，先增量拉取刷新一次，减少逐条定向补拉
        fresh, _, _ = raw_cache_fresh(job.form)
        if not fresh:
            job.info("附件 URL 缓存已超过 24h，先执行 01 增量拉取刷新 ...")
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            cmd = [PY, "-X", "utf8", str(SCRIPTS / "01_fetch_qingflow.py"), job.form, "--incremental"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=1800, cwd=str(BASE_DIR))
                tail = "\n".join((r.stdout or "").strip().splitlines()[-15:])
                job.info(f"增量拉取退出码 {r.returncode}\n{tail}")
                if r.returncode != 0:
                    job.warn("增量拉取失败，将继续使用现有缓存并按需定向补拉")
            except subprocess.TimeoutExpired:
                job.warn("增量拉取超时(30min)，将继续使用现有缓存并按需定向补拉")
            except Exception as e:
                job.warn(f"增量拉取异常: {e}，将继续使用现有缓存并按需定向补拉")
        raw = load_json(raw_path)
        records = raw if isinstance(raw, list) else raw.get("result", {}).get("result", [])
        # 仅迁移「宜搭已有该条数据」的附件：宜搭没有这条数据，上传附件毫无意义。
        # 以 02c 产物（宜搭真实存量）中的 轻流数据ID -> 宜搭实例 映射为准。
        yida_path = DATA_DIR / "raw" / f"{job.form}_yida_instances.json"
        yida_did_set = set()
        yida_filter = False
        if yida_path.exists():
            try:
                yj = load_json(yida_path)
                did_map = yj.get("didToInst") or {d: i for i, d in (yj.get("existing") or {}).items() if d}
                yida_did_set = set(did_map)
                yida_filter = bool(yida_did_set)
            except Exception as e:
                job.warn(f"读取宜搭存量失败: {e}")
        else:
            job.warn("未找到宜搭存量文件(02c产物)，无法按宜搭已有数据过滤，将处理全部源记录")
        if yida_filter:
            job.info(f"范围: 仅迁移「宜搭已有对应记录」的数据 (宜搭存量 {len(yida_did_set)} 个数据ID)")
        if job.mode == "migrate":
            job.info("迁移模式：复用预取已上传到 VPS 的文件（内容索引/HEAD 预检命中则免重复上传），仅把附件写入宜搭对应记录")
        token = None
        if job.commit:
            try: token = get_dingtalk_token(cred)
            except Exception as e: job.warn(f"获取钉钉 token 失败: {e}，将跳过写入"); job.commit = False
        stats = {"total_records": len(records), "has_att": 0, "total_files": 0,
                 "migrated_records": 0, "migrated_files": 0,
                 "pending_records": 0, "pending_files": 0,
                 "downloaded": 0, "cached": 0, "uploaded": 0, "vps_hit": 0,
                 "content_hit": 0, "written": 0,
                 "skipped_bad_url": 0, "skipped_expired": 0, "skipped_no_yida": 0,
                 "skipped_migrated": 0, "errors": 0, "refreshed_urls": 0}
        result_map = {}  # P1-6: 写入台账（dataID -> 汇总条目），增量落盘
        # 读旧台账：增量跳过已迁移记录 + 累积保留历史成功记录
        att_ledger = load_att_ledger(job.form)
        if att_ledger:
            job.info(f"附件台账: 已有 {len(att_ledger)} 条记录迁移成功")
            if job.mode in ("migrate", "prefetch"):
                job.info("增量模式: 已迁移记录将被跳过，仅处理未迁移/有变化的记录")
        content_index = load_content_index(local_cache_root)  # P1-5: md5 -> 已上传 URL
        if job.limit > 0:
            job.info(f"数量限制: 最多处理 {job.limit} 条待迁移记录（0=不限制）")
        limit_count = 0
        # 预构建 附件字段 queId -> 宜搭组件ID 映射
        att_cid_map = {}
        mp = BASE_DIR / "mappings" / f"{job.form}_mapping.csv"
        if mp.exists():
            with open(mp, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    row = {k.strip(): (v or "").strip() for k, v in row.items()}
                    qid = row.get("轻流queId", "").strip()
                    if qid in att_que_ids:
                        att_cid_map[qid] = row.get("componentId", "")
        # 1) 预筛：按记录聚合附件字段，过滤无 dataID / 无附件 / 宜搭无此数据
        eligible = []
        for rec in records:
            if job.cancelled:
                if result_map: _flush_att_result(job.form, result_map)
                save_content_index(local_cache_root, content_index)
                job.warn("任务被用户取消"); job.status = "cancelled"; return
            answers = {str(a.get("queId")): a for a in rec.get("answers", [])}
            data_id = get_scalar(answers.get(DEDUP_QUE_ID, {}))
            if not data_id: continue
            if yida_filter and data_id not in yida_did_set:
                stats["skipped_no_yida"] += 1; continue
            fields = []
            total = 0
            for att_que_id in att_que_ids:
                if att_que_id not in att_cid_map: continue
                att_ans = answers.get(att_que_id)
                vals = (att_ans.get("values") or []) if att_ans else []
                if vals:
                    fields.append((att_que_id, vals))
                    total += len(vals)
            if not fields: continue
            eligible.append((rec, data_id, total, fields))
        stats["has_att"] = len(eligible)
        stats["total_files"] = sum(e[2] for e in eligible)
        # C4: 前端进度条依据 —— total=待处理条数, done=已处理（含跳过）条数
        stats["total"] = len(eligible)
        stats["done"] = 0
        # 2) 逐记录处理（增量：台账命中的已迁移记录直接跳过）
        # C2: peek 保持串行（轻量，不下载）；prefetch/migrate 用线程池并发
        # （ATT_WORKERS 环境变量可调并发数，默认 4；http_request 限速在 common 内已加锁）
        all_refresh = []  # D2: 收集各 worker 定向重拉的记录，收尾统一回写镜像
        if job.mode == "peek":
            for idx, (rec, data_id, total_files, fields) in enumerate(eligible, 1):
                if job.cancelled:
                    if result_map: _flush_att_result(job.form, result_map)
                    save_content_index(local_cache_root, content_index)
                    job.warn("任务被用户取消"); job.status = "cancelled"; return
                if job.limit > 0 and limit_count >= job.limit:
                    job.info(f"达到数量限制（{job.limit} 条），停止处理")
                    break
                if att_ledger:
                    m = att_ledger.get(data_id)
                    if m and m["attachments"] >= total_files and not _ledger_url_stale(m):
                        stats["skipped_migrated"] += 1
                        stats["migrated_records"] += 1
                        stats["migrated_files"] += total_files
                        stats["done"] += 1
                        continue
                limit_count += 1
                stats["done"] += 1
                for att_que_id, att_vals in fields:
                    for v in att_vals:
                        url = v.get("value") or v.get("dataValue") or ""
                        name = (v.get("otherInfo") or "").strip() or url.rsplit("/", 1)[-1].split("?")[0]
                        exp = check_expire(url) if url else None
                        if exp and exp < MIN_EXPIRE_SEC:
                            job.warn(f"[{data_id}] {name}  已过期/将过期"); stats["skipped_expired"] += 1
                if idx % 20 == 0:
                    job.info(f"peek 进度: {idx}/{len(eligible)} (含附件)")
                time.sleep(0.05)
        else:
            # 并发：先按台账跳过已迁移记录，再提交线程池，主线程统一汇总
            shared = {
                "storage_cfg": storage_cfg, "ctx": ctx, "cfg": cfg,
                "ded_cid": ded_cid, "ded_cname": ded_cname,
                "att_que_ids": att_que_ids, "att_cid_map": att_cid_map,
                "content_index": content_index, "local_cache_root": local_cache_root,
                "token": token,
            }
            pending_items = []
            skipped_base = 0
            for rec, data_id, total_files, fields in eligible:
                if job.cancelled: break
                if att_ledger:
                    m = att_ledger.get(data_id)
                    if m and m["attachments"] >= total_files and not _ledger_url_stale(m):
                        stats["skipped_migrated"] += 1
                        stats["migrated_records"] += 1
                        stats["migrated_files"] += total_files
                        skipped_base += 1
                        continue
                pending_items.append((rec, data_id, total_files, fields))
                if job.limit > 0 and len(pending_items) >= job.limit:
                    job.info(f"达到数量限制（{job.limit} 条），停止处理")
                    break
            stats["done"] = skipped_base
            max_workers = min(4, max(1, int(os.environ.get("ATT_WORKERS", "4"))))
            job.info(f"并发处理: {max_workers} 线程，共 {len(pending_items)} 条待处理")
            done_count = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_att_record_worker, job, rec, data_id, total_files, fields, shared): data_id
                           for rec, data_id, total_files, fields in pending_items}
                for fut in as_completed(futures):
                    if job.cancelled:
                        job.warn("任务被用户取消")
                        break
                    data_id = futures[fut]
                    try:
                        _did, entry, delta, adds = fut.result()
                    except Exception as e:
                        job.error(f"[{data_id}] 并发处理异常: {e}"); stats["errors"] += 1
                        continue
                    refresh_records = delta.pop("refresh_records", [])
                    if refresh_records:
                        all_refresh.extend(refresh_records)
                    for k, v in delta.items():
                        stats[k] = stats.get(k, 0) + v
                    content_index.update(adds)
                    if entry:
                        result_map[str(data_id)] = entry
                        if len(result_map) % 10 == 0:
                            _flush_att_result(job.form, result_map)
                    done_count += 1
                    stats["done"] = skipped_base + done_count
                    if done_count % 10 == 0:
                        job.info(f"进度: {done_count}/{len(pending_items)} 条  download={stats['downloaded']} "
                                 f"upload={stats['uploaded']} vps命中={stats['vps_hit']} "
                                 f"内容命中={stats['content_hit']} written={stats['written']} errors={stats['errors']}")
            if job.cancelled:
                if result_map: _flush_att_result(job.form, result_map)
                save_content_index(local_cache_root, content_index)
                _writeback_mirror(job.form, all_refresh, job)  # D2: 已重拉的记录不丢弃
                job.status = "cancelled"; return
        if result_map:
            _flush_att_result(job.form, result_map)
        save_content_index(local_cache_root, content_index)
        _writeback_mirror(job.form, all_refresh, job)  # D2: 定向重拉结果回写镜像
        # 待迁移数 = 含附件记录 - 已迁移记录（未处理/失败/被数量限制的都算待迁移）
        stats["pending_records"] = stats["has_att"] - stats["migrated_records"]
        stats["pending_files"] = stats["total_files"] - stats["migrated_files"]
        job.stats = stats
        if job.mode == "peek": job.success(f"预览完成: {stats['has_att']} 条含附件, {stats['total_files']} 个文件, 跳过(无宜搭数据)={stats['skipped_no_yida']}", stats)
        elif job.mode == "prefetch": job.success(f"预取完成: download={stats['downloaded']} upload={stats['uploaded']} vps命中={stats['vps_hit']} 定向刷新={stats['refreshed_urls']} 跳过(已迁移)={stats['skipped_migrated']}", stats)
        else: job.success(f"迁移完成: 写入{stats['written']}条 定向刷新={stats['refreshed_urls']} 跳过(已迁移)={stats['skipped_migrated']} 跳过(无宜搭数据)={stats['skipped_no_yida']}", stats)
        job.status = "done"
    except Exception as e:
        tb = traceback.format_exc()
        job.error(f"任务异常: {e}\n{tb}"); job.status = "error"

def start_att_job(mode, form_name, limit=0, commit=False):
    job_id = uuid.uuid4().hex[:12]
    job = AttJob(job_id, form_name, mode, limit, commit)
    att_jobs[job_id] = job
    while len(att_jobs) > att_jobs_max:
        att_jobs.popitem(last=False)
    job._thread = threading.Thread(target=_run_att_job, args=(job,), daemon=True)
    job._thread.start()
    return job_id

# ================================================================
#  Flask App 与路由
# ================================================================
app = Flask(__name__, static_folder=str(WEB), static_url_path="")

# ---------- 可选的 API 认证（P2-6） ----------
# 默认不开启（服务只绑 127.0.0.1）。若需远程访问，设置环境变量 MIGRATION_API_TOKEN，
# 之后所有 /api/* 请求都必须携带 Authorization: Bearer <token>。
API_TOKEN = os.environ.get("MIGRATION_API_TOKEN", "").strip()

# 脱敏模式（凭证安全）：非本机监听（在 main 中随 --host 更新）或显式开启
# MIGRATION_REDACT_SECRETS=1 时，凭证/设置接口不再回显密钥明文（只写不回显）。
REDACT_SECRETS = os.environ.get("MIGRATION_REDACT_SECRETS", "").strip() == "1"


def mask_secret(v):
    """密钥脱敏：长度 >4 时保留首字符与末 4 位，中间掩码；短值整体返回。"""
    s = str(v or "")
    if len(s) <= 4:
        return s
    return s[0] + "***" + s[-4:]


def _keep_if_masked(new_val, old_val):
    """保存设置时对已掩码/空值的密钥保持原值，避免把脱敏回显值写回文件。"""
    new_val = str(new_val or "").strip()
    if not new_val or "***" in new_val:
        return str(old_val or "")
    return new_val


@app.before_request
def _require_token():
    if not API_TOKEN:
        return None
    if not request.path.startswith("/api/"):
        return None
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else request.headers.get("X-Api-Token", "")
    if token != API_TOKEN:
        return jsonify(err="unauthorized"), 401
    return None

# ---------- 静态文件 ----------
@app.route("/")
def serve_index():
    return send_from_directory(str(WEB), "index.html")

@app.route("/app.js")
def serve_js():
    return send_from_directory(str(WEB), "app.js", mimetype="application/javascript; charset=utf-8")

@app.route("/app.css")
def serve_css():
    return send_from_directory(str(WEB), "app.css", mimetype="text/css; charset=utf-8")

# ---------- 数据迁移 API ----------
@app.route("/api/forms", methods=["GET", "POST", "PUT", "DELETE"])
def api_forms():
    if request.method == "GET":
        return jsonify(list_forms())
    data = request.get_json(silent=True) or {}
    if request.method == "POST":
        ok, msg = add_form_to_registry(
            data.get("name"), data.get("appKey"),
            data.get("formUuid"), data.get("note"),
            _to_int(data.get("appId"), 0))
        if not ok: return jsonify({"ok": False, "msg": msg}), 400
        jid = start_data_job(data.get("name"), [("00", build_cmd("00", data.get("name")))])
        return jsonify({"ok": True, "msg": msg, "jobId": jid})
    if request.method == "PUT":
        ok, msg = update_form_in_registry(
            data.get("oldName"), data.get("name"),
            data.get("appKey"), data.get("formUuid"), data.get("note"),
            _to_int(data.get("appId"), 0))
        if not ok: return jsonify({"ok": False, "msg": msg}), 400
        old = data.get("oldName"); new_n = data.get("name")
        if old and new_n and old != new_n:
            old_cfg = CONFIG / "forms" / f"{old}.json"
            if old_cfg.exists():
                try: old_cfg.unlink()
                except Exception: pass
        jid = start_data_job(new_n, [("00", build_cmd("00", new_n))])
        return jsonify({"ok": True, "msg": msg, "jobId": jid})
    if request.method == "DELETE":
        ok, msg = delete_form_from_registry(data.get("name"))
        if not ok: return jsonify({"ok": False, "msg": msg}), 400
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"err": "method not allowed"}), 405

@app.route("/api/forms/<path:form_name>/detail")
def api_form_detail(form_name):
    """单表单详情：选中表单时按需加载完整状态（含计数、附件统计）。"""
    return jsonify(get_form_detail(form_name))

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        opts = load_settings()
        if REDACT_SECRETS:
            for a in opts.get("yidaApps", []):
                if a.get("systemToken"):
                    a["systemToken"] = mask_secret(a["systemToken"])
        return jsonify(opts)
    data = request.get_json(silent=True) or {}
    try: opt = save_settings(data)
    except Exception as e: return jsonify({"ok": False, "msg": f"保存设置失败: {e}"}), 500
    return jsonify({"ok": True, "options": opt})

# ---------- 凭证配置 API（部署后经网页配置凭证） ----------
# 安全设计：默认只返回脱敏状态；脱敏模式（非本机监听或 MIGRATION_REDACT_SECRETS=1）
# 下 ?view=full 被拒绝，前端变为「只写不回显」：空值=保持原值，clear:[...]=显式置空。
@app.route("/api/credentials", methods=["GET", "POST"])
def api_credentials():
    try:
        cred = load_credentials(required=False)
    except Exception:
        cred = {}
    if request.method == "GET":
        view = request.args.get("view", "status")
        if view == "full" and REDACT_SECRETS:
            return jsonify({"ok": False, "msg": "脱敏模式下不允许回显完整凭证"}), 403
        return jsonify(credential_summary(cred, redact=REDACT_SECRETS))
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "msg": "请求体格式错误"}), 400
    # 占位符校验：拒绝「填入/你的/XXX/xxxx」等非真实值
    placeholders = ("填入", "你的", "xxx", "xxxx", "****")
    def _scan(obj, path="<root>"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _scan(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            low = obj.lower().strip()
            if low and any(p in low for p in placeholders):
                raise ValueError(f"字段 {path} 含占位符，请填写真实凭证值")
    try:
        _scan(data)
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    # 显式置空：clear: ["qingflow.accessToken", ...]（兼容 "qingflow:accessToken"）
    clear_keys = data.get("clear") or []
    if isinstance(clear_keys, str):
        clear_keys = [clear_keys]
    for key in clear_keys:
        sep = ":" if ":" in key else "."
        if sep in key:
            sec, fld = key.split(sep, 1)
            if sec in cred and isinstance(cred[sec], dict):
                cred[sec].pop(fld, None)
    # 合并各分区字段（仅处理凭证字段；yidaApps/activeApp 仍由 /api/settings 管理）
    for section, field, sensitive, env_var in CREDENTIAL_FIELDS:
        if env_var and os.environ.get(env_var, "").strip():
            continue  # 被环境变量覆盖的字段不允许网页覆盖
        sec_data = data.get(section)
        if not isinstance(sec_data, dict) or field not in sec_data:
            continue
        val = str(sec_data.get(field)).strip()
        if val == "":
            continue  # 空字符串 = 保持原值
        cred.setdefault(section, {})[field] = val
    try:
        save_credentials(cred)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"保存凭证失败: {e}"}), 500
    return jsonify({"ok": True, "msg": "凭证已保存"})

@app.route("/api/credentials/test", methods=["POST"])
def api_credentials_test():
    """连通性自检：钉钉 accessToken / 宜搭列表单 / 轻流应用包，错误信息脱敏。"""
    try:
        cred = load_credentials(required=False)
    except Exception:
        cred = {}
    result = {}
    ding = cred.get("dingtalk") or {}
    if not (ding.get("appKey") and ding.get("appSecret")):
        result["dingtalk"] = {"ok": False, "msg": "appKey/appSecret 未配置"}
    else:
        try:
            token = get_dingtalk_token(cred)
            result["dingtalk"] = {"ok": bool(token), "msg": "OK"}
        except SystemExit as e:
            result["dingtalk"] = {"ok": False, "msg": str(e.args[0]) if e.args else "获取 accessToken 失败"}
        except Exception as e:
            result["dingtalk"] = {"ok": False, "msg": f"异常: {e}"}
    r = list_yida_forms(cred, page=1, page_size=1)
    if r["ok"]:
        result["yida"] = {"ok": True, "msg": f"OK（共 {r.get('totalCount', 0)} 个表单）"}
    else:
        result["yida"] = {"ok": False, "msg": r.get("msg") or "宜搭连接失败"}
    r2 = list_qingflow_apps(cred)
    if r2["ok"]:
        total = sum(len(t.get("apps", [])) for t in r2.get("tags", []))
        result["qingflow"] = {"ok": True, "msg": f"OK（共 {total} 个应用）"}
    else:
        result["qingflow"] = {"ok": False, "msg": r2.get("msg") or "轻流连接失败"}
    return jsonify(result)

# ---------- 表单列表拉取 API（需求：拉取列表 + 手动关联） ----------
@app.route("/api/yida/forms")
def api_yida_forms():
    try:
        cred = load_credentials(required=False)
    except Exception:
        cred = {}
    try:
        app_idx_raw = request.args.get("appIdx")
        app_idx = int(app_idx_raw) if app_idx_raw not in (None, "", "null") else None
        page = int(request.args.get("page", 1) or 1)
        page_size = int(request.args.get("pageSize", 100) or 100)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "分页参数无效"}), 400
    r = list_yida_forms(cred, app_idx=int(app_idx) if app_idx is not None else None,
                        page=page, page_size=page_size)
    if not r["ok"]:
        return jsonify({"ok": False, "msg": r["msg"]}), 400
    return jsonify({"ok": True, "forms": r["forms"], "totalCount": r["totalCount"],
                    "currentPage": r["currentPage"]})

@app.route("/api/qingflow/apps")
def api_qingflow_apps():
    try:
        cred = load_credentials(required=False)
    except Exception:
        cred = {}
    r = list_qingflow_apps(cred)
    if not r["ok"]:
        return jsonify({"ok": False, "msg": r["msg"]}), 400
    return jsonify({"ok": True, "tags": r["tags"]})

@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    form = data.get("form"); step = data.get("step")
    if not form or step not in STEP_DEFS:
        return jsonify({"ok": False, "msg": "无效的表单或步骤"}), 400
    if STEP_DEFS[step]["per_form"] and not (CONFIG / "forms" / f"{form}.json").exists():
        return jsonify({"ok": False, "msg": f"表单配置不存在，请先生成配置(步骤00): {form}"}), 400
    st = load_settings(); ao = active_app_opts(st)
    if step == "04":
        ok, msg = check_diff_fresh(form)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 409
    cmd = build_cmd(step, form,
                    commit=bool(data.get("commit", ao["commitDefault"])),
                    limit=data.get("limit", ao["limit"]) or None,
                    force=bool(data.get("force", ao["force"])),
                    skip_fetch=bool(data.get("skipFetch", False)),
                    force_full=bool(data.get("forceFull", False)))
    jid = start_data_job(form, [(step, cmd)])
    return jsonify({"ok": True, "jobId": jid})

@app.route("/api/run-stage", methods=["POST"])
def api_run_stage():
    data = request.get_json(silent=True) or {}
    form = data.get("form"); stage = data.get("stage")
    if not form or stage not in STAGES:
        return jsonify({"ok": False, "msg": "无效的表单或阶段"}), 400
    if stage != "s1" and not (CONFIG / "forms" / f"{form}.json").exists():
        return jsonify({"ok": False, "msg": f"表单配置不存在，请先运行阶段一: {form}"}), 400
    st = load_settings(); ao = active_app_opts(st)
    if stage == "s4":
        ok, msg = check_diff_fresh(form)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 409
    cmds = build_stage_cmds(stage, form,
                            commit=bool(data.get("commit", ao["commitDefault"])),
                            limit=data.get("limit", ao["limit"]) or None,
                            force=bool(data.get("force", ao["force"])),
                            skip_fetch=bool(data.get("skipFetch", False)),
                            force_full=bool(data.get("forceFull", False)),
                            refresh_yida=bool(data.get("refreshYida", False)))
    jid = start_data_job(form, cmds)
    return jsonify({"ok": True, "jobId": jid})

@app.route("/api/run-all", methods=["POST"])
def api_run_all():
    data = request.get_json(silent=True) or {}
    form = data.get("form")
    if not form: return jsonify({"ok": False, "msg": "缺少表单名"}), 400
    st = load_settings(); ao = active_app_opts(st)
    commit = bool(data.get("commit", ao["commitDefault"]))
    limit = data.get("limit", ao["limit"]) or None
    force = bool(data.get("force", ao["force"]))
    skip_fetch = bool(data.get("skipFetch", False))
    force_full = bool(data.get("forceFull", False))
    refresh_yida = bool(data.get("refreshYida", False))
    cmds = []
    for sk in STAGE_ORDER:
        cmds += build_stage_cmds(sk, form, commit=commit, limit=limit, force=force,
                                 skip_fetch=skip_fetch, force_full=force_full,
                                 refresh_yida=refresh_yida)
    jid = start_data_job(form, cmds)
    return jsonify({"ok": True, "jobId": jid})

@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    """数据准备：拉取数据与格式化解耦。

    mode:
      - all(默认) : 00+01+02+02b+02c+02d+03 完整准备（拉取+对齐+对账+转换）
      - fetch     : 00+01+02+02c+02d 仅拉取（轻流数据/宜搭结构/宜搭存量）+ 三方对账
      - transform : 02b+03 仅字段对齐+格式化（复用已拉取产物，宜搭修改后配合 fetch 使用）
    不含写入(04)，不产生任何宜搭侧修改。"""
    data = request.get_json(silent=True) or {}
    form = data.get("form")
    if not form: return jsonify({"ok": False, "msg": "缺少表单名"}), 400
    mode = data.get("mode", "all")
    if mode not in ("all", "fetch", "transform"):
        return jsonify({"ok": False, "msg": f"未知 mode: {mode}（可选 all/fetch/transform）"}), 400
    cmds = build_prepare_cmds(
        form, mode=mode,
        skip_fetch=bool(data.get("skipFetch", False)),
        force_full=bool(data.get("forceFull", False)),
        refresh_yida=bool(data.get("refreshYida", False)),
    )
    jid = start_data_job(form, cmds)
    return jsonify({"ok": True, "jobId": jid,
                    "mode": mode,
                    "steps": [s for s, _ in cmds]})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = get_data_job(job_id)
    return jsonify(job)

# ---------- 迁移前预检 API ----------
@app.route("/api/preflight/<form_name>")
def api_preflight(form_name):
    """迁移前预检：扫描各阶段产物，检测已知边界情况并返回结构化告警。"""
    import subprocess
    script = str(BASE_DIR / "scripts" / "preflight_check.py")
    try:
        r = subprocess.run(
            [sys.executable, script, form_name],
            capture_output=True, text=True, timeout=30, cwd=str(BASE_DIR))
        if r.returncode != 0:
            return jsonify({"ok": False, "msg": r.stderr.strip() or r.stdout.strip()[:500]})
        data = json.loads(r.stdout)
        return jsonify(data)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "msg": f"预检输出解析失败: {e}", "raw": r.stdout[:500]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "预检超时（30s）"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"预检失败: {e}"})


@app.route("/api/preflight/<form_name>/md")
def api_preflight_md(form_name):
    """生成「宜搭手动调整工作清单」Markdown：返回文本并保存到 docs/worklist/。"""
    import subprocess
    import importlib.util
    script = str(BASE_DIR / "scripts" / "preflight_check.py")
    try:
        r = subprocess.run(
            [sys.executable, script, form_name],
            capture_output=True, text=True, timeout=30, cwd=str(BASE_DIR))
        if r.returncode != 0:
            return jsonify({"ok": False, "msg": r.stderr.strip() or r.stdout.strip()[:500]})
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "msg": f"预检输出解析失败: {e}"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "预检超时（30s）"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"预检失败: {e}"})

    spec = importlib.util.spec_from_file_location("preflight_check_md", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    md_text = mod.render_worklist_md(
        form_name, data.get("worklist") or [], data.get("summary") or {},
        check_count=len(data.get("checks") or []))

    # 保存到独立文件夹 docs/worklist/
    from datetime import datetime
    wl_dir = BASE_DIR / "docs" / "worklist"
    wl_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{form_name}_工作清单_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    fpath = wl_dir / fname
    fpath.write_text(md_text, encoding="utf-8")
    return jsonify({"ok": True, "md": md_text, "path": str(fpath)})

# ---------- 附件迁移 API ----------
@app.route("/api/attach/stats/<form_name>")
def api_attach_stats(form_name):
    refresh = request.args.get("refresh") == "1"
    return jsonify(attachment_stats(form_name, refresh=refresh))

@app.route("/api/attach/run", methods=["POST"])
def api_attach_run():
    data = request.get_json(silent=True) or {}
    form_name = data.get("form"); mode = data.get("mode", "peek")
    limit = int(data.get("limit", 0) or 0); commit = bool(data.get("commit", False))
    if not form_name: return jsonify(err="form is required"), 400
    if mode not in ("peek", "prefetch", "migrate"): return jsonify(err="invalid mode"), 400
    # 迁移未勾「写入宜搭」时自动降级为预取（下载+上传VPS，不写宜搭），
    # 前端无需单独提供「预取」按钮
    if mode == "migrate" and not commit:
        mode = "prefetch"
    job_id = start_att_job(mode, form_name, limit, commit)
    return jsonify({"job_id": job_id})

@app.route("/api/attach/progress/<job_id>")
def api_attach_progress(job_id):
    job = att_jobs.get(job_id)
    if not job: return jsonify(err="job not found"), 404
    since = request.args.get("since")
    if since is not None:
        since_f = float(since)
        new_events = [e for e in job.events if e["time"] > since_f]
    else:
        new_events = list(job.events)
    return jsonify({
        "job_id": job.id, "status": job.status, "mode": job.mode,
        "form": job.form, "stats": job.stats, "events": new_events,
    })

@app.route("/api/attach/cancel/<job_id>", methods=["POST"])
def api_attach_cancel(job_id):
    job = att_jobs.get(job_id)
    if not job: return jsonify(err="job not found"), 404
    if job.status != "running": return jsonify(err=f"job is {job.status}"), 400
    job.cancel()
    return jsonify({"ok": True})

@app.route("/api/attach/jobs")
def api_attach_jobs():
    result = []
    for jid, job in att_jobs.items():
        result.append({
            "job_id": jid, "form": job.form, "mode": job.mode,
            "status": job.status, "stats": job.stats, "event_count": len(job.events),
        })
    return jsonify(result)

# ---------- 日志 API ----------
@app.route("/api/logs")
def api_logs():
    if not LOGS.exists(): return jsonify([])
    files = []
    for f in sorted(LOGS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix == ".log":
            files.append({"name": f.name, "size": f.stat().st_size,
                          "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat()})
    return jsonify(files[:50])

@app.route("/api/logs/<filename>")
def api_log_content(filename):
    # 只允许读取 logs 目录下的 .log 文件，拒绝任何路径穿越
    if os.path.basename(filename) != filename or not filename.endswith(".log"):
        return jsonify(err="bad filename"), 400
    p = (LOGS / filename).resolve()
    if not str(p).startswith(str(LOGS.resolve())) or not p.exists():
        return jsonify(err="not found"), 404
    return jsonify({"name": filename, "content": p.read_text(encoding="utf-8", errors="replace")[:200000]})

# ================================================================
#  启动
# ================================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；非 127.0.0.1 时必须设置 MIGRATION_API_TOKEN 环境变量")
    args = ap.parse_args()
    port = args.port

    # P2-6: 仅本机监听时无需认证；一旦对外暴露则强制要求 Bearer Token
    if args.host not in ("127.0.0.1", "localhost") and not API_TOKEN:
        sys.exit("[安全] 监听非本机地址时必须先设置环境变量 MIGRATION_API_TOKEN，否则任何人都可触发迁移任务")

    # 脱敏模式：非本机监听时自动开启（凭证/设置接口不回显密钥明文）
    # 注：此处位于模块顶层（__main__ 块），直接赋值即修改模块级 REDACT_SECRETS。
    if args.host not in ("127.0.0.1", "localhost"):
        REDACT_SECRETS = True

    removed = prune_logs()
    if removed:
        print(f"[日志清理] 已删除 {removed} 个历史日志（保留最近 {LOG_KEEP_DAYS} 天 / {LOG_KEEP_MAX} 个）")
    print(f"统一迁移控制台: http://{args.host}:{port}")
    print(f"  数据迁移: /api/*")
    print(f"  附件迁移: /api/attach/*")
    print(f"  接口认证: {'已启用 (Authorization: Bearer ...)' if API_TOKEN else '未启用（仅本机可访问）'}")
    print(f"  凭证脱敏: {'已开启（密钥不回显明文）' if REDACT_SECRETS else '未开启（仅本机，可直接查看/编辑凭证）'}")
    # P0-1: 多线程开发服务器，避免预检/凭证测试等慢请求阻塞整个控制台
    app.run(host=args.host, port=port, debug=False, threaded=True)
