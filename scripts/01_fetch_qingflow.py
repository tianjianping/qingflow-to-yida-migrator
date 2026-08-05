# -*- coding: utf-8 -*-
"""步骤1：分页拉取轻流表单数据 -> data/raw/<表单名>_raw.json

接口（已按实际文档核对）:
  POST {baseUrl}/app/{appKey}/apply/filter
  Header: accessToken
  Body:   {"pageSize": N, "pageNum": N, "type": N}   # type: 数据范围，8=全部数据
  返回:   errCode / errMsg / result{pageAmount, pageNum, pageSize, resultAmount,
          result[ {applyId, answers[ {queId, queTitle, queType, values[], tableValues[]} ]} ]}

增量模式（--incremental）:
  按内置字段"更新时间"(queId=3) 过滤，minValue = 上次拉取时间 - 5分钟重叠窗口，
  只拉变更记录并合并进本地镜像（以 applyId 为键覆盖/追加）。
  未变更记录保持原样，本地镜像始终是全量快照，下游 02d/03/04 无需改动。

用法: python 01_fetch_qingflow.py <表单名> [--incremental|--full]
  默认: 本地已有镜像和同步状态时走增量，否则全量。
"""
import csv
import json
import sys
import time

from common import (load_credentials, load_form_config, http_request, save_json,
                    BASE_DIR, DATA_DIR, load_sync_state, save_sync_state, _fmt_ts, _parse_ts)

INCREMENTAL_OVERLAP_SEC = 300  # 5 分钟重叠窗口，防秒级时间戳边界漏数据
CACHE_TTL_HOURS = 24           # 附件 URL 有效期
NO_ATT_CACHE_TTL_HOURS = 72    # D3: 无附件表单缓存有效期拉长，减少无谓重拉


def _has_attachment_field(form_name):
    """表单映射中是否存在附件字段（componentName=AttachmentField，queId 非 0）。

    D3: 无附件表单不依赖 URL 新鲜度，缓存 TTL 可拉长到 72h；
    有附件表单维持 24h，配合附件任务内的 URL 精确过期判断。"""
    mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
    if not mp.exists():
        return False
    try:
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                cn = (row.get("componentName") or "").strip()
                qid = (row.get("轻流queId") or "").strip()
                if cn == "AttachmentField" and qid and qid != "0":
                    return True
    except Exception:
        return False
    return False

ERR_HINTS = {
    49300: "accessToken 无效或已过期，请重新生成后更新 config/credentials.json 中的 qingflow.accessToken",
    40023: "数据/应用不存在，请检查表单配置中的 appKey",
}


def is_ok(resp):
    """轻流 errCode 成功时可能为 0 / "0" / "" / null"""
    return resp.get("errCode") in (0, "0", "", None)


def err_detail(resp):
    code = resp.get("errCode")
    try:
        hint = ERR_HINTS.get(int(code), "")
    except (TypeError, ValueError):
        hint = ""
    msg = f"errCode={code} errMsg={resp.get('errMsg')}"
    return msg + (f"\n  提示: {hint}" if hint else "")


def collect_fields(records):
    """汇总字段清单（queId / queTitle / queType / 是否子表单 / 子字段的父表单）。

    子表单子字段（仅出现在 tableValues 行内）也会一并收集，并标注 parentQueId，
    供 preflight 字段校验与 02b 顶层字段匹配区分使用。
    """
    fields = {}
    for apply in records:
        for ans in apply.get("answers", []):
            qid = ans.get("queId")
            if qid is None:
                continue
            if qid not in fields:
                fields[qid] = {
                    "queId": qid,
                    "queTitle": ans.get("queTitle", ""),
                    "queType": ans.get("queType", ""),
                    "hasTableValues": bool(ans.get("tableValues")),
                    "parentQueId": "",
                }
            elif ans.get("tableValues"):
                fields[qid]["hasTableValues"] = True
            # 子表单子字段：顶层 answers 不含它们，只能从 tableValues 行内收集
            for row in ans.get("tableValues") or []:
                for sub in row or []:
                    sq = sub.get("queId")
                    if sq is None:
                        continue
                    if sq not in fields:
                        fields[sq] = {
                            "queId": sq,
                            "queTitle": sub.get("queTitle", ""),
                            "queType": sub.get("queType", ""),
                            "hasTableValues": False,
                            "parentQueId": str(qid),
                        }
    return sorted(fields.values(), key=lambda x: str(x["queId"]))


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 01_fetch_qingflow.py <表单名> [--incremental|--full]")
    form_name = sys.argv[1]
    force_full = "--full" in sys.argv
    force_inc = "--incremental" in sys.argv

    cred = load_credentials()
    cfg = load_form_config(form_name)
    qf = cred["qingflow"]
    qf_cfg = cfg["qingflow"]
    url = f"{qf['baseUrl']}/app/{qf_cfg['appKey']}/apply/filter"
    headers = {"accessToken": qf["accessToken"]}
    page_size = qf_cfg.get("pageSize", 100)

    state = load_sync_state(form_name)
    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"
    has_raw = raw_path.exists()
    has_watermark = bool(state.get("lastIncrementalPullAt") or state.get("lastFullPullAt"))

    if force_full:
        mode = "full"
    elif force_inc:
        mode = "incremental" if (has_raw and has_watermark) else "full"
        if mode == "full":
            print("[提示] 本地无有效增量状态，本次按全量执行")
    else:
        mode = "incremental" if (has_raw and has_watermark) else "full"

    pull_start = time.time()
    all_applies = []
    seen_ids = set()
    dup = 0
    page_num = 1
    page_amount = None
    result_amount = None
    partial_path = DATA_DIR / "raw" / f"{form_name}_raw.partial.json"

    existing_map = {}
    existing_order = []
    existing_raw = []
    min_value = None
    if mode == "incremental":
        last_pull = state.get("lastIncrementalPullAt") or state.get("lastFullPullAt")
        base_ts = _parse_ts(last_pull)
        if base_ts is None:
            base_ts = pull_start - 86400
        min_value = _fmt_ts(base_ts - INCREMENTAL_OVERLAP_SEC)
        print(f"[增量] 仅拉取更新时间 >= {min_value} 的记录")
        try:
            existing_raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if isinstance(existing_raw, dict):
                existing_raw = existing_raw.get("result", {}).get("result", [])
        except Exception:
            existing_raw = []
        for r in existing_raw:
            aid = r.get("applyId")
            if aid is None:
                continue
            aid = str(aid)
            if aid not in existing_map:
                existing_order.append(aid)
            existing_map[aid] = r

    try:
        while True:
            body = {"pageSize": page_size, "pageNum": page_num,
                    "type": qf_cfg.get("type", 8)}
            if mode == "incremental":
                body["queries"] = [{"queId": 3, "minValue": min_value, "scope": 1}]
                body["queriesRel"] = "and"
                body["sorts"] = [{"queId": 3, "isAscend": True}]
            print(f"拉取第 {page_num} 页" + (f" / 共 {page_amount} 页" if page_amount else "") + " ...")
            resp = http_request(url, headers=headers, body=body, min_interval=0.25)
            if not is_ok(resp):
                sys.exit(f"[错误] 轻流接口返回异常: {err_detail(resp)}")

            result = resp.get("result") or {}
            applies = result.get("result") or []
            for a in applies:
                aid = a.get("applyId")
                if aid is not None and aid in seen_ids:
                    dup += 1
                    continue
                if aid is not None:
                    seen_ids.add(aid)
                all_applies.append(a)

            if page_amount is None:
                page_amount = int(result.get("pageAmount") or 0)
                result_amount = int(result.get("resultAmount") or 0)
                print(f"  本次命中: {result_amount} 条，总页数: {page_amount}")
                if result_amount == 0:
                    if mode == "incremental":
                        print("[提示] 本次没有变更记录（增量模式下属正常）")
                    else:
                        print("[警告] 该应用下没有数据（或 type 取值不包含目标数据）")
                    break

            if not applies or page_num >= page_amount:
                break
            page_num += 1
    except BaseException as e:
        # 中断/异常时把已拉到的页落到 .partial.json，保留现场供排查；
        # 正式 _raw.json 保持上一次的完整快照不被破坏。
        if all_applies:
            save_json(partial_path, all_applies)
            print(f"[中断] 已拉取 {len(all_applies)} 条，暂存至 {partial_path}（正式快照未被覆盖）")
        raise

    if mode == "incremental":
        fetched_map = {}
        fetched_order = []
        for a in all_applies:
            aid = str(a.get("applyId"))
            if aid not in fetched_map:
                fetched_order.append(aid)
            fetched_map[aid] = a
        merged = []
        for aid in existing_order:
            if aid in fetched_map:
                merged.append(fetched_map.pop(aid))
            elif aid in existing_map:
                merged.append(existing_map[aid])
        for aid in fetched_order:
            if aid in fetched_map:
                merged.append(fetched_map.pop(aid))
        final_records = merged
        print(f"合并完成: 原 {len(existing_raw)} 条 + 变更 {len(all_applies)} 条 -> {len(merged)} 条"
              + (f"（去重丢弃 {dup} 条重复 applyId）" if dup else ""))
        if len(merged) < len(existing_map):
            print("[警告] 合并后条数少于原镜像，请检查；必要时使用 --full")
    else:
        final_records = all_applies
        print(f"共拉取 {len(all_applies)} 条" + (f"（去重丢弃 {dup} 条重复 applyId）" if dup else ""))
        if result_amount and len(all_applies) != result_amount:
            print(f"[警告] 拉取条数({len(all_applies)}) 与接口报告的总量({result_amount}) 不一致，请检查")

    changed = bool(all_applies)
    if mode == "incremental" and not changed:
        print("[增量] 无变更记录，镜像保持不变")
        # 无变更时也重算字段清单：collect_fields 逻辑可能升级（如子表单子字段收集），
        # 让清单始终反映最新规则，避免 preflight 误报"queId 不在字段清单"
        field_list = collect_fields(final_records)
        save_json(DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json", field_list)
        print(f"[清单] 字段清单重算: 共 {len(field_list)} 个字段（含子表单子字段）")
    else:
        print("[收尾] 开始落盘镜像（此处可能因大文件写盘耗时数秒，勿中断）...")
        save_json(raw_path, final_records)
        field_list = collect_fields(final_records)
        save_json(DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json", field_list)
        print(f"字段清单共 {len(field_list)} 个字段")

    # 拉取成功后清理残留的中断暂存文件，避免误用
    try:
        if partial_path.exists():
            partial_path.unlink()
    except Exception:
        pass

    # 更新同步状态：watermark 用拉取开始时间，重叠窗口保证边界不丢数据
    pull_end = time.time()
    new_state = dict(state)
    new_state["lastPullCount"] = len(all_applies)
    new_state["sourceCount"] = len(final_records)
    if changed:
        # D3: 无附件表单不依赖签名 URL 新鲜度，缓存有效期拉长，减少无谓重拉
        ttl_h = CACHE_TTL_HOURS if _has_attachment_field(form_name) else NO_ATT_CACHE_TTL_HOURS
        new_state["cacheExpireAt"] = _fmt_ts(pull_end + ttl_h * 3600)
    if mode == "full":
        new_state["lastFullPullAt"] = _fmt_ts(pull_end)
        new_state["lastIncrementalPullAt"] = _fmt_ts(pull_end)
    else:
        new_state["lastIncrementalPullAt"] = _fmt_ts(pull_start)
        if not new_state.get("lastFullPullAt"):
            new_state["lastFullPullAt"] = ""
    try:
        save_sync_state(form_name, new_state)
    except Exception as e:
        # A2: 状态写盘失败不能静默——否则下次拉取退化全量（P0-E1）
        sys.exit(f"[状态] 同步状态写盘失败：{e}；本次拉取已完成但未记水印，"
                 f"下次将按全量执行（数据无损失，可重跑 --full 补齐）")
    print(f"[状态] 模式={mode} 本地镜像 {len(final_records)} 条"
          + (f"，附件缓存有效期至 {new_state['cacheExpireAt']}" if changed else "，附件缓存未刷新"))


if __name__ == "__main__":
    main()
