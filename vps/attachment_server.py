# -*- coding: utf-8 -*-
"""附件上传服务 —— 监听 127.0.0.1:8000，由 Caddy 反代 /upload

部署位置: VPS 的 D:\\yida-svc\\attachment_server.py
本文件是版本受控的权威版本，改动后同步到 VPS 并重启 YidaUpload 服务。

设计要点:
  1. 幂等：同路径 + 同大小视为已存在，直接返回已有 URL（不重复写盘）。
  2. 省流量：客户端应先对 {DOMAIN}/files/<path> 发 HEAD 预检，命中则完全不发文件体。
     本服务的幂等只能省"写盘"，省不了"上行带宽"，因此 HEAD 预检在客户端侧最关键。
  3. 内存安全：绝不把上传文件整体 read() 进内存，一律用 content_length / 流式写入。
  4. 大小限制：MAX_FILE_SIZE 通过 Flask MAX_CONTENT_LENGTH 强制 + 流式写入时二次兜底。
"""
import os
import sys
import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from flask import Flask, request, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

# ===== 配置（优先级：环境变量 > config/credentials.json 的 attachment_storage 段） =====
import json


def _load_attachment_cfg():
    """回退配置源：与本文件同级的 config/ 或上级 config/ 中的 credentials.json。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "config", "credentials.json"),
                 os.path.join(here, "..", "config", "credentials.json")):
        try:
            with open(cand, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("attachment_storage") or {}
        except Exception:
            continue
    return {}


_att_cfg = _load_attachment_cfg()
ROOT = os.environ.get("YIDA_FILES_ROOT") or r"D:\yida-attachments"
UPLOAD_TOKEN = (os.environ.get("YIDA_VPS_UPLOAD_TOKEN")
                or os.environ.get("YIDA_UPLOAD_TOKEN")
                or _att_cfg.get("upload_token") or "待配置")
DOMAIN = (os.environ.get("YIDA_FILES_DOMAIN") or _att_cfg.get("endpoint") or "https://待配置").rstrip("/")
if UPLOAD_TOKEN == "待配置" or DOMAIN == "https://待配置":
    print("错误: 未配置附件存储服务（UPLOAD_TOKEN / DOMAIN）。", file=sys.stderr)
    print("请设置环境变量 YIDA_VPS_UPLOAD_TOKEN / YIDA_FILES_DOMAIN，", file=sys.stderr)
    print("或提供 config/credentials.json（attachment_storage 段），然后重新启动。", file=sys.stderr)
    sys.exit(1)
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".doc", ".docx",
               ".png", ".jpg", ".jpeg", ".zip", ".csv", ".txt",
               ".ppt", ".pptx", ".rar", ".7z", ".bmp", ".gif"}
MAX_FILE_SIZE = 50 * 1024 * 1024      # 单文件上限 50MB
COPY_CHUNK = 1024 * 1024              # 流式写入块大小 1MB

app = Flask(__name__)
# P1-8: 由 Werkzeug 在解析请求体阶段就强制上限，超限直接 413，不会写盘也不会撑爆内存。
# 留 2MB 余量给 multipart 边界与表单字段。
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE + 2 * 1024 * 1024


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _safe_dest(rel):
    """把相对路径解析为 ROOT 下的绝对路径，并防止 .. 越权逃逸。"""
    dest = os.path.abspath(os.path.join(ROOT, rel))
    root_abs = os.path.abspath(ROOT)
    if not (dest == root_abs or dest.startswith(root_abs + os.sep)):
        return None
    return dest


def _stream_save(fileobj, dest):
    """流式写入临时文件后原子改名。返回写入字节数；超限抛 RequestEntityTooLarge。
    P1-9: 全程不做 fileobj.read() 全量读取，内存占用恒定为 COPY_CHUNK。
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + f".part{os.getpid()}"
    total = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                buf = fileobj.read(COPY_CHUNK)
                if not buf:
                    break
                total += len(buf)
                if total > MAX_FILE_SIZE:
                    raise RequestEntityTooLarge()
                out.write(buf)
        os.replace(tmp, dest)
        return total
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


@app.post("/upload")
def upload():
    """接收文件上传，返回公网 URL。已存在同路径+同大小文件则跳过写盘。"""
    # 鉴权
    if request.headers.get("X-Upload-Token") != UPLOAD_TOKEN:
        log(f"403 无权限: {request.remote_addr}")
        return jsonify(err="forbidden"), 403

    # 校验路径
    rel = request.form.get("path", "").replace("\\", "/").strip("/")
    if not rel or ".." in rel.split("/"):
        return jsonify(err="bad path"), 400

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(err="no file"), 400

    # 校验扩展名
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(err=f"ext blocked: {ext}"), 400

    dest = _safe_dest(rel)
    if dest is None:
        return jsonify(err="path escapes root"), 400

    # P1-8: 声明的大小就超限时提前拒绝，不必等到写盘
    declared = f.content_length or request.content_length or 0
    if declared and declared > MAX_FILE_SIZE + 2 * 1024 * 1024:
        log(f"413 超过大小上限 {rel} declared={declared}")
        return jsonify(err="file too large", limit=MAX_FILE_SIZE), 413

    # P1-9: 幂等检查改用 content_length，不再把整个文件读入内存
    if os.path.exists(dest):
        existing_size = os.path.getsize(dest)
        if declared and _multipart_body_matches(declared, existing_size):
            log(f"SKIP(已存在) {rel}")
            return jsonify(url=f"{DOMAIN}/files/{rel}", cached=True)

    try:
        size = _stream_save(f.stream, dest)
    except RequestEntityTooLarge:
        log(f"413 写入中超限 {rel}")
        return jsonify(err="file too large", limit=MAX_FILE_SIZE), 413
    except Exception as e:
        log(f"ERR 写入失败 {rel}: {e}")
        return jsonify(err=f"write failed: {e}"), 500

    log(f"OK {rel}  ({size} bytes)")
    return jsonify(url=f"{DOMAIN}/files/{rel}", size=size)


def _multipart_body_matches(declared, existing_size):
    """multipart 中单个 part 的 content_length 通常等于文件真实大小；
    若客户端未提供则 declared 为整个请求体大小，会略大于文件本身。
    这里只在两者完全相等时才认定命中，宁可多传一次也不写错文件。"""
    return int(declared) == int(existing_size)


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_e):
    return jsonify(err="file too large", limit=MAX_FILE_SIZE), 413


@app.get("/upload/health")
def health():
    return jsonify(status="ok", root=ROOT, exists=os.path.isdir(ROOT),
                   maxFileSize=MAX_FILE_SIZE)


@app.get("/upload")
def upload_get():
    """GET /upload 用于验证反代是否正常"""
    return jsonify(status="ok", method="GET")


if __name__ == "__main__":
    # 开发模式直接跑（生产用 waitress：python -m waitress --listen=127.0.0.1:8000 attachment_server:app）
    app.run(host="127.0.0.1", port=8000, debug=False)
