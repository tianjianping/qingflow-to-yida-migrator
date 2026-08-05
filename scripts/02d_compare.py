# -*- coding: utf-8 -*-
"""阶段二 / 数据对比：在格式化与写入之前，独立产出「差异清单」。

对账三方数据（均来自阶段一的拉取产物，本步骤零网络请求、只读+写本地清单）：
  1. 轻流源数据      data/raw/<表单>_raw.json          （01 产物）
  2. 宜搭真实存量    data/raw/<表单>_yida_instances.json（02c 产物）
  3. 本地写入台账    data/result/<表单>_result.json     （04 历次写入记录）

分类规则（以 轻流数据ID(queId=-17) 为主匹配键；存在性权威=宜搭真实存量里的轻流数据ID 集合；变更检测用轻流「源数据」md5 指纹）：
  - 轻流数据ID 在宜搭存量中命中 且 源指纹变化(或 --force) -> update
  - 轻流数据ID 在宜搭存量中命中 且 源指纹未变              -> skip
  - 轻流数据ID 未在宜搭存量中命中（从未迁移 / 宜搭缺失重建）-> create
  - 轻流数据ID 为空                              -> create（并告警，建议先补全再迁移）
  - 台账里有、但轻流源里已不存在的 applyId       -> srcDeleted（仅报告，不删除宜搭数据）

产物: data/diff/<表单>_diff.json —— 阶段三(03)据此只转换差异集，阶段四(04)据此纯执行写入。

指纹说明: 自本版本起指纹改为「轻流原始记录」md5（此前为转换后 formData 的 md5）。
首次运行新管线时旧指纹全部失配，会把全部已迁移记录标为 update（安全、幂等，仅多耗时）。
若确认当前宜搭与轻流已完全同步，可加 --rebase 将台账指纹重置为当前源指纹（跳过这次全量更新）。

用法:
  python 02d_compare.py <表单配置名>            # 生成差异清单
  python 02d_compare.py <表单配置名> --force    # 已存在记录全部标记为 update
  python 02d_compare.py <表单配置名> --rebase   # 确认已同步：重置台账指纹后再对比
"""
import hashlib
import json
import sys
import time
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
from common import load_form_config, load_json, save_json, DATA_DIR


def _file_fp(path):
    """文件的轻量指纹 (mtime_ns, size)，用于判断产物是否被重写。"""
    if not path.exists():
        return None
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def src_hash(apply):
    """轻流原始记录的稳定指纹（任何字段变化都会体现）。"""
    return hashlib.md5(
        json.dumps(apply, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def extract_qingflow_did(apply):
    """从轻流原始记录取 queId=-17(数据ID) 的值，作为跨系统主匹配键。

    轻流系统字段(queId=-17)的值常放在 answers[].values 列表内的对象里
    （顶层 value/dataValue 可能为 None），故优先解析 values，再回退顶层。
    """
    for a in (apply.get("answers") or []):
        if str(a.get("queId")) == "-17":
            candidates = []
            vals = a.get("values")
            if isinstance(vals, list):
                candidates.extend(vals)
            candidates.append(a)  # 回退顶层
            for item in candidates:
                if not isinstance(item, dict):
                    if item not in (None, ""):
                        return str(item).strip()
                    continue
                v = item.get("value")
                if v in (None, ""):
                    v = item.get("dataValue")
                if v not in (None, ""):
                    return str(v).strip()
    return None


def normalize_done(result):
    done = result.get("done", {})
    for aid, v in list(done.items()):
        if isinstance(v, str):
            done[aid] = {"inst": v, "hash": None}
    result["done"] = done
    return result


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 02d_compare.py <表单配置名> [--force] [--rebase]")
    form_name = sys.argv[1]
    force = "--force" in sys.argv
    rebase = "--rebase" in sys.argv
    load_form_config(form_name)  # 校验配置存在

    # 1) 轻流源数据（阶段一 / 01）：取每条记录的 轻流数据ID(queId=-17) 与源指纹
    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"
    if not raw_path.exists():
        sys.exit(f"[缺产物] {raw_path} 不存在 —— 请先执行阶段一（01 拉取轻流数据）")
    yida_path = DATA_DIR / "raw" / f"{form_name}_yida_instances.json"
    if not yida_path.exists():
        sys.exit(f"[缺产物] {yida_path} 不存在 —— 请先执行阶段一（02c 拉取宜搭存量）")

    # B2: 输入指纹未变时直接复用既有差异清单，跳过全量对比。
    # 01 增量无变更不重写 raw、02c 增量无变更不重写存量 -> 两者指纹均不变，
    # 对比从秒级降到毫秒级。任一输入变化（或 --force）都强制重算。
    diff_path = DATA_DIR / "diff" / f"{form_name}_diff.json"
    if not force and diff_path.exists():
        try:
            prev = load_json(diff_path)
        except Exception:
            prev = None
        if isinstance(prev, dict) and prev.get("srcCount") is not None:
            cur = {"raw": _file_fp(raw_path), "yida": _file_fp(yida_path)}
            if prev.get("inputFingerprint") == cur:
                print(f"[快速跳过] raw 与宜搭存量均未变化，复用既有差异清单（生成于 {prev.get('generatedAt')}）")
                print(f"  待新建 {len(prev.get('create') or [])} | 待更新 {len(prev.get('update') or [])} | "
                      f"跳过(无变化) {len(prev.get('skip') or [])}")
                return

    raw = load_json(raw_path)
    src_did, src_hash_map = {}, {}
    for apply in raw:
        aid = str(apply.get("applyId"))
        src_did[aid] = extract_qingflow_did(apply)
        src_hash_map[aid] = src_hash(apply)

    # 2) 宜搭真实存量（阶段一 / 02c）：按 轻流数据ID 建索引（存在性权威 = 数据ID）
    yida = load_json(yida_path)
    existing = yida.get("existing") or {}
    did_to_inst = yida.get("didToInst") or {d: i for i, d in existing.items() if d}
    did_set = set(did_to_inst)
    # P1-2: 02c 中查询失败、存在性未知的实例 —— 相关源记录本轮一律不写，避免重复创建
    unresolved_inst = set(yida.get("unresolved") or [])
    # 02c 全量扫描中断时 unresolved 为 page_unresolved:N 标记，无法定位具体实例ID，
    # 此时所有记录的存在性都可能未知 -> 本轮全部延后，绝不新建（配合 02c 默认 exit(2) 阻断）
    partial_scan = any(str(x).startswith("page_unresolved") for x in unresolved_inst)
    if partial_scan:
        print(f"[严重] 02c 全量扫描未完整结束（存在性未知），本轮所有记录将被标为 deferred，不会写入任何数据。")
    if not did_set:
        print("[提示] 宜搭存量中未索引到任何 轻流数据ID -> 全部源记录将视为新建（若宜搭已清空则符合预期）")

    # 3) 本地写入台账
    result_path = DATA_DIR / "result" / f"{form_name}_result.json"
    result = {"done": {}, "failed": {}}
    if result_path.exists():
        result = load_json(result_path)
    result = normalize_done(result)
    done = result["done"]

    # 可选: 指纹基线重置（确认宜搭与轻流当前已同步时使用）
    if rebase:
        n = 0
        for aid, info in done.items():
            did = src_did.get(aid)
            if did and did in did_set:
                info["hash"] = src_hash_map[aid]
                n += 1
        save_json(result_path, result)
        print(f"[rebase] 已将 {n} 条台账指纹重置为当前源指纹（视为已同步）")

    # 4) 分类（以 轻流数据ID 为主匹配键；空数据ID=宜搭手工记录，自然不匹配、不触碰）
    create, update, skip, src_deleted, deferred = [], [], [], [], []
    empty_did = 0
    for aid, did in src_did.items():
        # 该记录存在性本轮未知（02c 扫描中断）-> 全部延后，绝不新建
        if partial_scan:
            deferred.append(aid)
            continue
        # 该记录历史上写入过的实例本轮存在性未知 -> 延后处理，绝不新建
        if unresolved_inst:
            info = done.get(aid)
            inst = info.get("inst") if isinstance(info, dict) else info
            if inst and inst in unresolved_inst:
                deferred.append(aid)
                continue
        if did and did in did_set:
            h = src_hash_map[aid]
            info = done.get(aid)
            prev = info.get("hash") if info else None
            if force or prev is None or prev != h:
                update.append(aid)                      # 数据ID命中且源变化(或指纹未知/强制)
            else:
                skip.append(aid)                        # 数据ID命中且未变化
        else:
            if not did:
                empty_did += 1
            create.append(aid)                          # 数据ID未在宜搭命中 -> 新建(或宜搭缺失重建)
    for aid in done:
        if aid not in src_did:
            src_deleted.append(aid)                     # 轻流侧已删除（仅报告，不删宜搭）

    diff = {
        "form": form_name,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "srcCount": len(src_did),
        "yidaExisting": len(did_set),
        "matchKey": "轻流数据ID(queId=-17)",
        "create": create,
        "update": update,
        "skip": skip,
        "srcDeleted": src_deleted,
        "deferred": deferred,      # 存在性未知，本轮不写（02c 查询失败），网络恢复后重跑 02c
        "partialSource": bool(unresolved_inst),
        # P2-1: 只保留差异集的指纹，避免大表单 diff.json 膨胀（04 只用 create+update 的指纹）
        "srcHash": {aid: src_hash_map[aid]
                    for aid in set(create) | set(update) if aid in src_hash_map},
        # B2: 记录输入产物指纹，下次对比时指纹未变则直接复用本清单
        "inputFingerprint": {"raw": _file_fp(raw_path), "yida": _file_fp(yida_path)},
        "force": force,
    }
    out = DATA_DIR / "diff" / f"{form_name}_diff.json"
    save_json(out, diff)

    if empty_did:
        print(f"[警告] {empty_did} 条轻流记录 数据ID(queId=-17) 为空，已按新建处理"
              f"（建议先补全轻流数据ID再迁移，以免重复创建）")

    print(f"[对比完成] 轻流源 {len(src_did)} 条 | 宜搭存量(含轻流数据ID) {len(did_set)} 个")
    print(f"  待新建 {len(create)} | 待更新 {len(update)} | 跳过(无变化) {len(skip)} | 源已删除 {len(src_deleted)}")
    if deferred:
        print(f"  [延后] {len(deferred)} 条记录对应的宜搭实例在 02c 中查询失败（存在性未知），"
              f"本轮不写以避免重复创建。请在网络恢复后重跑 02c 再对比。")
    if src_deleted:
        print(f"  [注意] {len(src_deleted)} 条记录在轻流已删除但宜搭仍存在，本工具不会删除宜搭数据，如需清理请人工处理:")
        for aid in src_deleted[:10]:
            print(f"     applyId={aid} -> 实例 {done[aid].get('inst')}")
        if len(src_deleted) > 10:
            print(f"     ... 其余 {len(src_deleted) - 10} 条见差异清单")
    print(f"  差异清单 -> {out}")
    if update and not force:
        matched = len(update) + len(skip)
        if matched and len(update) == matched:
            print("  [提示] 全部已匹配记录被标为更新，多半因指纹机制升级（旧=转换后指纹，新=源指纹）。")
            print("         若确认宜搭与轻流当前已同步，可用 --rebase 跳过这次全量更新。")


if __name__ == "__main__":
    main()
