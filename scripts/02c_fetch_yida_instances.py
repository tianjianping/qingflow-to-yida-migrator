# -*- coding: utf-8 -*-
"""步骤 2.5 / 对比宜搭存量：让宜搭真实数据成为「存在性」权威。

背景: 之前 04 用本地 result.json 判断"该记录是否已迁移"，一旦用户在宜搭里手动
清空/删除数据，result.json 仍记着旧实例ID，就会误判"已存在"而跳过 —— 导致
清空后只跑少数记录、其余永久缺失。

[2026-07-31 修复] 存在性对账从「按台账实例ID 查询」改为「全量扫描宜搭实例、按轻流数据ID 匹配」。
  旧实现只查询 result.json 里记录的实例ID，一旦宜搭数据经历过重建/外部导入
  （实例ID 整体变化但数据ID 字段保留），台账实例ID 会几乎全部查不到，导致 02d
  把真实存在的数据误判为「缺失 -> 新建」，04 执行时重复创建大量记录。
  宜搭实例ID 是一次性生成的，不具备跨系统稳定性；轻流数据ID(queId=-17)才是
  跨系统唯一业务键。因此本步改为全量分页扫描宜搭实例，提取每条实例的数据ID，
  以「轻流源 applyId ∈ 宜搭数据ID集合」作为存在性判定，天然覆盖外部导入/重建场景。

本步调用宜搭「查询表单实例列表」接口分页拉取全部实例，构建数据ID 索引。后续 04 据此：
  - 记录从未迁移(数据ID 不在宜搭)        -> 新建
  - 数据ID 在宜搭已查不到(被清空/删除)    -> 视为缺失，新建(自动重建)
  - 数据ID 存在 且 源未变                 -> 跳过(省接口)
  - 数据ID 存在 且 源变了                 -> 更新(insertOrUpdate 按 dataID 定位)

接口文档: 需要的API及辅助说明文件/宜搭查询表单实例列表.md
  POST /v1.0/yida/forms/instances/search
  请求体: formUuid, appType, systemToken, userId, currentPage, pageSize, searchCondition
  响应: data[].formInstanceId / instanceValue
用法:
  python 02c_fetch_yida_instances.py <表单配置名>                  # 拉取并对比，输出存量文件
  python 02c_fetch_yida_instances.py <表单配置名> --allow-partial  # 允许带未解决页继续（不阻断管线）

可靠性说明:
  若某页实例扫描失败（网络/限速），这些实例的「存在性未知」，绝不能当作"不存在"，
  否则 02d 会把对应源记录标为新建 -> 重复创建。本步骤会做页级重试，仍失败则
  写入 unresolved 并以退出码 2 阻断管线；02d 读到 unresolved 会把相关记录标为
  deferred（本轮不写）。网络恢复后重跑本步骤即可自动消解。

重复数据提示:
  若同一数据ID 在宜搭存在多条实例（如历史残留 + 重建导入并存），本步会打印告警，
  didToInst 只保留其中一条（用于存在性判断，update 走 insertOrUpdate 按数据ID 定位
  不影响正确性）。建议人工在宜搭清理重复实例。
"""
import csv
import json
import sys
import time
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
from common import (load_credentials, load_form_config, http_request, load_json,
                    save_json, get_dingtalk_token, yida_context, DATA_DIR, DINGTALK_API, BASE_DIR,
                    load_sync_state)
from form_type import detect_form_type, FORM_TYPE_LABEL

# 普通表单: 查询/创建/更新走 forms/instances/*；流程表单走 processes/instances/*
SCAN_URL = f"{DINGTALK_API}/v1.0/yida/forms/instances/search"
PROCESS_LIST_URL = f"{DINGTALK_API}/v1.0/yida/processes/instances"
PAGE_SIZE = 100      # 每页实例数上限(宜搭列表接口)
MAX_PAGES = 500      # 分页上限保护(500页=5万条，超出即视为异常)
SCAN_RETRY = 3       # 单页扫描失败后的额外重试次数（http_request 内部已有重试，此处为页级兜底）


def find_dedup_field(form_name):
    """从映射表找 轻流 queId=-17(数据ID) 对应的宜搭组件，用于存在性对账。"""
    mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
    if not mp.exists():
        return None
    with open(mp, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("轻流queId", "").strip() == "-17":
                return (row["componentId"], row.get("componentName") or "TextField")
    return None


def extract_data_id(inst, dedup_cid):
    """从单个宜搭实例按去重键组件取回 dataID 值。

    兼容两种响应结构:
      - 普通表单实例: formData: {cid: value} / instanceValue(JSON 字符串)
      - 流程表单实例: 扁平 dict（processInstanceId + cid: value 直接平铺）
    """
    if not dedup_cid:
        return None
    fd = inst.get("formData")
    if isinstance(fd, dict) and dedup_cid in fd and fd[dedup_cid] not in (None, ""):
        return str(fd[dedup_cid])
    if dedup_cid in inst and inst[dedup_cid] not in (None, ""):
        return str(inst[dedup_cid])
    iv = inst.get("instanceValue")
    if isinstance(iv, str) and iv:
        try:
            for comp in json.loads(iv):
                if comp.get("fieldId") == dedup_cid:
                    fdata = comp.get("fieldData") or {}
                    if "value" in fdata and fdata["value"] not in (None, ""):
                        return str(fdata["value"])
                    if "text" in fdata and fdata["text"] not in (None, ""):
                        return str(fdata["text"])
        except Exception:
            pass
    return None


def scan_all_instances(ctx, token, form_type="normal"):
    """全量分页扫描宜搭实例。

    普通表单走 POST /v1.0/yida/forms/instances/search（currentPage 分页）；
    流程表单走 POST /v1.0/yida/processes/instances（pageNumber 分页，实例键为 processInstanceId）。

    返回 (insts: list[dict], unresolved: int, last_err: str|None)
    insts 为成功拉取的实例列表；unresolved 为扫描失败的页数（存在性未知）。
    """
    insts = []
    unresolved_pages = 0
    last_err = None
    page = 1
    while page <= MAX_PAGES:
        if form_type == "process":
            url = PROCESS_LIST_URL
            body = {
                "formUuid": ctx["formUuid"],
                "appType": ctx["appType"],
                "systemToken": ctx["systemToken"],
                "userId": ctx["userId"],
                "pageNumber": page,
                "pageSize": PAGE_SIZE,
                "searchFieldJson": {},
            }
        else:
            url = SCAN_URL
            body = {
                "formUuid": ctx["formUuid"],
                "appType": ctx["appType"],
                "systemToken": ctx["systemToken"],
                "userId": ctx["userId"],
                "currentPage": page,
                "pageSize": PAGE_SIZE,
                "searchCondition": "[]",  # 空条件 = 拉全量（实测必需，否则返回空）
            }
        resp = None
        for attempt in range(1, SCAN_RETRY + 1):
            try:
                resp = http_request(url,
                                    headers={"x-acs-dingtalk-access-token": token},
                                    body=body, min_interval=0.3)
                break
            except Exception as e:
                last_err = e
                if attempt < SCAN_RETRY:
                    wait = 2 ** attempt
                    print(f"  [扫描失败] 第{page}页 第{attempt}次: {e}，{wait}s 后重试")
                    time.sleep(wait)
        if resp is None:
            print(f"  [扫描失败·未解决] 第{page}页 重试耗尽: {last_err}（该页实例存在性未知）")
            unresolved_pages += 1
            # 不确定缺了哪些实例 -> 直接中断扫描，交由上游判定未解决
            break
        data = resp.get("data") or (resp.get("result") or {}).get("data") or []
        insts.extend(data)
        if len(data) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)
    return insts, unresolved_pages, last_err


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 02c_fetch_yida_instances.py <表单配置名> [--allow-partial]"
                 " [--form-type normal|process|auto] [--force]")
    form_name = sys.argv[1]
    # 默认：有页存在性未知时以非零码退出，阻断管线（防止下游重复创建）
    allow_partial = "--allow-partial" in sys.argv
    # 表单类型: auto=自动探测（config 覆盖 > 缓存 > 接口探测），也可显式指定
    type_override = "auto"
    if "--form-type" in sys.argv:
        idx = sys.argv.index("--form-type")
        if idx + 1 >= len(sys.argv):
            sys.exit("[参数错误] --form-type 需要一个取值: normal | process | auto")
        type_override = sys.argv[idx + 1].strip().lower()
    if type_override not in ("normal", "process", "auto"):
        sys.exit(f"[参数错误] --form-type 取值无效: {type_override}（可选 normal / process / auto）")
    force_type = "--force" in sys.argv

    cred = load_credentials()
    cfg = load_form_config(form_name)
    ctx = yida_context(cred, cfg)
    for k in ("appType", "systemToken", "userId", "formUuid"):
        if not ctx.get(k):
            sys.exit(f"[配置缺失] 宜搭 {k} 未填写（请在 credentials.json 或 forms/{form_name}.json 配置）")

    if type_override == "auto":
        form_type, type_src = detect_form_type(form_name, force=force_type)
    else:
        form_type, type_src = type_override, "cli"
    print(f"[表单类型] {form_name}: {FORM_TYPE_LABEL[form_type]}（来源 {type_src}）"
          + ("" if form_type == "normal" else " —— 按流程表单接口扫描"))
    inst_key = "processInstanceId" if form_type == "process" else "formInstanceId"

    dedup = find_dedup_field(form_name)
    dedup_cid = dedup[0] if dedup else None
    if dedup_cid:
        print(f"[去重键] 存在性对账键 {dedup_cid}({dedup[1]})")
    else:
        print("[警告] 未找到 轻流queId=-17(数据ID) 对应组件，将无法按数据ID 对账（全部源记录将被判为新建）")

    token = get_dingtalk_token(cred)
    inst_path = DATA_DIR / "raw" / f"{form_name}_yida_instances.json"
    # C3: 01 增量拉取无变更（lastPullCount==0）时，轻流源数据未变化，
    # 宜搭存量也无需重扫 —— 复用既有存量文件（保持 mtime 不变），
    # 与 02d 的 inputFingerprint 快速跳过形成闭环，整段准备零扫描。
    if "--full" not in sys.argv and inst_path.exists():
        try:
            st = load_sync_state(form_name)
        except Exception:
            st = None
        if st and st.get("lastPullCount") == 0:
            print("[增量] 轻流源数据无变更（增量拉取 0 条），复用既有宜搭存量，跳过全量扫描")
            return
    print("[扫描] 全量分页拉取宜搭实例（按数据ID 对账，不依赖本地台账）...")
    insts, unresolved_pages, last_err = scan_all_instances(ctx, token, form_type=form_type)
    print(f"[扫描] 拉到 {len(insts)} 条宜搭实例")

    existing = {}       # instId -> dataID
    did_to_inst = {}    # 轻流数据ID -> 实例ID（重复数据ID 保留第一条并告警）
    dup_dids = []       # 同一数据ID 出现多条实例的清单
    for it in insts:
        inst_id = it.get(inst_key)
        if not inst_id:
            continue
        did = extract_data_id(it, dedup_cid) if dedup_cid else None
        existing[inst_id] = did
        if did:
            if did in did_to_inst:
                dup_dids.append((did, did_to_inst[did], inst_id))
            else:
                did_to_inst[did] = inst_id

    unresolved = [] if unresolved_pages == 0 else [f"page_unresolved:{unresolved_pages}"]
    save_json(DATA_DIR / "raw" / f"{form_name}_yida_instances.json",
              {"existing": existing, "didToInst": did_to_inst,
               "count": len(existing), "queried": len(insts),
               "unresolved": unresolved, "partial": bool(unresolved)})
    print(f"[存量对比] 宜搭现有 {len(existing)} 条实例，其中 {len(did_to_inst)} 个数据ID 可匹配轻流")
    print(f"            已写入 data/raw/{form_name}_yida_instances.json")
    if dup_dids:
        print(f"[警告] {len(dup_dids)} 个数据ID 在宜搭存在多条实例（历史残留与重建导入并存）：")
        for did, old_inst, new_inst in dup_dids[:10]:
            print(f"        {did}: {old_inst} / {new_inst}")
        if len(dup_dids) > 10:
            print(f"        ... 共 {len(dup_dids)} 组。didToInst 仅保留第一条，建议在宜搭人工清理重复实例。")
    if unresolved:
        print(f"[严重] {unresolved_pages} 页实例存在性未知（网络/限速导致扫描失败，末次错误: {last_err}）。")
        print(f"       02d 会把相关记录标为 deferred（本轮不写），以避免重复创建。")
        print(f"       网络恢复后重跑本步骤即可自动消解。")
        if not allow_partial:
            sys.exit(2)


if __name__ == "__main__":
    main()
