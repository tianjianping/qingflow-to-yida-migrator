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


def load_credentials():
    with open(CONFIG_DIR / "credentials.json", encoding="utf-8") as f:
        cred = json.load(f)
    for env_key, (section, field) in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            cred.setdefault(section, {})[field] = val
    return cred


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


def http_request(url, method="POST", headers=None, body=None, min_interval=0.25, max_retry=3):
    """带限速与重试的 HTTP 请求，body 为 dict 时自动转 JSON"""
    global _last_request_ts
    headers = dict(headers or {})
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
