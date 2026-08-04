# -*- coding: utf-8 -*-
"""公共模块：配置加载、HTTP 请求（限速+重试）、钉钉 accessToken 缓存"""
import json
import os
import time
import csv
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import urllib.request
import urllib.error
from urllib.parse import urlencode
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # migration/
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"

DINGTALK_API = "https://api.dingtalk.com"

_last_request_ts = 0.0


# 环境变量 -> credentials 路径的覆盖表（P2-7 凭证安全）
# 设置了对应环境变量时优先使用，credentials.json 里就可以不写明文密钥。
_ENV_OVERRIDES = {
    "QINGFLOW_ACCESS_TOKEN":  ("qingflow", "accessToken"),
    "DINGTALK_APP_KEY":       ("dingtalk", "appKey"),
    "DINGTALK_APP_SECRET":    ("dingtalk", "appSecret"),
    "YIDA_SYSTEM_TOKEN":      ("yida", "systemToken"),
    "YIDA_VPS_UPLOAD_TOKEN":  ("attachment_storage", "upload_token"),
}
# 公开引用：供 Web 服务端/前端提示「某字段由环境变量提供」。
ENV_OVERRIDES = dict(_ENV_OVERRIDES)

# 凭证页可配置的全部字段（分区, 字段, 是否敏感, 对应环境变量或 None）
# 敏感字段在脱敏模式下只显示末 4 位；非敏感字段（baseUrl/userId/endpoint 等）可显示完整值。
CREDENTIAL_FIELDS = [
    ("qingflow", "accessToken", True,  "QINGFLOW_ACCESS_TOKEN"),
    ("qingflow", "baseUrl",     False, None),
    ("qingflow", "userId",      False, None),
    ("dingtalk", "appKey",      True,  "DINGTALK_APP_KEY"),
    ("dingtalk", "appSecret",   True,  "DINGTALK_APP_SECRET"),
    ("yida", "systemToken",     True,  "YIDA_SYSTEM_TOKEN"),
    ("yida", "appType",         False, None),
    ("yida", "userId",          False, None),
    ("attachment_storage", "endpoint",     False, None),
    ("attachment_storage", "upload_url",   False, None),
    ("attachment_storage", "upload_token", True,  "YIDA_VPS_UPLOAD_TOKEN"),
    ("attachment_storage", "local_cache",  False, None),
]


def load_credentials(required=True):
    """加载 credentials.json 并应用环境变量覆盖。

    required=True（默认，管线脚本用）：文件缺失/损坏时抛出明确异常；
    required=False（网页首次配置用）：文件缺失/损坏时返回空分区 dict，
    由调用方按未配置处理，不抛裸异常。"""
    path = CONFIG_DIR / "credentials.json"
    cred = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cred = json.load(f)
    elif required:
        raise FileNotFoundError(f"凭证文件不存在: {path}")
    if not isinstance(cred, dict):
        if required:
            raise ValueError(f"凭证文件格式错误（应为 JSON 对象）: {path}")
        cred = {}
    for env_key, (section, field) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            cred.setdefault(section, {})[field] = val
    return cred


def save_credentials(cred):
    """原子保存凭证到 credentials.json（网页凭证配置用）。"""
    save_json(CONFIG_DIR / "credentials.json", cred)


def credential_summary(cred, redact=False):
    """生成凭证脱敏摘要，供网页「凭证」页展示。

    返回 {section: {field: {configured, source, envVar, value}}}：
      - configured: 是否已配置有效值；
      - source: env（环境变量覆盖）| file（credentials.json）| none；
      - envVar: 覆盖该字段的环境变量名（无则空串）；
      - value: redact=True 时敏感字段仅给末 4 位（保留首位），否则敏感字段完整值；
               非敏感字段始终给完整值。"""
    summary = {}
    for section, field, sensitive, env_var in CREDENTIAL_FIELDS:
        env_val = os.environ.get(env_var, "").strip() if env_var else ""
        file_val = str((cred.get(section) or {}).get(field, "") or "").strip()
        if env_val:
            source, value = "env", env_val
        elif file_val:
            source, value = "file", file_val
        else:
            summary.setdefault(section, {})[field] = {
                "configured": False, "source": "none", "envVar": env_var or "",
                "value": "", "sensitive": sensitive}
            continue
        if sensitive and redact:
            show = value[-4:] if len(value) > 4 else value
            if len(value) > 4:
                show = value[0] + "***" + value[-4:]
            value = show
        summary.setdefault(section, {})[field] = {
            "configured": True, "source": source, "envVar": env_var or "",
            "value": value, "sensitive": sensitive}
    return summary


def load_form_config(form_name):
    path = CONFIG_DIR / "forms" / f"{form_name}.json"
    if not path.exists():
        sys.exit(f"[错误] 表单配置不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_mapping(mapping_file):
    """读取映射 CSV，返回 [{componentId, componentType, queId, transform, ...}]（跳过 skip 行）"""
    path = BASE_DIR / mapping_file
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("transform", "").lower() == "skip":
                continue
            rows.append(row)
    return rows


def http_request(url, method="POST", headers=None, body=None, min_interval=0.25, max_retry=3, params=None):
    """带限速与重试的 HTTP 请求，body 为 dict 时自动转 JSON；params 为 dict 时拼接到 URL 查询串。"""
    global _last_request_ts
    headers = dict(headers or {})
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    for attempt in range(1, max_retry + 1):
        wait = min_interval - (time.time() - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.time()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503) and attempt < max_retry:
                backoff = 2 ** attempt
                print(f"  [重试] HTTP {e.code}，{backoff}s 后第 {attempt + 1} 次尝试")
                time.sleep(backoff)
                continue
            raise RuntimeError(f"HTTP {e.code} {url}\n{detail}") from e
        except urllib.error.URLError as e:
            if attempt < max_retry:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"请求失败（重试耗尽）: {url}")


_token_cache = {"token": None, "expire_at": 0}


def get_dingtalk_token(cred):
    """获取并缓存钉钉企业内部应用 accessToken"""
    if _token_cache["token"] and time.time() < _token_cache["expire_at"]:
        return _token_cache["token"]
    resp = http_request(
        f"{DINGTALK_API}/v1.0/oauth2/accessToken",
        body={"appKey": cred["dingtalk"]["appKey"], "appSecret": cred["dingtalk"]["appSecret"]},
        min_interval=0,
    )
    token = resp.get("accessToken")
    if not token:
        sys.exit(f"[错误] 获取钉钉 accessToken 失败: {resp}")
    _token_cache["token"] = token
    _token_cache["expire_at"] = time.time() + int(resp.get("expireIn", 7200)) - 120
    return token


def list_yida_forms(cred, app_idx=None, page=1, page_size=100, form_types=None):
    """调用宜搭「获取指定应用下的表单列表」接口（GET /v1.0/yida/forms，分页）。

    成功返回 {"ok": True, "forms": [{formUuid, title, formType}], "totalCount", "currentPage"}；
    失败返回 {"ok": False, "msg": "..."}。任何异常都不抛出，供 Web 路由与 CLI 共用。
    app_idx: yidaApps 下标；None 表示使用活跃应用（activeApp）。"""
    try:
        token = get_dingtalk_token(cred)
    except SystemExit as e:
        return {"ok": False, "msg": str(e.args[0]) if e.args else "获取钉钉 accessToken 失败"}
    except Exception as e:
        return {"ok": False, "msg": f"获取钉钉 accessToken 失败: {e}"}
    apps = cred.get("yidaApps") or []
    if not apps:
        return {"ok": False, "msg": "尚未配置宜搭应用（yidaApps 为空），请先在「设置-宜搭应用」中配置"}
    if app_idx is None:
        app_idx = int(cred.get("activeApp", 0) or 0)
    try:
        app_idx = int(app_idx)
    except (TypeError, ValueError):
        return {"ok": False, "msg": f"宜搭应用下标无效: {app_idx}"}
    if not (0 <= app_idx < len(apps)):
        return {"ok": False, "msg": f"宜搭应用下标无效: {app_idx}"}
    app = apps[app_idx]
    app_type = str(app.get("appType", "")).strip()
    system_token = str(app.get("systemToken", "")).strip()
    user_id = str((cred.get("yida") or {}).get("userId", "")).strip()
    if not app_type or not system_token:
        return {"ok": False, "msg": f"宜搭应用「{app.get('name', '')}」的 appType/systemToken 未配置"}
    if not user_id:
        return {"ok": False, "msg": "yida.userId（宜搭操作人 userId）未配置"}
    try:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
    except (TypeError, ValueError):
        page, page_size = 1, 100
    params = {"appType": app_type, "systemToken": system_token, "userId": user_id,
              "pageNumber": page, "pageSize": page_size}
    if form_types:
        params["formTypes"] = form_types
    try:
        resp = http_request(
            f"{DINGTALK_API}/v1.0/yida/forms", method="GET",
            headers={"x-acs-dingtalk-access-token": token},
            params=params, min_interval=0)
    except Exception as e:
        return {"ok": False, "msg": f"宜搭接口调用失败: {e}"}
    result = resp.get("result") or {}
    forms = []
    for f in result.get("data") or []:
        title = f.get("title") or ""
        if isinstance(title, dict):
            title = title.get("zhCN") or title.get("enUS") or ""
        forms.append({"formUuid": str(f.get("formUuid", "") or ""),
                      "title": str(title or ""),
                      "formType": str(f.get("formType", "") or "")})
    return {"ok": True, "forms": forms,
            "totalCount": result.get("totalCount", len(forms)),
            "currentPage": result.get("currentPage", page)}


def list_qingflow_apps(cred):
    """调用轻流「获取工作区全部应用包信息」接口（GET {baseUrl}/tags?userId=）。

    成功返回 {"ok": True, "tags": [{tagName, tagId, apps: [{appKey, appName}]}]}；
    失败返回 {"ok": False, "msg"}。任何异常都不抛出。
    注意：userId 为轻流成员 ID（必填），需在凭证页配置 qingflow.userId。"""
    qf = cred.get("qingflow") or {}
    base = str(qf.get("baseUrl", "")).strip().rstrip("/")
    token = str(qf.get("accessToken", "")).strip()
    user_id = str(qf.get("userId", "")).strip()
    if not base:
        return {"ok": False, "msg": "qingflow.baseUrl 未配置"}
    if not token:
        return {"ok": False, "msg": "qingflow.accessToken 未配置"}
    if not user_id:
        return {"ok": False, "msg": "qingflow.userId（轻流成员 ID）未配置，请在轻流后台个人中心获取"}
    try:
        resp = http_request(f"{base}/tags", method="GET",
                            headers={"accessToken": token},
                            params={"userId": user_id}, min_interval=0)
    except Exception as e:
        return {"ok": False, "msg": f"轻流接口调用失败: {e}"}
    if resp.get("errCode") not in (0, None, ""):
        return {"ok": False, "msg": f"轻流接口返回错误: {resp.get('errMsg') or resp.get('errCode')}"}
    result = resp.get("result") or {}
    tags = []
    for tag in result.get("tagList") or []:
        apps = []
        for a in tag.get("appList") or []:
            if a.get("appKey"):
                apps.append({"appKey": str(a.get("appKey", "")),
                             "appName": str(a.get("appName", "") or "")})
        tags.append({"tagName": str(tag.get("tagName", "") or ""),
                     "tagId": tag.get("tagId"),
                     "apps": apps})
    return {"ok": True, "tags": tags}


def load_attachment_config(cred, local_mandatory=True):
    """读取附件存储配置，返回 {endpoint, upload_url, upload_token, local_cache}。
    若 local_mandatory=True 则要求 local_cache（本地缓存目录）不为空。
    设计为 cred['attachment_storage'] 可选 —— 不存在时返回空字典（由调用方自行处理缺少）。

    P2-7 凭证安全：upload_token 支持用环境变量 YIDA_VPS_UPLOAD_TOKEN 覆盖，
    这样 credentials.json 里可以不写明文密钥。环境变量优先级高于文件。"""
    ac = cred.get("attachment_storage")
    if not ac:
        return {}
    cfg = {
        "endpoint": str(ac.get("endpoint", "")).rstrip("/"),
        "upload_url": str(ac.get("upload_url", "")),
        "upload_token": str(ac.get("upload_token", "")),
        "local_cache": str(ac.get("local_cache", "")),
    }
    env_token = os.environ.get("YIDA_VPS_UPLOAD_TOKEN", "").strip()
    if env_token:
        cfg["upload_token"] = env_token
    if local_mandatory and not cfg["local_cache"]:
        sys.exit("[配置缺失] attachment_storage.local_cache 未填写")
    return cfg


# ── 附件内容去重索引（P1-5）────────────────────────────────────────
# 索引把「文件内容 md5」映射到「已上传成功的 VPS URL」。
# 同一份内容出现在不同记录/不同文件名时，可直接复用已有 URL，零上传流量。
# 注意：只影响新上传，不改变已写入宜搭的 URL 结构，因此对存量数据完全安全。

def file_md5(path, chunk=1024 * 1024):
    """流式计算文件 md5，内存占用恒定。"""
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def content_index_path(cache_root):
    return Path(cache_root) / "_content_index.json"


def load_content_index(cache_root):
    p = content_index_path(cache_root)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}   # 索引损坏不影响主流程，最多退化为重新上传


def save_content_index(cache_root, index):
    try:
        save_json(content_index_path(cache_root), index, quiet=True)
    except Exception:
        pass


def yida_context(cred, cfg):
    """合并宜搭调用上下文：表单配置(forms/*.json 的 yida 节点)优先，credentials.json 兜底。
    返回 {systemToken, appType, formUuid, userId}"""
    base = dict(cred.get("yida", {}))
    override = {k: v for k, v in (cfg.get("yida") or {}).items() if v and "填入" not in str(v)}
    base.update(override)
    return base


def require_non_placeholder(value, name):
    """校验配置项已填写（非占位符、非空），否则明确报错退出"""
    if value is None or str(value).strip() == "" or "填入" in str(value):
        sys.exit(f"[配置缺失] 请在凭证/表单配置中填写「{name}」，当前仍为占位符或未填写")
    return value


def save_json(path, obj, quiet=False):
    """原子写入 JSON：先写同目录临时文件并 fsync，再 os.replace 原子替换目标。
    避免进程在写入过程中崩溃（断电/Ctrl+C/OOM）导致目标文件被截断为半截 JSON。
    quiet=True 用于高频增量保存（如每批次台账），不打印日志。

    健壮性增强：Windows 上目标文件可能被杀毒/同步工具或编辑器瞬时独占
    （ERROR_SHARING_VIOLATION，Python 报为 PermissionError），或残留只读属性位。
    这里在替换前先清除只读位，并对替换步骤做有限次重试退避，避免瞬时锁打断管线。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    # 临时文件写入也可能在极端情况下被占用，整体重试
    last_err = None
    for attempt in range(1, 4):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # 替换前清除目标只读位（Windows: 置 owner-write 即清 readonly）
            if path.exists():
                try:
                    os.chmod(str(path), 0o666)
                except OSError:
                    pass
            os.replace(str(tmp), str(path))  # 同分区原子替换
            last_err = None
            break
        except (PermissionError, OSError) as e:
            last_err = e
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if attempt < 3:
                time.sleep(0.5 * attempt)  # 0.5s / 1.0s 退避
                # 换一个临时文件名重试，规避上次的残留
                tmp = path.with_name(path.name + f".tmp{os.getpid()}.{attempt}")
                continue
            raise
    if last_err is not None:
        raise last_err
    if not quiet:
        print(f"  已保存: {path}")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
