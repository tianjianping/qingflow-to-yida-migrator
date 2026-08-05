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

from common import load_form_config, load_credentials, iter_json_array, BASE_DIR, DATA_DIR

DEDUP_QUE_ID = "-17"
MIN_EXPIRE_DAYS = 3
SECONDS_PER_DAY = 86400


def check(level, category, title, detail="", suggestion=""):
    return {"level": level, "category": category, "title": title,
            "detail": detail, "suggestion": suggestion}


def build_manual_worklist(form_name, cfg, rows, matched, unmatched):
    """生成「宜搭手动搭建/调整工作清单」：需在宜搭设计器人工处理的待办列表。

    每项 {action, field, detail}：
      - 检查：宜搭字段未匹配轻流字段，需确认删除/改名/标 skip
      - 新增：轻流有字段但宜搭无可承接字段，数据将被忽略
      - 补建：必填系统字段（编号/数据ID）缺失
      - 补充：子表单子组件未匹配（父子表单已启用）
      - 配置：关联组件未在 config associations 登记
    """
    worklist = []
    alias_cfg = cfg.get("labelAliases") or {}
    qf_fields_path = DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json"
    qf_fields = json.loads(qf_fields_path.read_text(encoding="utf-8")) if qf_fields_path.exists() else []

    def _qid_str(v):
        return "" if v is None else str(v)

    # 已承接的轻流 queId（映射表已匹配 + 系统字段）
    matched_qids = set()
    for r in matched:
        qid = str(r.get("轻流queId", "") or "").strip()
        if qid:
            matched_qids.add(qid)

    # 02 最新映射草稿中的宜搭字段标题集合：判断「字段已在宜搭创建但映射未承接」。
    # 用户手动修改宜搭表单（新增/删除字段）并刷新结构后，草稿反映最新结构，
    # 据此避免把「已在宜搭存在、只是尚未对齐」的字段误报为需「新增」。
    draft_path = BASE_DIR / (cfg.get("mappingDraftFile") or f"mappings/{form_name}_mapping_draft.csv")
    yida_has = set()
    if draft_path.exists():
        with open(draft_path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {k.strip(): (v or "").strip() for k, v in r.items()}
                lbl = r.get("宜搭字段名", "")
                if lbl:
                    yida_has.add(lbl)
    # 别名反向展开：labelAliases = {宜搭标题: 轻流标题} -> {轻流标题: [宜搭标题]}
    alias_rev = {}
    for yl, ql in (cfg.get("labelAliases") or {}).items():
        alias_rev.setdefault(ql, []).append(yl)

    # 轻流重名字段（同标题多个 queId）：标题匹配无法一一对应，需人工核对
    qf_title_qids = {}
    for qf in qf_fields:
        if qf.get("parentQueId"):
            continue
        t = (qf.get("queTitle") or "").strip()
        if t:
            qf_title_qids.setdefault(t, []).append(_qid_str(qf.get("queId")))
    dup_qf_titles = {t for t, qids in qf_title_qids.items() if len(qids) > 1}

    # 1) 未匹配的宜搭字段 -> 检查/删除/改名
    for r in unmatched:
        yida_name = r.get("宜搭字段名", "?")
        cid = r.get("componentId", "?")
        worklist.append({
            "action": "检查",
            "field": yida_name,
            "detail": f"宜搭字段「{yida_name}」(componentId={cid}) 未匹配到轻流字段。"
                      f"请核对宜搭字段标题与轻流字段名是否一致（含别名配置 labelAliases）；"
                      f"若该字段无需迁移请在 mapping.csv 标 skip。",
        })

    # 2) 轻流有字段、宜搭无可承接 -> 在宜搭新增
    sys_que_ids = {"0", "-17", "1", "2"}
    align_pending = 0
    for qf in qf_fields:
        if qf.get("parentQueId"):
            continue  # 子表单子字段单独处理
        qid = _qid_str(qf.get("queId"))
        title = (qf.get("queTitle") or "").strip()
        if not title or qid in matched_qids or qid in sys_que_ids:
            continue
        # 已被别名承接的宜搭标题不再要求新增
        if qid in {str(v) for v in alias_cfg.values()}:
            continue
        # 轻流重名字段：自动匹配无法一一对应，归入「检查」而非「新增」
        if title in dup_qf_titles:
            continue
        # 轻流字段已在宜搭（最新草稿）中存在：字段已创建，仅需 02b 对齐，不要求「新增」
        if any(t in yida_has for t in [title] + alias_rev.get(title, [])):
            align_pending += 1
            continue
        worklist.append({
            "action": "新增",
            "field": title,
            "detail": f"轻流字段「{title}」(queId={qid}, 类型 {qf.get('queType')}) 在宜搭无同名/别名可承接字段，"
                      f"该字段数据将被忽略。如需迁移请在宜搭创建标题为「{title}」的字段（或配置 labelAliases 别名）后重新运行 02b。",
        })
    if align_pending:
        worklist.append({
            "action": "检查",
            "field": f"{align_pending} 个字段",
            "detail": f"有 {align_pending} 个轻流字段已在宜搭表单中存在（标题一致），但映射表尚未承接。"
                      f"请点击「格式化数据」执行字段对齐（02b）后重新查看预检；"
                      f"若对齐后仍无对应行，请核对标题全半角/空格差异或配置 labelAliases 别名。",
        })

    # 3) 必填系统字段缺失（编号 queId=0 / 数据ID queId=-17）
    for r in rows:
        if str(r.get("轻流queId", "") or "").strip() in ("0", DEDUP_QUE_ID):
            matched_qids.add(str(r.get("轻流queId", "")).strip())
    if "0" not in matched_qids:
        worklist.append({"action": "补建", "field": "编号",
                         "detail": "定位键「编号」未映射（轻流 queId=0）。请在宜搭创建标题为「编号」的 TextField 后重新运行 02b。"})
    if DEDUP_QUE_ID not in matched_qids:
        worklist.append({"action": "补建", "field": "数据ID",
                         "detail": "跨系统匹配键「数据ID」未映射（轻流 queId=-17）。请在宜搭创建标题为「数据ID」的 TextField 后重新运行 02b。"})

    # 4) 子表单子组件未匹配（父 TableField 已启用、子组件 skip）
    for r in rows:
        note = r.get("备注", "") or ""
        if "子表单子组件" in note and "未匹配" in note and not r.get("轻流queId"):
            worklist.append({
                "action": "补充",
                "field": r.get("宜搭字段名", "?"),
                "detail": f"子表单子组件「{r.get('宜搭字段名')}」未匹配轻流子字段。"
                          f"请在宜搭子表单中确认该子组件的标题与轻流子表单内的子字段标题一致。",
            })

    # 5) 关联组件未在 config associations 登记
    assoc_cfg = cfg.get("associations") or {}
    for r in rows:
        if r.get("componentName") == "AssociationFormField" and r.get("componentId") \
                and r.get("componentId") not in assoc_cfg and r.get("轻流queId"):
            worklist.append({
                "action": "配置",
                "field": r.get("宜搭字段名", "?"),
                "detail": f"关联组件「{r.get('宜搭字段名')}」(componentId={r.get('componentId')}) 未在表单 config 的 associations 段登记 "
                          f"targetForm/titleField，转换时该关联字段将为空。请补充 config 后重跑 02b/03。",
            })

    # 6) 重复承接：同一轻流字段映射到多个宜搭字段（如同名+别名并存），数据冗余需人工确认
    que_id_count = {}
    for r in matched:
        qid = str(r.get("轻流queId", "") or "").strip()
        if qid and qid != DEDUP_QUE_ID:
            que_id_count.setdefault(qid, []).append(r.get("宜搭字段名", "?"))
    for qid, names in que_id_count.items():
        if len(names) > 1:
            worklist.append({
                "action": "检查",
                "field": "、".join(names),
                "detail": f"轻流字段 queId={qid} 同时映射到 {len(names)} 个宜搭字段（{'、'.join(names)}）。"
                          f"同一值会写入多个字段（常见于同名与别名并存，如「省/自治区/直辖市」与「省」）。"
                          f"请确认是否需要，建议在宜搭删除冗余字段或保留其一。",
            })

    # 7) 轻流顶层重名字段（如两个「电话」）：提示项，不阻断迁移。
    #    子表单内与顶层同名字段（parentQueId 非空）属正常现象，不计入。
    #    若 mapping 已将该标题的每个顶层重名 queId 都承接，则无需提示。
    for t in sorted(dup_qf_titles):
        qids = qf_title_qids[t]
        # mapping 中已承接该标题的顶层行（排除子表单子组件行）
        mapped_qids = set()
        for r in rows:
            if (r.get("宜搭字段名", "") == t) and r.get("componentId") \
                    and "子表单子组件" not in (r.get("备注") or ""):
                qid = str(r.get("轻流queId", "") or "").strip()
                if qid:
                    mapped_qids.add(qid)
        uncovered = [q for q in qids if q not in mapped_qids]
        if not uncovered:
            continue  # 全部承接，无需手工操作
        chosen = sorted(mapped_qids)
        worklist.append({
            "action": "检查",
            "field": t,
            "detail": f"轻流顶层存在 {len(qids)} 个同名「{t}」字段（queId={'、'.join(qids)}），"
                      f"mapping 已承接 {'、'.join(chosen) or '无'}，另有 {len(uncovered)} 个未承接"
                      f"（queId={'、'.join(uncovered)}），其数据将被忽略。"
                      f"子表单内与顶层同名字段属正常现象，不计入。"
                      f"提示项不阻断迁移：若未承接字段为冗余副本（值已被承接字段覆盖）可忽略；"
                      f"如需迁移，请在 mapping.csv 为该 queId 增加一行或改选承接字段。",
        })
    return worklist


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
        # 流式遍历（内存 O(单条)），统计空数据ID 记录数
        try:
            src_iter = iter_json_array(raw_path)
        except Exception:
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw = raw.get("result", {}).get("result", [])
                src_iter = iter(raw)
            except Exception:
                src_iter = iter(())
        empty_did = 0
        total = 0
        for apply in src_iter:
            total += 1
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
        if total == 0:
            checks.append(check("warn", "源数据", "轻流源数据为空（0 条记录）",
                                "", "检查轻流 appKey 是否正确，或该应用是否确实无数据"))
        elif empty_did:
            checks.append(check("warn", "源数据", f"{empty_did} 条轻流记录数据ID(queId=-17)为空或缺失",
                                f"共 {total} 条，其中 {empty_did} 条无有效数据ID",
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
        import time, urllib.parse
        now = time.time()
        expiring = 0
        # 流式读取，仅抽样前 200 条（内存 O(单条)）
        try:
            src_iter = iter_json_array(raw_path)
        except Exception:
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw = raw.get("result", {}).get("result", [])
                src_iter = iter(raw)
            except Exception:
                src_iter = iter(())
        for idx, apply in enumerate(src_iter):
            if idx >= 200:
                break
            for a in (apply.get("answers") or []):
                if str(a.get("queId")) in att_que_ids:
                    vals = a.get("values") or []
                    for v in vals:
                        url = (v.get("value") or v.get("dataValue") or "") if isinstance(v, dict) else str(v)
                        if url and "expire" in url.lower():
                            # 尝试解析 expire 参数
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
        # 检查映射表中的 queId 是否在轻流字段清单中存在。
        # 清单已含子表单子字段（parentQueId 非空，01 collect_fields 收集），一并纳入合法集。
        qf_que_ids = {str(f.get("queId")) for f in qf_fields if f.get("queId") is not None}
        orphan_que = set()
        for r in matched:
            qid = r.get("轻流queId", "")
            if qid and qid != DEDUP_QUE_ID and qid not in qf_que_ids:
                # 子表单子组件行豁免：子字段只存在于轻流 tableValues 行内，
                # 若本地是旧清单（未重跑 01 收集子字段），由 02b load_qingflow_subfields 兜底，
                # 不应误报为"字段被删除"
                if "子表单子组件" in (r.get("备注") or ""):
                    continue
                orphan_que.add(f"{r.get('宜搭字段名', '?')}(queId={qid})")
        if orphan_que:
            checks.append(check("warn", "轻流字段", f"{len(orphan_que)} 个映射的 queId 在轻流字段清单中不存在",
                                "、".join(list(orphan_que)[:5]),
                                "可能轻流表单已修改/删除了该字段，请重新拉取轻流数据或更新映射表"))

    # ---- 汇总 ----
    worklist = build_manual_worklist(form_name, cfg, rows, matched, unmatched)
    return checks, worklist


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "msg": "用法: python preflight_check.py <表单配置名>"}, ensure_ascii=False))
        sys.exit(1)
    form_name = sys.argv[1]
    try:
        results, worklist = run(form_name)
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
                "worklist": len(worklist),
            },
            "worklist": worklist,
            "checks": results,
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "msg": f"预检失败: {e}"}, ensure_ascii=False))
        sys.exit(1)


def render_worklist_md(form_name, worklist, summary, check_count=None):
    """把「宜搭手动调整工作清单」渲染为 Markdown 文本（含表单基本信息）。

    供保存为本地 md 文件 / 一键复制使用。
    """
    from datetime import datetime
    lines = []
    lines.append("# 宜搭手动调整工作清单")
    lines.append("")
    lines.append(f"- **表单**：{form_name}")
    lines.append(f"- **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **预检摘要**：{summary.get('errors', 0)} 个错误 / "
                 f"{summary.get('warnings', 0)} 个警告 / {summary.get('infos', 0)} 个提示")
    if check_count:
        lines.append(f"- **检查项**：共 {check_count} 条")
    lines.append("")
    if not worklist:
        lines.append("> 当前无需手动调整宜搭表单。")
        lines.append("")
        return "\n".join(lines)
    lines.append(f"共 {len(worklist)} 项待处理，请按以下顺序在宜搭设计器中人工调整后，"
                 "重新执行「拉取数据」（勾选刷新宜搭结构）+「格式化数据」。")
    lines.append("")
    lines.append("| # | 动作 | 字段 | 说明 |")
    lines.append("|---|------|------|------|")
    for i, w in enumerate(worklist, 1):
        detail = (w.get("detail") or "").replace("\n", " ")
        lines.append(f"| {i} | {w.get('action', '')} | {w.get('field', '')} | {detail} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("### 动作含义")
    lines.append("")
    lines.append("- **检查**：宜搭字段未匹配到轻流字段 / 存在重名或重复承接，需人工确认删除、改名或标 skip。")
    lines.append("- **新增**：轻流有字段但宜搭无可承接字段，需在宜搭创建对应字段（标题与轻流字段名一致），或配置 labelAliases 别名。")
    lines.append("- **补建**：必填定位键（编号/数据ID）缺失，需在宜搭创建对应 TextField。")
    lines.append("- **补充**：子表单子组件未匹配轻流子字段，需核对宜搭子组件标题。")
    lines.append("- **配置**：关联组件未在表单 config 的 associations 段登记 targetForm/titleField。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
