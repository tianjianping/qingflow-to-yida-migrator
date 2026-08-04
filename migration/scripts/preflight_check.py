# -*- coding: utf-8 -*-
"""迁移前预检：扫描各阶段产物，检测已知的边界情况并输出结构化告警。

在「准备数据」完成后自动触发（也可手动调用），帮助用户在写入前发现：
  - 宜搭字段未创建 / 标题不匹配 / 定位键缺失
  - 轻流数据ID 为空 / 宜搭存量扫描不完整
  - 映射表无有效字段 / 去重键组件类型不支持检索
  - 转换后有记录无字段 / 系统字段未配置
  - 附件字段无对应组件ID / URL 即将过期

用法:
  python preflight_check.py <表单配置名>
输出:
  JSON 到 stdout，格式 {ok, checks:[{level, category, title, detail, suggestion}]}
"""
import csv
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from common import load_form_config, load_credentials, BASE_DIR, DATA_DIR

DEDUP_QUE_ID = "-17"
MIN_EXPIRE_DAYS = 3
SECONDS_PER_DAY = 86400


def check(level, category, title, detail="", suggestion=""):
    return {"level": level, "category": category, "title": title,
            "detail": detail, "suggestion": suggestion}


def run(form_name):
    cfg = load_form_config(form_name)
    checks = []

    # ---- 1. 映射表检查 ----
    mapping_rel = cfg.get("mappingFile") or f"mappings/{form_name}_mapping.csv"
    mp = BASE_DIR / mapping_rel
    if not mp.exists():
        checks.append(check("error", "映射表", "映射表不存在",
                            f"{mp}", "请先运行「准备数据」生成映射草稿并完成自动映射"))
        return checks

    rows = []
    with open(mp, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r = {k.strip(): (v or "").strip() for k, v in r.items()}
            rows.append(r)
    if not rows:
        checks.append(check("error", "映射表", "映射表为空", str(mp), "请重新运行 02b 自动映射"))
        return checks

    matched = [r for r in rows if r.get("轻流queId")]
    unmatched = [r for r in rows if not r.get("轻流queId") and r.get("transform") != "skip"
                 and "子表单子组件" not in r.get("备注", "")]
    skip_rows = [r for r in rows if r.get("transform") == "skip"]

    if not matched:
        checks.append(check("error", "映射表", "映射表无任何已匹配字段",
                            "所有行的 轻流queId 均为空", "请核对宜搭字段标题与轻流字段名是否完全一致"))
    if unmatched:
        names = "、".join(r.get("宜搭字段名", "?") for r in unmatched[:8])
        more = f" 等 {len(unmatched)} 个" if len(unmatched) > 8 else ""
        checks.append(check("warn", "映射表", f"{len(unmatched)} 个宜搭字段未匹配轻流字段",
                            names + more,
                            "请检查宜搭字段标题(label)与轻流字段名(queTitle)是否完全一致（含空格/全半角），"
                            "或在 mapping.csv 中手工补 轻流queId，或标 skip"))

    # 1b. componentId 为空的已匹配行（宜搭组件未正确拉取）
    empty_cid = [r for r in matched if not r.get("componentId")]
    if empty_cid:
        checks.append(check("error", "映射表", f"{len(empty_cid)} 个已匹配字段缺少宜搭 componentId",
                            "、".join(r.get("宜搭字段名", "?") for r in empty_cid[:5]),
                            "宜搭组件定义可能未正确拉取，请重跑 02_fetch_yida_schema.py"))

    # 1c. 重复 queId（同一轻流字段映射到多个宜搭字段，子表单除外）
    que_id_count = {}
    for r in matched:
        qid = r.get("轻流queId", "")
        if qid and qid != DEDUP_QUE_ID:
            que_id_count.setdefault(qid, []).append(r.get("宜搭字段名", "?"))
    dup_que = {q: n for q, n in que_id_count.items() if len(n) > 1}
    if dup_que:
        examples = "; ".join(f"queId={q} -> {','.join(n[:3])}" for q, n in list(dup_que.items())[:3])
        checks.append(check("warn", "映射表", f"{len(dup_que)} 个轻流字段被映射到多个宜搭字段",
                            examples,
                            "同一轻流字段写入多个宜搭字段通常非预期，请确认是否需要"))

    # 1d. 重复 componentId（同一宜搭字段被多个轻流字段映射）
    cid_count = {}
    for r in matched:
        cid = r.get("componentId", "")
        if cid:
            cid_count.setdefault(cid, []).append(r.get("轻流字段名") or r.get("宜搭字段名", "?"))
    dup_cid = {c: n for c, n in cid_count.items() if len(n) > 1}
    if dup_cid:
        examples = "; ".join(f"{','.join(n[:3])}" for n in list(dup_cid.values())[:3])
        checks.append(check("warn", "映射表", f"{len(dup_cid)} 个宜搭字段被多个轻流字段映射",
                            examples,
                            "后写入的值会覆盖先写入的，请确认映射关系是否正确"))

    # ---- 2. 必需字段强制检查：编号(queId=0) + 数据ID(queId=-17) ----
    bianhao_row = next((r for r in matched if r.get("轻流queId") == "0"), None)
    dedup_row = next((r for r in rows if r.get("轻流queId") == DEDUP_QUE_ID), None)

    if not bianhao_row:
        checks.append(check("error", "定位键", "必需字段「编号」未建立（queId=0 未映射）",
                            "宜搭表单中必须创建标题为「编号」的文本字段，并与轻流 queId=0 关联",
                            "请在宜搭设计器中创建标题为「编号」的 TextField，然后重新运行 02b 自动映射。"
                            "编号是用户可见的定位键，缺失则不允许执行更新操作"))
    else:
        # 编号字段有 componentId 才算真正可用
        if not bianhao_row.get("componentId"):
            checks.append(check("error", "定位键", "「编号」字段缺少宜搭 componentId",
                                f"映射行: {bianhao_row}",
                                "宜搭组件可能未正确拉取，请重跑 02_fetch_yida_schema.py 后重新 02b"))

    if not dedup_row:
        checks.append(check("error", "定位键", "必需字段「数据ID」未建立（queId=-17 未映射）",
                            "宜搭表单中必须创建标题为「数据ID」的文本字段，并与轻流 queId=-17 关联",
                            "请在宜搭设计器中创建标题为「数据ID」的 TextField，然后重新运行 02b 自动映射。"
                            "数据ID是跨系统匹配键，缺失则全部记录被判为新建，导致重复创建"))
    elif not dedup_row.get("componentId"):
        checks.append(check("error", "定位键", "「数据ID」字段缺少宜搭 componentId",
                            f"映射行: {dedup_row}",
                            "宜搭组件可能未正确拉取，请重跑 02_fetch_yida_schema.py 后重新 02b"))
    else:
        cn = dedup_row.get("componentName", "")
        CONDITION_TYPES = {"TextField", "TextAreaField", "NumberField", "DateField",
                           "SelectField", "DropdownField", "RadioField"}
        if cn not in CONDITION_TYPES:
            checks.append(check("warn", "定位键", f"数据ID组件类型 {cn} 不在检索条件支持列表",
                                f"04 写入时 insertOrUpdate 的检索条件可能不匹配，回退为 TEXT/eq",
                                f"建议将数据ID字段改为 TextField 等文本类组件"))

    # ---- 3. 轻流源数据检查 ----
    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"
    if not raw_path.exists():
        checks.append(check("error", "源数据", "轻流源数据不存在",
                            f"{raw_path}", "请先运行 01_fetch_qingflow.py 拉取数据"))
    else:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("result", {}).get("result", [])
        if not raw:
            checks.append(check("warn", "源数据", "轻流源数据为空（0 条记录）",
                                "", "检查轻流 appKey 是否正确，或该应用是否确实无数据"))
        else:
            empty_did = 0
            for apply in raw:
                found = False
                for a in (apply.get("answers") or []):
                    if str(a.get("queId")) == DEDUP_QUE_ID:
                        v = None
                        vals = a.get("values")
                        if isinstance(vals, list) and vals:
                            v = vals[0].get("value") or vals[0].get("dataValue") if isinstance(vals[0], dict) else vals[0]
                        else:
                            v = a.get("value") or a.get("dataValue")
                        if v in (None, "", "null"):
                            empty_did += 1
                        found = True
                        break
                if not found:
                    empty_did += 1
            if empty_did:
                checks.append(check("warn", "源数据", f"{empty_did} 条轻流记录数据ID(queId=-17)为空或缺失",
                                    f"共 {len(raw)} 条，其中 {empty_did} 条无有效数据ID",
                                    "这些记录会被按新建处理，可能重复创建。建议先在轻流补全数据ID"))

    # ---- 4. 宜搭存量检查 ----
    yida_path = DATA_DIR / "raw" / f"{form_name}_yida_instances.json"
    if not yida_path.exists():
        checks.append(check("warn", "宜搭存量", "宜搭存量文件不存在（02c 未运行）",
                            f"{yida_path}", "全部记录将视为新建。如宜搭已有数据会导致重复创建"))
    else:
        yj = json.loads(yida_path.read_text(encoding="utf-8"))
        existing = yj.get("existing") or {}
        did_to_inst = yj.get("didToInst") or {d: i for i, d in existing.items() if d}
        unresolved = yj.get("unresolved") or []
        partial = any(str(x).startswith("page_unresolved") for x in unresolved)
        if partial:
            checks.append(check("error", "宜搭存量", "02c 全量扫描未完整结束",
                                "部分页实例存在性未知",
                                "网络恢复后重跑 02c，否则全部记录会被延后处理（不写入）"))
        elif unresolved:
            checks.append(check("warn", "宜搭存量", f"{len(unresolved)} 个实例存在性未知",
                                "02c 中查询失败的实例",
                                "相关记录本轮会被延后处理，网络恢复后重跑 02c"))
        # 重复实例检测
        if existing:
            seen_dids = {}
            for inst_id, did in existing.items():
                if did:
                    seen_dids.setdefault(did, []).append(inst_id)
            dups = {d: i for d, i in seen_dids.items() if len(i) > 1}
            if dups:
                checks.append(check("warn", "宜搭存量", f"{len(dups)} 个数据ID在宜搭存在多条实例",
                                    f"如 数据ID={list(dups.keys())[:3]}",
                                    "历史残留与重建导入并存，建议清理宜搭侧重复实例"))

    # ---- 5. 差异清单检查 ----
    diff_path = DATA_DIR / "diff" / f"{form_name}_diff.json"
    if diff_path.exists():
        diff = json.loads(diff_path.read_text(encoding="utf-8"))
        create_n = len(diff.get("create") or [])
        update_n = len(diff.get("update") or [])
        skip_n = len(diff.get("skip") or [])
        deferred_n = len(diff.get("deferred") or [])
        src_deleted_n = len(diff.get("srcDeleted") or [])
        if deferred_n:
            checks.append(check("warn", "差异清单", f"{deferred_n} 条记录被延后处理",
                                "存在性未知，本轮不写入",
                                "网络恢复后重跑「准备数据」即可自动恢复"))
        if src_deleted_n:
            checks.append(check("info", "差异清单", f"{src_deleted_n} 条记录在轻流已删除但宜搭仍存在",
                                "工具不会自动删除宜搭数据",
                                "如需清理请人工处理"))
        if create_n > 0 and not did_to_inst if yida_path.exists() else create_n > 0:
            if not (yida_path.exists() and did_to_inst):
                checks.append(check("warn", "差异清单", f"全部 {create_n} 条记录被判为新建",
                                    "宜搭存量为空或无数据ID索引",
                                    "确认宜搭确实无数据，否则可能重复创建。请检查定位键是否正确配置"))
        all_update = (create_n == 0 and skip_n == 0 and update_n > 0)
        if all_update and not diff.get("force"):
            checks.append(check("info", "差异清单", "全部已匹配记录被标为更新",
                                "多半因指纹机制升级（旧=转换后指纹，新=源指纹）",
                                "若确认宜搭与轻流已同步，可在高级操作中用 --rebase 跳过全量更新"))

    # ---- 6. 转换结果检查 ----
    trans_path = DATA_DIR / "transformed" / f"{form_name}_formdata.json"
    if trans_path.exists():
        trans = json.loads(trans_path.read_text(encoding="utf-8"))
        if isinstance(trans, dict):
            trans = trans.get("records") or []
        empty_recs = [r for r in trans if not r.get("formData")]
        if empty_recs:
            checks.append(check("warn", "格式转换", f"{len(empty_recs)} 条记录转换后无字段",
                                "整条记录无可迁移字段",
                                "检查映射表是否正确，或该记录在轻流确实所有字段为空"))
        # 6b. 转换告警文件检查
        warn_path = DATA_DIR / "transformed" / f"{form_name}_warnings.csv"
        if warn_path.exists():
            with open(warn_path, encoding="utf-8-sig", newline="") as wf:
                warn_rows = list(csv.DictReader(wf))
            if warn_rows:
                # 按告警类型分类统计
                warn_types = {}
                for wr in warn_rows:
                    msg = (wr.get("msg") or "")[:40]
                    warn_types[msg] = warn_types.get(msg, 0) + 1
                top = "; ".join(f"{m}({n})" for m, n in
                                sorted(warn_types.items(), key=lambda x: -x[1])[:3])
                checks.append(check("warn", "格式转换", f"转换产生 {len(warn_rows)} 条告警",
                                    top,
                                    "查看 _warnings.csv 了解详情。常见可忽略项：附件/图片/关联跳过；"
                                    "需关注：数字解析失败、成员字段未取到 userId、日期解析失败"))

    # ---- 7. 附件字段检查 ----
    att_que_ids = set()
    for r in rows:
        cn = r.get("componentName", "")
        if cn in ("AttachmentField", "ImageField"):
            qid = r.get("轻流queId", "").strip()
            if qid:
                att_que_ids.add(qid)
            else:
                checks.append(check("warn", "附件", f"附件字段「{r.get('宜搭字段名', '?')}」未关联轻流queId",
                                    f"componentId={r.get('componentId', '?')}",
                                    "附件迁移将跳过此字段。请在 mapping.csv 中补 轻流queId 或确认标 skip"))

    # ---- 8. 附件URL过期检查（仅 peek 模式有产物时） ----
    if att_que_ids and raw_path.exists():
        import time
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("result", {}).get("result", [])
        now = time.time()
        expiring = 0
        for apply in raw[:200]:  # 抽样前 200 条
            for a in (apply.get("answers") or []):
                if str(a.get("queId")) in att_que_ids:
                    vals = a.get("values") or []
                    for v in vals:
                        url = (v.get("value") or v.get("dataValue") or "") if isinstance(v, dict) else str(v)
                        if url and "expire" in url.lower():
                            # 尝试解析 expire 参数
                            import urllib.parse
                            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                            exp_str = params.get("expire", params.get("expires", [""]))[0]
                            try:
                                exp_ts = int(exp_str)
                                days_left = (exp_ts - now) / SECONDS_PER_DAY
                                if days_left < MIN_EXPIRE_DAYS:
                                    expiring += 1
                            except (ValueError, IndexError):
                                pass
        if expiring:
            checks.append(check("warn", "附件", f"{expiring} 个附件URL将在 {MIN_EXPIRE_DAYS} 天内过期",
                                "轻流附件URL有时效性",
                                "请尽快执行附件迁移，或重新运行 01 拉取新鲜URL"))

    # ---- 9. 系统字段配置检查 ----
    # 宜搭系统字段（创建时间/提交人）不可通过 API 修改，需在宜搭建同名自定义字段接收轻流原始值。
    # 自动映射(02b)会将 queId=2→申请时间、queId=1→申请人 通过同名匹配自动关联。
    # 03_transform.py 主循环会自动处理映射表中的 queId=2/1，无需手工配置 systemFields。
    sys_cfg = cfg.get("systemFields", {})
    # 检查映射表中是否已自动匹配了系统字段
    has_origin_time = any(r.get("轻流queId") == "2" and r.get("componentId") for r in matched)
    has_origin_user = any(r.get("轻流queId") == "1" and r.get("componentId") for r in matched)
    for field, label, que_id, auto_matched in [
        ("originCreateTime", "申请时间", "2", has_origin_time),
        ("originApplier", "申请人", "1", has_origin_user),
    ]:
        cid = sys_cfg.get(field, "")
        configured = cid and not str(cid).startswith("填入")
        if not configured and not auto_matched:
            checks.append(check("info", "系统字段", f"系统字段「{label}」未配置（queId={que_id} 未自动匹配）",
                                "宜搭系统字段(创建时间/提交人)不可通过API修改，"
                                "需在宜搭建同名自定义字段（标题「申请时间」/「申请人」）接收轻流原始值",
                                "在宜搭表单中创建标题为「申请时间」(DateField) 和「申请人」(EmployeeField) 的字段，"
                                "重新运行 02b 自动映射即可同名匹配。不影响数据迁移，但原始时间/提交人信息会丢失"))
        elif not configured and auto_matched:
            checks.append(check("info", "系统字段", f"系统字段「{label}」已通过映射表自动匹配(queId={que_id})",
                                "03 格式化阶段会自动从映射表检测 componentId，无需手工配置 systemFields",
                                ""))

    # ---- 10. 表单配置完整性检查 ----
    yida_cfg = cfg.get("yida") or {}
    form_uuid = yida_cfg.get("formUuid", "")
    if not form_uuid or str(form_uuid).startswith("填入"):
        checks.append(check("error", "表单配置", "宜搭 formUuid 未配置",
                            f"当前值: {form_uuid}",
                            "请在表单配置文件或设置中填写宜搭表单的 formUuid"))

    # ---- 11. 轻流字段清单检查 ----
    qf_fields_path = DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json"
    if qf_fields_path.exists():
        qf_fields = json.loads(qf_fields_path.read_text(encoding="utf-8"))
        qf_titles = {(f.get("queTitle") or "").strip() for f in qf_fields if f.get("queTitle")}
        # 检查映射表中的 queId 是否在轻流字段清单中存在
        qf_que_ids = {str(f.get("queId")) for f in qf_fields if f.get("queId") is not None}
        orphan_que = set()
        for r in matched:
            qid = r.get("轻流queId", "")
            if qid and qid != DEDUP_QUE_ID and qid not in qf_que_ids:
                orphan_que.add(f"{r.get('宜搭字段名', '?')}(queId={qid})")
        if orphan_que:
            checks.append(check("warn", "轻流字段", f"{len(orphan_que)} 个映射的 queId 在轻流字段清单中不存在",
                                "、".join(list(orphan_que)[:5]),
                                "可能轻流表单已修改/删除了该字段，请重新拉取轻流数据或更新映射表"))

    # ---- 汇总 ----
    return checks


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "msg": "用法: python preflight_check.py <表单配置名>"}, ensure_ascii=False))
        sys.exit(1)
    form_name = sys.argv[1]
    try:
        results = run(form_name)
        errors = [c for c in results if c["level"] == "error"]
        warnings = [c for c in results if c["level"] == "warn"]
        infos = [c for c in results if c["level"] == "info"]
        print(json.dumps({
            "ok": len(errors) == 0,
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "infos": len(infos),
                "total": len(results),
            },
            "checks": results,
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "msg": f"预检失败: {e}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
