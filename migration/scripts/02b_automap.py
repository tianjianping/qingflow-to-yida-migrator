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
import json
import sys
import importlib.util
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
    "AssociationFormField": "关联表单：本期跳过（需重建关联）",
    "AttachmentField": "附件/图片：二期处理（需下载后重传）",
    "ImageField": "附件/图片：二期处理（需下载后重传）",
    "SerialNumberField": "流水号：系统自动生成，跳过",
    "TableField": "子表单：本期跳过",
}


def infer_transform(component_name):
    return TRANSFORM_MAP.get(component_name, "text")


def load_qingflow_fields(form_name):
    """读取轻流字段清单 -> {queTitle: {queId, queType}}"""
    path = DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json"
    if not path.exists():
        sys.exit(f"[错误] 未找到轻流字段清单: {path}\n请先运行 01_fetch_qingflow.py 拉取数据")
    arr = json.loads(path.read_text(encoding="utf-8"))
    title_map = {}
    for f in arr:
        t = (f.get("queTitle") or "").strip()
        if t:
            title_map[t] = {"queId": f.get("queId"), "queType": f.get("queType")}
    return title_map


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

    # 保护手工编辑：若已存在含映射的 mapping.csv，默认跳过
    if out_path.exists() and not force:
        old = list(csv.DictReader(open(out_path, encoding="utf-8-sig", newline="")))
        if any((r.get("轻流queId") or "").strip() for r in old):
            print(f"[跳过] 已存在手工映射: {out_path}\n"
                  f"        如需根据草稿重新自动生成，请加 --force")
            return

    qf = load_qingflow_fields(form_name)
    yida = load_yida_draft(form_name)

    matched, skipped, unmatched = 0, 0, 0
    out_rows = []
    unmatched_list = []

    for c in yida:
        cid = c["componentId"]
        label = c["label"]
        cn = c["componentName"]

        # 子表单子组件：不参与（03 不做嵌套）
        if c["isSub"]:
            out_rows.append([cid, label, cn, "", "", "skip", c["behavior"], "子表单子组件：本期跳过"])
            skipped += 1
            continue

        # 跳过类型
        if cn in SKIP_TYPES:
            qid = ""
            qname = ""
            if label in qf:  # 即便跳过也补全轻流侧信息，便于文档化
                qid = str(qf[label]["queId"])
                qname = label
            out_rows.append([cid, label, cn, qid, qname, "skip", c["behavior"],
                             SKIP_NOTE.get(cn, "本期跳过")])
            skipped += 1
            continue

        # 同名匹配
        if label in qf:
            q = qf[label]
            out_rows.append([cid, label, cn, str(q["queId"]), label,
                             infer_transform(cn), c["behavior"], ""])
            matched += 1
        else:
            # 系统字段匹配：检查是否是轻流系统字段的宜搭对应字段
            sys_match = None
            for sqid, sinfo in SYSTEM_FIELD_MAP.items():
                if label in sinfo["yida_labels"]:
                    # 找到宜搭字段，确认轻流侧有对应的系统字段
                    qf_entry = None
                    for qf_title, qf_data in qf.items():
                        if str(qf_data["queId"]) == sqid:
                            qf_entry = qf_data
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
