# -*- coding: utf-8 -*-
"""附件迁移：轻流签名URL → 下载 → 本地缓存 → 上传VPS → 写入宜搭附件字段

阶段: 独立于主管线 00-04，专门处理 AttachmentField（主管线在 03_transform 跳过）
产物: data/attachment_cache/ 本地缓存(幂等)、data/result/<表单>_attachment_result.json(写入台账)

架构:
  轻流 24h 签名 URL → requests.get 下载字节 → 本地缓存 data/attachment_cache/
  → POST https://<你的域名>/upload 上传VPS → 得公网长期 URL
  → 构造宜搭附件数组 [{downloadUrl, name, previewUrl, url, ext}]
  → insertOrUpdate 按 textField_<数据ID>(轻流数据ID) 定位，只写附件字段

用法:
  python 03b_attachment.py 示例表单                                    # 全量，默认 dry-run
  python 03b_attachment.py 示例表单 --commit                            # 真实写入
  python 03b_attachment.py 示例表单 --apply <dataID> --commit           # 单条
  python 03b_attachment.py 示例表单 --limit 10 --commit                 # 小批量
  python 03b_attachment.py 示例表单 --peek --limit 5                    # 只预览不下不写
  python 03b_attachment.py 示例表单 --skip-download                     # 跳过下载(缓存已就绪)
  python 03b_attachment.py 示例表单 --skip-upload                       # 不上传VPS(只用缓存)

安全: 默认 dry-run，必须加 --commit 才真正写宜搭；VPS 上传需 upload_token 不为占位符。
"""
import atexit
import csv
import json
import os
import sys
import time
import hashlib
import argparse
import urllib.parse
import urllib.request
import urllib.error
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
from common import (load_credentials, load_form_config, http_request,
                    get_dingtalk_token, yida_context, load_json, save_json,
                    load_attachment_config, file_md5, load_content_index,
                    save_content_index, DATA_DIR, DINGTALK_API, BASE_DIR)

INSERT_UPDATE_URL = f"{DINGTALK_API}/v2.0/yida/forms/instances/insertOrUpdate"

# ── 全局统计 ──
STATS = {"total": 0, "downloaded": 0, "cached": 0, "uploaded": 0, "vps_hit": 0,
         "content_hit": 0, "skipped_no_att": 0, "skipped_bad_url": 0,
         "skipped_expired": 0, "written": 0, "dry_run": 0, "errors": 0}

# 台账增量落盘间隔（每 N 条写一次，配合 common.save_json 的原子写入）
RESULT_FLUSH_EVERY = 10

# 附件字段 queId 列表由 mapping 表动态读取（mappings/<表单名>_mapping.csv 中 AttachmentField 行）
# 通过 mapping CSV 找 AttachmentField 类组件发现, 不硬编码
ATTACHMENT_QUE_IDS = {}  # {表单名: [queId, ...]}，由 parse_mapping 填充

# 去重键 queId（轻流系统字段 数据ID=-17）
DEDUP_QUE_ID = "-17"

# 文件扩展名白名单（与 VPS 端保持一致）
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".doc", ".docx",
               ".png", ".jpg", ".jpeg", ".zip", ".csv", ".txt",
               ".ppt", ".pptx", ".rar", ".7z", ".bmp", ".gif"}

# 签名 URL 剩余有效时间安全阈值（秒）—— 低于此值告警并跳过
MIN_EXPIRE_SEC = 120  # 2 分钟


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def sanitize_filename(name):
    """清洗文件名：去掉非法字符，截断过长名称"""
    name = name.strip()
    # 去掉路径分隔符、换行等
    bad = '<>:"/\\|?*\r\n\t'
    for ch in bad:
        name = name.replace(ch, "_")
    # 截断（保留扩展名）
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:180] + ext
    return name if name else "unnamed"


def parse_mapping(form_name):
    """从映射 CSV 读取附件字段 queId 和去重键 componentId。
    返回 (att_que_ids: list[str], ded_cid: str, ded_cname: str)
    同时更新 ATTACHMENT_QUE_IDS 缓存。
    """
    global ATTACHMENT_QUE_IDS
    if form_name in ATTACHMENT_QUE_IDS:
        att_ids = ATTACHMENT_QUE_IDS[form_name]
    else:
        att_ids = []
        mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
        if mp.exists():
            with open(mp, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    row = {k.strip(): (v or "").strip() for k, v in row.items()}
                    cn = row.get("componentName", "")
                    qid = row.get("轻流queId", "").strip()
                    if cn == "AttachmentField" and qid and qid != "0":
                        att_ids.append(qid)
        ATTACHMENT_QUE_IDS[form_name] = att_ids

    ded_cid = None
    ded_cname = "TextField"
    mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
    if mp.exists():
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row.get("轻流queId", "").strip() == DEDUP_QUE_ID:
                    ded_cid = row["componentId"]
                    ded_cname = row.get("componentName") or "TextField"
                    break
    return att_ids, ded_cid, ded_cname


def get_scalar(answer):
    """取轻流答案的标量值（兼容 value / values[].value / values[].dataValue）"""
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


def check_expire(url):
    """解析签名 URL 中 qingflow-expire-time，返回剩余秒数。无法解析返回 None。"""
    try:
        q = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(q)
        exp_vals = params.get("qingflow-expire-time")
        if exp_vals:
            return int(exp_vals[0]) - int(time.time())
    except Exception:
        pass
    return None


def build_attachment_payload(items):
    """构造宜搭附件字段格式。
    items: [(vps_url, filename, ext), ...]
    返回: [{downloadUrl, name, previewUrl, url, ext}, ...]
    """
    out = []
    for vps_url, name, ext in items:
        out.append({
            "downloadUrl": vps_url,
            "name": name,
            "previewUrl": vps_url,
            "url": vps_url,
            "ext": ext,
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# 阶段1: 下载（轻流签名 URL → 本地缓存）
# ═══════════════════════════════════════════════════════════════════

DOWNLOAD_CHUNK = 64 * 1024  # 流式下载块大小，避免大文件整体读入内存


def download_attachment(url, cache_path, retry=2):
    """从轻流签名 URL 流式下载附件到本地缓存。
    返回 True 表示下载成功或缓存已命中，False 表示失败。
    幂等：缓存文件已存在(且非空)则跳过下载。
    安全性：先写 .part 临时文件，完整后 os.replace 原子改名 —— 中断产生的半截文件
            不会被下轮误判为"缓存命中"。
    """
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        STATS["cached"] += 1
        return True

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp_path = cache_path + f".part{os.getpid()}"

    for attempt in range(retry + 1):
        total = 0
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as f:
                while True:
                    buf = resp.read(DOWNLOAD_CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    total += len(buf)
            if total == 0:
                raise RuntimeError("empty response")
            os.replace(tmp_path, cache_path)
            print(f"    ↓ 下载 {os.path.basename(cache_path)} ({total // 1024}KB)")
            STATS["downloaded"] += 1
            return True
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            if attempt < retry:
                wait = 2 ** attempt
                print(f"    ⚠ 下载失败(第{attempt+1}次): {e}，{wait}s后重试...")
                time.sleep(wait)
            else:
                print(f"    ✗ 下载失败(重试耗尽): {e}")
                return False
    return False


# ═══════════════════════════════════════════════════════════════════
# 阶段2: 上传（本地缓存 → VPS）
# ═══════════════════════════════════════════════════════════════════

def vps_head_check(endpoint, vps_path, expect_size):
    """HEAD 预检：VPS 上已存在同路径且同大小的文件时，无需再上传整个文件体。
    这是节省 VPS 上行流量的关键 —— 重跑/断点续传场景下可完全避免重复上传。
    返回 (exists: bool, url: str)。任何异常都返回 False（安全回退为正常上传）。
    """
    if not endpoint:
        return False, ""
    url = f"{endpoint}/files/{urllib.parse.quote(vps_path)}"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False, ""
            remote_size = resp.headers.get("Content-Length")
            if remote_size is None or int(remote_size) != int(expect_size):
                return False, ""
            return True, url
    except Exception:
        return False, ""


def upload_to_vps(cache_path, vps_path, storage_cfg, skip_head=False, content_index=None):
    """上传本地缓存文件到 VPS。
    返回 (vps_url, True) 成功；(None, False) 失败。
    三级省流量策略：
      1) 内容 md5 命中本地索引 -> 复用已有 URL，零请求（跨记录/跨文件名去重，P1-5）
      2) HEAD 预检同路径同大小   -> 零文件体流量（P1-4）
      3) 才真正 POST 上传；VPS 端还有同名同大小判重兜底
    """
    upload_url = storage_cfg["upload_url"]
    upload_token = storage_cfg["upload_token"]
    endpoint = storage_cfg["endpoint"]

    if not upload_token or "待" in upload_token or "填入" in upload_token:
        print("    ⊘ 跳过上传：upload_token 未配置（占位符）")
        return None, False

    if not os.path.exists(cache_path):
        print(f"    ✗ 缓存文件不存在: {cache_path}")
        return None, False

    local_size = os.path.getsize(cache_path)

    # ── 1) 内容去重：同内容不同文件名/不同记录，直接复用已有 URL ──
    digest = None
    if content_index is not None:
        try:
            digest = file_md5(cache_path)
        except Exception:
            digest = None
        if digest and digest in content_index:
            reused = content_index[digest]
            print(f"    ⇢ 内容已存在(md5命中，未上传 {local_size // 1024}KB): {reused}")
            STATS["content_hit"] += 1
            return reused, True

    # ── 2) HEAD 预检：省流量的关键一步 ──
    if not skip_head:
        hit, hit_url = vps_head_check(endpoint, vps_path, local_size)
        if hit:
            print(f"    ⇢ VPS已有(HEAD预检命中，未上传 {local_size // 1024}KB): {hit_url}")
            STATS["vps_hit"] += 1
            if content_index is not None and digest:
                content_index[digest] = hit_url
            return hit_url, True

    filename = os.path.basename(cache_path)
    try:
        import requests  # 仅上传用到（因为 urllib 构造 multipart 较繁琐）
        with open(cache_path, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"file": (filename, f)},
                data={"path": vps_path},
                headers={"X-Upload-Token": upload_token},
                timeout=120,
            )
        if resp.status_code == 200:
            data = resp.json()
            vps_url = data.get("url", "")
            if "cached" in data:
                print(f"    ↑ VPS已存在: {vps_url}")
            else:
                print(f"    ↑ 上传成功: {vps_url}")
            STATS["uploaded"] += 1
            if content_index is not None and digest and vps_url:
                content_index[digest] = vps_url
            return vps_url, True
        else:
            print(f"    ✗ 上传失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return None, False
    except ImportError:
        print("    ✗ 需要 requests 库: pip install requests")
        return None, False
    except Exception as e:
        print(f"    ✗ 上传异常: {e}")
        return None, False


# ═══════════════════════════════════════════════════════════════════
# 阶段3: 写入宜搭
# ═══════════════════════════════════════════════════════════════════

def write_to_yida(data_id, att_payload, att_cid, ded_cid, ded_cname,
                  ctx, cfg, cred, token, dry_run):
    """按轻流数据ID定位，只写附件字段到宜搭。
    payload 不含去重键（符合宜搭 insertOrUpdate 限制：去重键不可出现在 formDataJson）。
    返回 True 成功，False 失败。
    """
    body = {
        "appType": ctx["appType"],
        "systemToken": ctx["systemToken"],
        "userId": ctx["userId"],
        "formUuid": ctx["formUuid"],
        "noExecuteExpression": cfg.get("noExecuteExpression", True),
        "searchCondition": json.dumps([{
            "key": ded_cid,
            "value": str(data_id),
            "type": "TEXT",
            "operator": "eq",
            "componentName": ded_cname,
        }], ensure_ascii=False),
        "formDataJson": json.dumps({att_cid: att_payload}, ensure_ascii=False),
        "useAlias": False,
    }

    if dry_run:
        print(f"    [dry-run] 将写入 {len(att_payload)} 个附件到 dataID={data_id}")
        # 打印第一条 URL 样本
        if att_payload:
            print(f"    [dry-run] 样本URL: {att_payload[0]['url'][:100]}...")
        STATS["dry_run"] += 1
        return True

    try:
        resp = http_request(INSERT_UPDATE_URL,
                            headers={"x-acs-dingtalk-access-token": token},
                            body=body, min_interval=0.3)
        if resp.get("success") is False:
            print(f"    ✗ 宜搭返回错误: {json.dumps(resp, ensure_ascii=False)[:300]}")
            STATS["errors"] += 1
            return False
        inst_id = (resp.get("result") or [None])[0]
        print(f"    ✓ 写入成功 instID={inst_id}")
        STATS["written"] += 1
        return True
    except Exception as e:
        print(f"    ✗ 写入异常: {e}")
        STATS["errors"] += 1
        return False


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="附件迁移：轻流→本地缓存→VPS→宜搭")
    ap.add_argument("form", nargs="?", default=None, help="表单名（必填）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条（0=全部）")
    ap.add_argument("--apply", help="指定轻流数据ID(dataID)，只处理这一条")
    ap.add_argument("--peek", action="store_true", help="只预览，不下载不上传不写宜搭")
    ap.add_argument("--skip-download", action="store_true", help="跳过下载（缓存已就绪）")
    ap.add_argument("--skip-upload", action="store_true", help="跳过VPS上传（仅下载+缓存）")
    ap.add_argument("--commit", action="store_true", help="真实写入宜搭（默认 dry-run）")
    ap.add_argument("--force-download", action="store_true", help="强制重新下载（忽略缓存）")
    ap.add_argument("--no-content-dedup", action="store_true",
                    help="关闭内容 md5 去重（默认开启：同内容附件复用已有 VPS URL，零上传）")
    args = ap.parse_args()
    if not args.form:
        sys.exit("用法: python 03b_attachment.py <表单名> [--peek|--commit] [--limit N]")

    # ── 加载配置 ──
    cred = load_credentials()
    cfg = load_form_config(args.form)
    ctx = yida_context(cred, cfg)
    for k in ("appType", "systemToken", "userId", "formUuid"):
        if not ctx.get(k):
            sys.exit(f"[配置缺失] 宜搭 {k} 未填写")

    storage_cfg = load_attachment_config(cred)
    local_cache_root = BASE_DIR / (storage_cfg.get("local_cache", "data/attachment_cache"))

    # ── 解析映射表 ──
    att_que_ids, ded_cid, ded_cname = parse_mapping(args.form)
    if not att_que_ids:
        sys.exit(f"[错误] 表单「{args.form}」无附件字段（mapping 中未找到 AttachmentField）")
    if not ded_cid:
        sys.exit(f"[错误] 表单「{args.form}」未找到去重键（queId={DEDUP_QUE_ID}）")

    # 多附件字段支持：代码按 mapping 表发现的全部 AttachmentField 逐一处理
    print(f"[配置] 表单={args.form}  附件字段数={len(att_que_ids)}  去重键={ded_cid}")
    for aq in att_que_ids:
        # 找对应宜搭组件ID
        mp = BASE_DIR / "mappings" / f"{args.form}_mapping.csv"
        att_cid_for_q = None
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row.get("轻流queId", "").strip() == aq:
                    att_cid_for_q = row.get("componentId", "")
                    print(f"  附件 queId={aq} → componentId={att_cid_for_q}")
                    break

    # ── 加载轻流原始数据 ──
    raw_path = DATA_DIR / "raw" / f"{args.form}_raw.json"
    if not raw_path.exists():
        sys.exit(f"[错误] 原始数据不存在: {raw_path}（请先跑 01_fetch_qingflow.py）")
    raw = load_json(raw_path)
    records = raw if isinstance(raw, list) else raw.get("result", {}).get("result", [])

    # ── 模式提示 ──
    if args.peek:
        print("[模式] PEEK — 仅预览，不下载不上传不写宜搭")
    elif args.commit:
        print("[模式] COMMIT — 将真实写入宜搭")
        if args.skip_upload:
            print("  ⚠ --skip-upload 开启：不会上传 VPS，载荷中 url 将为本地路径")
    else:
        print("[模式] DRY-RUN — 会下载+上传，但不写宜搭（加 --commit 才写）")

    if args.skip_download:
        print("[模式] 跳过下载，仅使用本地缓存")
    if args.skip_upload:
        print("[模式] 跳过 VPS 上传")
    if args.force_download:
        print("[模式] 强制重新下载（忽略缓存）")

    # ── 预处理：生成 token（用于宜搭写入） ──
    token = None
    if args.commit and not args.peek:
        try:
            token = get_dingtalk_token(cred)
        except Exception as e:
            print(f"⚠ 获取钉钉 token 失败: {e}，将跳过写入")
            args.commit = False

    # ── 内容去重索引（P1-5）：md5 -> 已上传 VPS URL ──
    content_index = {} if args.no_content_dedup else load_content_index(local_cache_root)
    if content_index:
        print(f"[内容索引] 已加载 {len(content_index)} 条 md5→URL 映射（同内容附件不再重复上传）")

    # ── 逐个附件字段遍历 ──
    result_log = []  # 写入台账
    result_path = DATA_DIR / "result" / f"{args.form}_attachment_result.json"

    _flushed = {"n": -1}

    def flush_result(force=False):
        """P1-6: 增量落盘台账。崩溃/中断后已完成部分不丢失，重跑可据此续传。"""
        if args.peek or not result_log:
            return
        if not force and len(result_log) % RESULT_FLUSH_EVERY != 0:
            return
        if _flushed["n"] == len(result_log):
            return
        save_json(result_path, result_log, quiet=not force)
        _flushed["n"] = len(result_log)

    # Ctrl+C / 异常退出时也保底落盘一次（台账 + 内容索引）
    atexit.register(lambda: flush_result(force=True))
    if not args.no_content_dedup:
        atexit.register(lambda: save_content_index(local_cache_root, content_index))

    for att_que_id in att_que_ids:
        # 找对应宜搭组件ID
        att_cid = None
        mp = BASE_DIR / "mappings" / f"{args.form}_mapping.csv"
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                if row.get("轻流queId", "").strip() == att_que_id:
                    att_cid = row.get("componentId", "")
                    break
        if not att_cid:
            print(f"[跳过] queId={att_que_id} 无对应宜搭组件ID")
            continue

        print(f"\n{'='*60}")
        print(f"处理附件字段: queId={att_que_id}  componentId={att_cid}")
        print(f"{'='*60}")

        for rec in records:
            if args.limit > 0 and STATS["total"] >= args.limit:
                break

            # 取去重键 dataID
            answers = {str(a.get("queId")): a for a in rec.get("answers", [])}
            data_id = get_scalar(answers.get(DEDUP_QUE_ID, {}))
            if not data_id:
                continue
            if args.apply and str(data_id) != str(args.apply):
                continue

            # 取附件数据
            att_ans = answers.get(att_que_id)
            if not att_ans:
                STATS["skipped_no_att"] += 1
                continue
            att_vals = att_ans.get("values") or []
            if not att_vals:
                STATS["skipped_no_att"] += 1
                continue

            STATS["total"] += 1
            print(f"\n── 记录 #{STATS['total']}  dataID={data_id}  附件数={len(att_vals)} ──")

            # ── peek 模式：只打印预览 ──
            if args.peek:
                for v in att_vals:
                    url = v.get("value") or v.get("dataValue") or ""
                    name = (v.get("otherInfo") or "").strip() or url.rsplit("/",1)[-1].split("?")[0]
                    exp = check_expire(url) if url else None
                    exp_str = f"  有效期剩余: {exp//60}分钟" if exp and exp > 0 else "  ⚠ 已过期或无有效期"
                    print(f"  [{name}] {url[:120]}...{exp_str}")
                continue

            # ── 处理每个附件 ──
            payload_items = []  # [(vps_url, filename, ext), ...]
            record_ok = True

            for v in att_vals:
                qf_url = v.get("value") or v.get("dataValue") or ""
                if not qf_url:
                    print(f"  ⊘ 跳过空URL的附件")
                    STATS["skipped_bad_url"] += 1
                    record_ok = False
                    continue

                # 解析文件名
                raw_name = (v.get("otherInfo") or "").strip()
                if not raw_name:
                    # 从 URL 中提取
                    raw_name = qf_url.rsplit("/", 1)[-1].split("?")[0]
                name = sanitize_filename(raw_name)
                ext = os.path.splitext(name)[1].lower()

                # 检查 URL 有效期
                exp_left = check_expire(qf_url)
                if exp_left is not None and exp_left < MIN_EXPIRE_SEC:
                    print(f"  ⚠ [{name}] URL 已/将过期(剩余{exp_left}秒)，跳过。请先重跑 01 拉新鲜URL")
                    STATS["skipped_expired"] += 1
                    record_ok = False
                    continue

                # 本地缓存路径
                cache_rel = f"{args.form}/{data_id}/{att_que_id}/{name}"
                cache_path = os.path.join(str(local_cache_root), cache_rel)

                # ── 阶段1: 下载 ──
                if not args.skip_download:
                    if args.force_download and os.path.exists(cache_path):
                        os.remove(cache_path)
                    if not download_attachment(qf_url, cache_path):
                        record_ok = False
                        continue
                elif not os.path.exists(cache_path):
                    print(f"    ✗ 缓存不存在且 --skip-download 开启: {cache_path}")
                    record_ok = False
                    continue

                # ── 阶段2: 上传 VPS ──
                if not args.skip_upload and storage_cfg.get("upload_token"):
                    vps_rel = cache_rel.replace("\\", "/")
                    vps_url, ok = upload_to_vps(
                        cache_path, vps_rel, storage_cfg,
                        content_index=None if args.no_content_dedup else content_index)
                    if not ok:
                        record_ok = False
                        continue
                    final_url = vps_url
                else:
                    # 未上传 VPS（--skip-upload 或 token 未配置）
                    final_url = f"file://{cache_path.replace(os.sep, '/')}"
                    if args.skip_upload:
                        print(f"    ⊘ 跳过上传(--skip-upload): {name}")
                    else:
                        print(f"    ⊘ 跳过上传(token未配置): {name}")

                payload_items.append((final_url, name, ext))

            if not payload_items:
                print(f"  ⊘ 无可用附件，跳过写入")
                continue
            if not record_ok:
                print(f"  ⚠ 部分附件处理失败，跳过该记录写入")
                continue

            # ── 阶段3: 构造 payload + 写入宜搭 ──
            att_payload = build_attachment_payload(payload_items)
            success = write_to_yida(data_id, att_payload, att_cid,
                                    ded_cid, ded_cname, ctx, cfg, cred, token,
                                    dry_run=not args.commit or args.peek)
            result_log.append({
                "dataID": str(data_id),
                "attachments": len(payload_items),
                "urls": [u for u, _, _ in payload_items],
                "success": success,
            })
            flush_result()

    # ── 保存台账（最终落盘） ──
    flush_result(force=True)

    # ── 统计总结 ──
    print(f"\n{'='*60}")
    print(f"迁移完成统计")
    print(f"{'='*60}")
    print(f"  扫描记录数: {STATS['total'] + STATS['skipped_no_att']}")
    print(f"  含附件记录: {STATS['total']}")
    print(f"  下载(新增): {STATS['downloaded']}")
    print(f"  本地缓存命中: {STATS['cached']}")
    print(f"  VPS已有(HEAD预检命中,零上传): {STATS['vps_hit']}")
    print(f"  内容去重命中(md5,零上传):    {STATS['content_hit']}")
    print(f"  上传VPS成功: {STATS['uploaded']}")
    print(f"  写入宜搭:     {STATS['written']}")
    print(f"  Dry-run预览: {STATS['dry_run']}")
    print(f"  跳过(无附件): {STATS['skipped_no_att']}")
    print(f"  跳过(坏URL):  {STATS['skipped_bad_url']}")
    print(f"  跳过(过期):   {STATS['skipped_expired']}")
    print(f"  错误:        {STATS['errors']}")
    if not args.commit and not args.peek:
        print(f"\n  ⚠ 当前为 DRY-RUN 模式，加 --commit 才会真实写入宜搭")


if __name__ == "__main__":
    main()
