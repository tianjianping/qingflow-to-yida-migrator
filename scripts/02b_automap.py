# -*- coding: utf-8 -*-
"""步骤2b：自动对齐「宜搭组件 ↔ 轻流字段」，生成 03 可直接读取的正式映射表

这是「全自动迁移」链路里缺失的一环：
  02 生成的 _mapping_draft.csv 里「轻流queId / 轻流字段名 / transform」三列是空的，
  本脚本根据「宜搭字段名」与「轻流 queTitle」做同名匹配，并依据组件类型推断 transform，
  输出 03 需要的正式 mapping.csv。

输入:
  - data/raw/<表单>_轻流字段清单.json            (01 产出)
  - mappings/<表单>_mapping_draft.csv            (02 产出；若缺失则从 宜搭组件.json 重建)
输出:
  - <cfg["mappingFile"] 指向的路径>              (默认 mappings/<表单>_mapping.csv)
  - 终端打印匹配统计（已匹配 / 跳过 / 未匹配）

匹配与推断规则:
  - 同名匹配：宜搭字段名(label) == 轻流 queTitle（全等比较，含空格、全半角、标点差异均视为不同）
  - 前置要求：宜搭目标表单须在宜搭设计器中手动创建（宜搭无创建表单定义的 API），
    且字段标题(label) 需与轻流字段名保持一致，否则自动匹配会跳过该字段。
    详细说明见 docs/宜搭表单准备说明.md。
  - 跳过类型（永远标记 skip，不参与迁移）：
        AssociationFormField 关联表单、AttachmentField 附件、ImageField 图片、
        SerialNumberField 流水号、TableField 子表单(及其子组件)
  - transform 仅由「宜搭组件类型」决定：
        TextField/TextareaField/RichText        -> text
        NumberField/RateField                   -> number
        SelectField/RadioField/DropdownField    -> select
        DateField                               -> date
        CascadeDateField                        -> dateRange
        CheckboxField/MultiSelectField/CascadeSelectField/CitySelectField -> multi
        EmployeeField                           -> employee
        DepartmentSelectField                   -> department
        AddressField                            -> address
        CountrySelectField                      -> country
        LinkField                               -> link
        其它                                     -> text(兜底)

用法:
  python 02b_automap.py <表单配置名> [--force]
    --force  覆盖已存在的手工映射（默认：若已存在含映射的 mapping.csv 则跳过，保护手工编辑）
"""
import csv
import hashlib
import json
import sys
import importlib.util
from datetime import datetime
from pathlib import Path

from common import load_credentials, load_form_config, BASE_DIR, DATA_DIR

MAPPINGS_DIR = BASE_DIR / "mappings"

# 跳过类型（不参与迁移，transform=skip）
SKIP_TYPES = {"AssociationFormField", "AttachmentField", "ImageField",
              "SerialNumberField", "TableField"}

# 轻流系统字段 → 宜搭字段标题的映射
# 宜搭侧创建与轻流同名的字段即可自动匹配（无需加"原"前缀）
# 宜搭系统字段（创建时间/提交人）不可通过 API 修改，必须创建同名的自定义字段接收
SYSTEM_FIELD_MAP = {
    "0": {
        "yida_labels": ["编号"],
        "note": "轻流系统字段：编号（定位键，必须建立）",
        "required": True,
    },
    "-17": {
        "yida_labels": ["数据ID"],
        "note": "轻流系统字段：数据ID（匹配键，必须建立）",
        "required": True,
    },
    "1": {
        # queId=1 在轻流中标题为"申请人"，宜搭中也创建"申请人"字段即可同名匹配
        # 由于同名匹配已在主逻辑中处理，此处仅作为系统字段标注
        "yida_labels": ["申请人"],
        "note": "轻流系统字段：申请人（宜搭需创建同名自定义字段，系统内置的提交人字段不可API修改）",
        "required": False,
    },
    "2": {
        # queId=2 在轻流中标题为"申请时间"，宜搭中也创建"申请时间"字段即可同名匹配
        "yida_labels": ["申请时间"],
        "note": "轻流系统字段：申请时间（宜搭需创建同名自定义字段，系统内置的创建时间字段不可API修改）",
        "required": False,
    },
}

# 宜搭组件类型 -> transform
TRANSFORM_MAP = {
    "TextField": "text", "TextareaField": "text", "RichText": "text",
    "NumberField": "number", "RateField": "number",
    "SelectField": "select", "RadioField": "select", "DropdownField": "select",
    "DateField": "date", "CascadeDateField": "dateRange",
    "CheckboxField": "multi", "MultiSelectField": "multi",
    "CascadeSelectField": "multi", "CitySelectField": "multi",
    "EmployeeField": "employee", "DepartmentSelectField": "department",
    "AddressField": "address", "CountrySelectField": "country", "LinkField": "link",
}

# 跳过的备注文案
SKIP_NOTE = {
    "AssociationFormField": "关联表单：需在 config associations 中配置目标表单后启用（transform 列改为关联），否则跳过",
    "AttachmentField": "附件：由 03b_attachment.py 独立阶段迁移（下载→缓存→VPS→直写宜搭，主管线跳过）",
    "ImageField": "图片：由 03b_attachment.py 独立阶段迁移（主管线跳过）",
    "SerialNumberField": "流水号：宜搭系统按规则生成，API传值无效(已实测)，跳过",
    "TableField": "子表单：本期跳过",
}


def infer_transform(component_name):
    return TRANSFORM_MAP.get(component_name, "text")


def schema_fingerprint(yida):
    """计算宜搭组件结构指纹：按 componentId 排序后取 SHA1。

    用于检测宜搭表单结构是否变化（新增/删除字段），供自动重映射判断。
    """
    ids = sorted((c.get("componentId") or "") for c in yida)
    return hashlib.sha1(",".join(ids).encode("utf-8")).hexdigest()[:16]


def load_schema_fingerprint(form_name):
    """读取上次生成映射时的宜搭结构指纹。"""
    fp = MAPPINGS_DIR / f"{form_name}_yida_schema_hash.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8")).get("hash", "")
        except Exception:
            return ""
    return ""


def save_schema_fingerprint(form_name, yida):
    """写宜搭结构指纹（随 mapping.csv 一起落盘）。"""
    fp = MAPPINGS_DIR / f"{form_name}_yida_schema_hash.json"
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({
        "hash": schema_fingerprint(yida),
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def load_old_mapping(out_path):
    """读取旧 mapping.csv -> {componentId: row_dict}（用于保留手工映射）。"""
    old_map = {}
    if out_path.exists():
        try:
            with open(out_path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    r = {k.strip(): (v or "").strip() for k, v in r.items()}
                    cid = r.get("componentId", "")
                    if cid:
                        old_map[cid] = r
        except Exception:
            old_map = {}
    return old_map


def load_label_aliases(cfg):
    """读取「标题别名」配置：宜搭字段标题 -> 轻流字段标题。

    当宜搭字段标题与轻流字段名不一致（如宜搭"省/自治区/直辖市" vs 轻流"省"）时，
    通过别名让 02b 自动匹配上，避免手工补 mapping。支持:
      - 内置兜底：行政区标准长标题 -> 轻流短标题
      - config labelAliases 扩展: {"宜搭标题": "轻流标题"}（用户可覆盖/追加）
    返回 {宜搭标题: 轻流标题}
    """
    aliases = {k: v for k, v in (cfg.get("labelAliases") or {}).items()}
    # 内置兜底（setdefault: config 显式配置优先）
    aliases.setdefault("省/自治区/直辖市", "省")
    aliases.setdefault("市/自治州", "市")
    return aliases


def load_qingflow_fields(form_name):
    """读取轻流字段清单 -> {queTitle: [ {queId, queType, hasTableValues}, ... ]}

    轻流允许顶层存在多个同名字段（如两个顶层「电话」），因此按标题聚合为列表
    （保持清单原始顺序），由调用方通过 pick_qf_field 挑选：
      - 有旧映射（mapping.csv 中该 componentId 已映射的 queId）时优先复用旧映射；
      - 无旧映射时取清单中第一个，并在备注中提示全部同名 queId 供人工核对。
    跳过子表单子字段（parentQueId 非空）：它们只参与子表单行转换，
    不参与宜搭顶层字段的标题匹配（子字段标题如"姓名"可能干扰顶层同名匹配）。
    """
    path = DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json"
    if not path.exists():
        sys.exit(f"[错误] 未找到轻流字段清单: {path}\n请先运行 01_fetch_qingflow.py 拉取数据")
    arr = json.loads(path.read_text(encoding="utf-8"))
    title_map = {}
    for f in arr:
        if f.get("parentQueId"):
            continue  # 子表单子字段不参与顶层字段标题匹配
        t = (f.get("queTitle") or "").strip()
        if t:
            title_map.setdefault(t, []).append({
                "queId": f.get("queId"),
                "queType": f.get("queType"),
                "hasTableValues": bool(f.get("hasTableValues")),
            })
    return title_map


def pick_qf_field(qf, title, old_map=None, cid=None):
    """按标题在轻流字段清单中挑选字段（处理重名字段）。

    返回 (field_dict|None, dup_note)。field_dict 含 queId/queType/hasTableValues；
    dup_note 为空字符串表示无重名。重名时:
      1) 若 old_map 中该 componentId 已映射轻流queId，优先复用该 queId 对应字段
         （保护手工核对的映射）；
      2) 否则取清单顺序第一个，并在备注标注全部同名 queId 供人工核对。
    """
    fields = qf.get(title) or []
    if not fields:
        return None, ""
    if len(fields) == 1:
        return fields[0], ""
    qids = "、".join(str(x["queId"]) for x in fields)
    if old_map and cid:
        old_row = old_map.get(cid) or {}
        old_qid = str((old_row.get("轻流queId") or "").strip())
        if old_qid:
            for f in fields:
                if str(f["queId"]) == old_qid:
                    return f, (f"重名字段：轻流存在 {len(fields)} 个同名「{title}」"
                               f"（queId={qids}），沿用旧映射 queId={old_qid}")
    f0 = fields[0]
    return f0, (f"⚠️ 重名字段：轻流存在 {len(fields)} 个同名「{title}」"
                f"（queId={qids}），按清单顺序取 queId={f0['queId']}，请在 mapping.csv 核对")


def load_qingflow_subfields(form_name):
    """从 raw.json 提取子表单子字段定义。

    轻流子表单主字段（queType=18 且 hasTableValues=true）的子字段不在字段清单中，
    只能从原始数据 tableValues 里提取。返回:
      {父queId(str): {子queTitle: {queId, queType}}}
    """
    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"
    if not raw_path.exists():
        return {}
    from common import iter_json_array
    subs_map = {}
    try:
        for rec in iter_json_array(raw_path):
            for ans in rec.get("answers", []) or []:
                tv = ans.get("tableValues") or []
                if not tv:
                    continue
                pid = str(ans.get("queId"))
                bucket = subs_map.setdefault(pid, {})
                for row in tv or []:
                    for sub in row or []:
                        t = (sub.get("queTitle") or "").strip()
                        qid = sub.get("queId")
                        if t and qid is not None and t not in bucket:
                            bucket[t] = {"queId": qid, "queType": sub.get("queType")}
    except Exception as e:
        print(f"  [警告] 提取子表单子字段失败: {e}")
    return subs_map


def load_yida_draft(form_name):
    """读取 02 生成的映射草稿；若不存在则从 宜搭组件.json 重建。
    返回 list[{componentId, label, componentName, isSub, parentId, behavior, note}]
    """
    draft_path = MAPPINGS_DIR / f"{form_name}_mapping_draft.csv"
    if draft_path.exists():
        rows = []
        with open(draft_path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {k.strip(): (v or "").strip() for k, v in r.items()}
                rows.append({
                    "componentId": r.get("componentId", ""),
                    "label": r.get("宜搭字段名", ""),
                    "componentName": r.get("componentName", ""),
                    "isSub": r.get("是否子组件", "").upper() == "Y",
                    "parentId": r.get("父组件componentId", ""),
                    "behavior": r.get("behavior", ""),
                    "note": r.get("备注", ""),
                })
        return rows

    # 草稿缺失：从 宜搭组件.json 重建（复用 02 的拍平逻辑）
    comp_path = DATA_DIR / "raw" / f"{form_name}_宜搭组件.json"
    if not comp_path.exists():
        sys.exit(f"[错误] 未找到宜搭组件定义: {comp_path}\n请先运行 02_fetch_yida_schema.py")
    spec = importlib.util.spec_from_file_location(
        "m02", str(BASE_DIR / "scripts" / "02_fetch_yida_schema.py"))
    m02 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m02)
    resp = json.loads(comp_path.read_text(encoding="utf-8"))
    flat = m02.flatten_components(resp.get("result") or [])
    return [{
        "componentId": c["fieldId"],
        "label": c["label"],
        "componentName": c["componentName"],
        "isSub": c["isSub"],
        "parentId": c["parentId"],
        "behavior": c["behavior"],
        "note": "",
    } for c in flat]


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 02b_automap.py <表单配置名> [--force]")
    form_name = sys.argv[1]
    force = "--force" in sys.argv[1:]

    cfg = load_form_config(form_name)
    mapping_rel = cfg.get("mappingFile") or f"mappings/{form_name}_mapping.csv"
    out_path = BASE_DIR / mapping_rel

    qf = load_qingflow_fields(form_name)
    yida = load_yida_draft(form_name)
    assoc_cfg = cfg.get("associations") or {}
    qf_sub = load_qingflow_subfields(form_name)
    label_aliases = load_label_aliases(cfg)

    # 宜搭表单结构指纹：若宜搭新增/删除了字段，自动重映射（保留旧映射关系）
    cur_fp = schema_fingerprint(yida)
    old_fp = load_schema_fingerprint(form_name)
    old_map = load_old_mapping(out_path) if out_path.exists() else {}

    # 保护手工编辑：
    #  - mapping.csv 存在、有映射、宜搭结构未变化 -> 跳过（保护手工编辑）
    #  - mapping.csv 存在但宜搭结构已变化（新增/删除字段）-> 自动重映射，保留旧映射关系
    if out_path.exists() and not force and old_map:
        if old_fp and old_fp == cur_fp:
            print(f"[跳过] 已存在手工映射且宜搭结构未变化: {out_path}\n"
                  f"        如宜搭已新增/删除字段，结构指纹会自动触发重映射；"
                  f"如需强制按草稿重新生成，请加 --force")
            return
        print(f"[检测] 宜搭表单结构已变化（新增/删除字段），自动重新对齐映射表…\n"
              f"        旧映射 {len(old_map)} 行将被保留（含手工编辑），新增组件自动匹配")
        old_map = old_map  # 保留旧映射供 auto_match 继承
    else:
        old_map = {}

    matched, skipped, unmatched = 0, 0, 0
    out_rows = []
    unmatched_list = []

    for c in yida:
        cid = c["componentId"]
        label = c["label"]
        cn = c["componentName"]

        # 旧映射继承：同 componentId 且已有映射的行直接保留（保护手工编辑/别名匹配结果），
        # 宜搭新增的组件不受影响，会走下方正常自动匹配
        if old_map and cid in old_map and (old_map[cid].get("轻流queId") or "").strip():
            old = old_map[cid]
            out_rows.append([cid, label, cn,
                             old.get("轻流queId", ""), old.get("轻流字段名", ""),
                             old.get("transform", infer_transform(cn)), c["behavior"],
                             old.get("备注", "") or ""])
            matched += 1
            continue

        # 子表单子组件：若父 TableField 已匹配到轻流子表单主字段，
        # 则按子组件 label 匹配轻流子表单内的子字段（tableValues 中提取）
        if c["isSub"]:
            parent_id = c.get("parentId") or ""
            parent_queid = ""
            for prow in out_rows:
                if prow[0] == parent_id and prow[5] == "table":
                    parent_queid = str(prow[3] or "")
                    break
            if not parent_queid:
                out_rows.append([cid, label, cn, "", "", "skip", c["behavior"],
                                 "子表单子组件：父 TableField 未匹配到轻流子表单，跳过"])
                skipped += 1
                continue
            subs = qf_sub.get(parent_queid) or {}
            if label in subs:
                sq = subs[label]
                if cn == "AssociationFormField":
                    transform, note = "assoc", "子表单内关联：数据ID -> 宜搭instanceId（config associations 需含该组件）"
                else:
                    transform = infer_transform(cn)
                    note = "子表单子组件"
                out_rows.append([cid, label, cn, str(sq["queId"]), label, transform,
                                 c["behavior"], note])
                matched += 1
            else:
                out_rows.append([cid, label, cn, "", "", "skip", c["behavior"],
                                 "子表单子组件：轻流子表单内无同名子字段，跳过"])
                skipped += 1
            continue

        # 跳过类型
        if cn in SKIP_TYPES:
            # 子表单(TableField)：轻流侧存在同名字表单主字段(hasTableValues)时启用
            if cn == "TableField":
                q, dup_note = pick_qf_field(qf, label, old_map, cid)
                if q and q.get("hasTableValues"):
                    out_rows.append([cid, label, cn, str(q["queId"]), label, "table",
                                     c["behavior"],
                                     "子表单：轻流 tableValues 行数组 -> 宜搭对象数组"])
                    matched += 1
                else:
                    out_rows.append([cid, label, cn, "", "", "skip", c["behavior"],
                                     "子表单：轻流侧无对应子表单字段，跳过"])
                    skipped += 1
                continue
            # 关联表单：config associations 已配置该组件时参与迁移（数据ID -> 宜搭instanceId）
            if cn == "AssociationFormField" and cid in assoc_cfg:
                q, dup_note = pick_qf_field(qf, label, old_map, cid)
                if q:
                    note = "关联表单：config associations 已配置（数据ID -> 宜搭instanceId）"
                    if dup_note:
                        note = dup_note + "；" + note
                    out_rows.append([cid, label, cn, str(q["queId"]), label, "assoc", c["behavior"], note])
                    matched += 1
                else:
                    out_rows.append([cid, label, cn, "", "", "assoc", c["behavior"],
                                     "⚠️ 未匹配轻流字段，需人工确认"])
                    unmatched += 1
                continue
            qid = ""
            qname = ""
            q, _ = pick_qf_field(qf, label, old_map, cid)
            if q:  # 即便跳过也补全轻流侧信息，便于文档化
                qid = str(q["queId"])
                qname = label
            out_rows.append([cid, label, cn, qid, qname, "skip", c["behavior"],
                             SKIP_NOTE.get(cn, "本期跳过")])
            skipped += 1
            continue

        # 同名匹配
        q, dup_note = pick_qf_field(qf, label, old_map, cid)
        if q:
            out_rows.append([cid, label, cn, str(q["queId"]), label,
                             infer_transform(cn), c["behavior"], dup_note])
            matched += 1
        else:
            # 标题别名匹配：宜搭标题与轻流字段名不一致时（如 省/自治区/直辖市 -> 省）
            alias_label = label_aliases.get(label, "")
            alias_q, _ = pick_qf_field(qf, alias_label) if alias_label else (None, "")
            if alias_q:
                out_rows.append([cid, label, cn, str(alias_q["queId"]), alias_label,
                                 infer_transform(cn), c["behavior"],
                                 f"标题别名匹配: 宜搭「{label}」-> 轻流「{alias_label}」"])
                matched += 1
            else:
                # 系统字段匹配：检查是否是轻流系统字段的宜搭对应字段
                sys_match = None
                for sqid, sinfo in SYSTEM_FIELD_MAP.items():
                    if label in sinfo["yida_labels"]:
                        # 找到宜搭字段，确认轻流侧有对应的系统字段
                        qf_entry = None
                        for qf_title, qf_fields in qf.items():
                            for qf_data in qf_fields:
                                if str(qf_data["queId"]) == sqid:
                                    qf_entry = qf_data
                                    break
                            if qf_entry:
                                break
                        if qf_entry:
                            sys_match = (sqid, qf_entry, sinfo)
                            break

                if sys_match:
                    sqid, qf_entry, sinfo = sys_match
                    out_rows.append([cid, label, cn, sqid, qf_entry.get("queTitle", ""),
                                     infer_transform(cn), c["behavior"], sinfo["note"]])
                    matched += 1
                else:
                    out_rows.append([cid, label, cn, "", "", infer_transform(cn), c["behavior"],
                                     "⚠️ 未匹配轻流字段，需人工确认"])
                    unmatched += 1
                    unmatched_list.append(label)

    # 系统字段完整性检查：编号(queId=0) 和 数据ID(queId=-17) 必须映射
    matched_queids = set()
    for row in out_rows:
        qid = (row[3] or "").strip()
        if qid:
            matched_queids.add(qid)
    for sqid, sinfo in SYSTEM_FIELD_MAP.items():
        if sinfo["required"] and sqid not in matched_queids:
            labels = "或".join(sinfo["yida_labels"])
            print(f"[警告] 必需的系统字段未匹配: queId={sqid}（宜搭需建立标题为「{labels}」的字段）")
            # 追加一行占位提醒（不写 componentId，仅提示用户手工补全）
            out_rows.append(["", sinfo["yida_labels"][0], "TextField", sqid,
                             "", "text", "", f"⚠️ {sinfo['note']} —— 请在宜搭创建此字段后重新运行"])

    # 写正式 mapping.csv
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["componentId", "宜搭字段名", "componentName",
                    "轻流queId", "轻流字段名", "transform", "behavior", "备注"])
        w.writerows(out_rows)

    # 记录宜搭结构指纹（供下次运行检测结构变化自动重映射）
    save_schema_fingerprint(form_name, yida)

    print(f"已生成正式映射: {out_path}")
    print(f"  已匹配(自动): {matched}  跳过(skip): {skipped}  未匹配(需人工): {unmatched}")
    # 系统字段匹配状态
    sys_status = []
    for sqid, sinfo in SYSTEM_FIELD_MAP.items():
        ok = sqid in matched_queids
        tag = "✓" if ok else "✗"
        labels = "/".join(sinfo["yida_labels"])
        sys_status.append(f"  {tag} queId={sqid}({labels}){' [必需]' if sinfo['required'] else ''}")
    print("  系统字段匹配:")
    for s in sys_status:
        print(s)
    if unmatched_list:
        print("  未匹配字段: " + "、".join(unmatched_list))
        print("  → 请先核对宜搭字段标题(label)与轻流字段名是否完全一致（含空格/全半角差异），")
        print("    再在 mapping.csv 中手工补 轻流queId / transform，或直接标 skip")
    # 必需字段缺失提示
    missing_required = [sqid for sqid, sinfo in SYSTEM_FIELD_MAP.items()
                        if sinfo["required"] and sqid not in matched_queids]
    if missing_required:
        print(f"\n[严重] 必需的系统字段未匹配: {', '.join(missing_required)}")
        print("  宜搭表单中必须建立「编号」和「数据ID」字段，否则无法进行更新操作（insertOrUpdate）")
        print("  请在宜搭设计器中创建对应字段后，重新运行 02b 自动映射")


if __name__ == "__main__":
    main()
