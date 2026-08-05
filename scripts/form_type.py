# -*- coding: utf-8 -*-
"""宜搭表单类型自动探测：普通表单 / 流程表单

宜搭表单分为两类，数据读写接口完全不同：
  - 普通表单（单据/普通表单）: forms/instances/*（batchSave / search / insertOrUpdate）
  - 流程表单（审批流程表单）: processes/instances/*（start / 列表 / PUT 更新）

探测优先级:
  1. 表单 config 显式配置 formType: "normal" | "process"（最高优先；写入 config/forms/<表单>.json 即可）
  2. 本地缓存 data/raw/<表单>_form_type.json（TTL 30 分钟；force=True / --force 忽略缓存重探）
  3. 接口探测:
     a. GET /v1.0/yida/forms 表单列表返回的 formType 字段（取值 "0"=普通表单 "1"=流程表单）
     b. POST /v1.0/yida/processes/instances 探测流程接口（pageSize=1）是否可访问
  探测失败默认回落 normal（普通表单），保证老管线行为不变。

用法:
  from form_type import detect_form_type
  ft, src = detect_form_type("示例表单")                 # -> ("process", "probe")
  ft, src = detect_form_type("示例表单", force=True)     # 忽略缓存重新探测
  python form_type.py 示例表单 [--force] [--quiet]
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (load_credentials, load_form_config, http_request,
                    yida_context, list_yida_forms, get_dingtalk_token,
                    DINGTALK_API, DATA_DIR)

TYPE_CACHE_TTL = 1800   # 缓存有效期：30 分钟
FORM_TYPE_LABEL = {"normal": "普通表单", "process": "流程表单"}


def detect_form_type(form_name, force=False, verbose=True):
    """返回 (form_type, source)。form_type ∈ {"normal", "process"}。

    流程表单请先在 config/forms/<表单>.json 配置 formType: "process"，
    并视情况配置 processCode（不配则使用表单默认流程）。
    """
    cfg = load_form_config(form_name)
    # 1) config 显式覆盖
    ft = str(cfg.get("formType") or "").strip().lower()
    if ft in ("normal", "process"):
        if verbose:
            print(f"[表单类型] {form_name}: {FORM_TYPE_LABEL[ft]}（来源 config.formType）")
        return ft, "config"
    if ft and ft != "auto":
        print(f"[警告] config.formType 取值无法识别: {ft!r}（可选 normal / process / auto），按自动探测处理")
    # 2) 本地缓存
    cache = DATA_DIR / "raw" / f"{form_name}_form_type.json"
    if cache.exists() and not force:
        try:
            obj = json.loads(cache.read_text(encoding="utf-8"))
            age = datetime.now().timestamp() - datetime.fromisoformat(str(obj.get("detectedAt", ""))).timestamp()
            if 0 <= age < TYPE_CACHE_TTL and obj.get("formType") in ("normal", "process"):
                if verbose:
                    print(f"[表单类型] {form_name}: {FORM_TYPE_LABEL[obj['formType']]}"
                          f"（来源 缓存，{int(age)}s 前探测）")
                return obj["formType"], "cache"
        except Exception:
            pass
    # 3) 接口探测
    ft, src = _probe_form_type(form_name, verbose=verbose)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "formType": ft, "source": src,
            "detectedAt": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    if verbose:
        print(f"[表单类型] {form_name}: {FORM_TYPE_LABEL[ft]}（来源 {src}）")
    return ft, src


def _probe_form_type(form_name, verbose=True):
    """接口探测，返回 (form_type, source)。"""
    try:
        cred = load_credentials()
    except Exception:
        cred = {}
    cfg = load_form_config(form_name)
    ctx = yida_context(cred, cfg)
    if not (ctx.get("formUuid") and ctx.get("systemToken") and ctx.get("appType") and ctx.get("userId")):
        if verbose:
            print("[警告] 缺少宜搭凭证/formUuid，无法探测表单类型，默认按「普通表单」处理")
        return "normal", "fallback"
    # 3a) 表单列表接口直接返回 formType
    try:
        r = list_yida_forms(cred)
        if r.get("ok"):
            for f in r.get("forms") or []:
                if str(f.get("formUuid", "")).strip().upper() == str(ctx["formUuid"]).strip().upper():
                    t = str(f.get("formType", "") or "").strip()
                    if t == "1":
                        return "process", "formList"
                    if t == "0":
                        return "normal", "formList"
                    break
    except Exception as e:
        if verbose:
            print(f"[探测] 表单列表接口不可用（{e}），改用流程接口探测")
    # 3b) 流程实例列表接口探测
    verdict = _probe_process_api(ctx, cred, verbose=verbose)
    if verdict is True:
        return "process", "probe"
    if verdict is False:
        return "normal", "probe"
    return "normal", "fallback"


def _probe_process_api(ctx, cred, verbose=True):
    """POST /v1.0/yida/processes/instances（pageSize=1）探测流程接口。
    返回 True=流程表单 / False=普通表单 / None=无法判定。"""
    url = f"{DINGTALK_API}/v1.0/yida/processes/instances"
    body = {
        "appType": ctx["appType"], "systemToken": ctx["systemToken"],
        "userId": ctx["userId"], "formUuid": ctx["formUuid"],
        "pageNumber": 1, "pageSize": 1, "searchFieldJson": {},
    }
    try:
        token = get_dingtalk_token(cred)
        resp = http_request(url, headers={"x-acs-dingtalk-access-token": token},
                            body=body, min_interval=0.3)
    except Exception as e:
        if verbose:
            print(f"[探测] 流程实例接口调用失败（{e}），无法判定表单类型")
        return None
    if not isinstance(resp, dict):
        return None
    if resp.get("success") is False:
        msg = f"{resp.get('code') or ''} {resp.get('message') or ''}"
        low = str(msg).lower()
        # 权限不足（Forbidden/PermissionDenied）无法判定，不能当作“非流程表单”
        if "forbidden" in low or "permission" in low or "accessdenied" in low \
                or "未开通" in msg or "权限" in msg:
            if verbose:
                print(f"[探测] 流程接口权限不足（{msg[:200]}），无法判定表单类型")
            return None
        if "form" in low or "process" in low or "notexist" in low or "不存在" in msg:
            return False   # 明确的“表单不存在/非流程表单” -> 普通表单
        if verbose:
            print(f"[探测] 流程接口返回未知错误（{msg[:200]}），无法判定表单类型")
        return None        # 权限类等无法判定
    return True            # 流程接口可用 -> 流程表单


def main():
    args = [a for a in sys.argv[1:]]
    quiet = "--quiet" in args
    force = "--force" in args
    names = [a for a in args if not a.startswith("--")]
    if not names:
        sys.exit("用法: python form_type.py <表单配置名> [--force] [--quiet]")
    for n in names:
        ft, src = detect_form_type(n, force=force, verbose=not quiet)
        print(f"结果: {n} = {FORM_TYPE_LABEL[ft]}（{ft}，来源 {src}）")


if __name__ == "__main__":
    main()
