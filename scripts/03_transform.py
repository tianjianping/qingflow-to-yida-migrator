# -*- coding: utf-8 -*-
"""阶段三 / 格式化：将轻流原始数据按映射表转换为宜搭 formData（宜搭原生裸值格式）
用法: python 03_transform.py <表单配置名> [--full]
产物: data/transformed/<表单名>_formdata.json（可直接交给 04 写入）
      data/transformed/<表单名>_warnings.csv（告警清单）

增量模式: 若存在阶段二差异清单 data/diff/<表单>_diff.json，默认只转换
「待新建 + 待更新」的差异集（日常增量同步秒级完成）；加 --full 或清单缺失时全量转换。

关键格式依据《宜搭数据格式说明.md》「表单实例 FormData 结构」章节：
  - 绝大多数组件的值是「裸值」：文本=字符串、数值=数字、单选/下拉=字符串、
    日期=毫秒时间戳数字、成员/部门/多选=字符串数组、子表单=对象数组、地址=对象
  - 仅 国家/地区、超链接、关联表单 三类需要 [{}] 包装
  - 严禁把值包成 [{"value": ...}]（那是错误的，会导致宜搭存进字面量或 500）
依赖: 映射表（config 中 mappingFile 指向）的 componentName 列驱动格式
"""
import csv
import json
import sys
from datetime import datetime
from common import (load_credentials, load_form_config, load_mapping, load_json,
                    save_json, yida_context, BASE_DIR, DATA_DIR)

WARNINGS = []

# 宜搭组件类型分类
TEXT_TYPES = {"TextField", "TextareaField", "RichText", "RadioField",
              "SelectField", "DropdownField"}
NUM_TYPES = {"NumberField", "RateField"}
LIST_TYPES = {"CheckboxField", "MultiSelectField", "CascadeSelectField",
              "CitySelectField", "DepartmentSelectField", "EmployeeField"}
SKIP_TYPES = {"TableField", "ImageField", "AttachmentField",
              "SerialNumberField", "AssociationFormField"}


def warn(apply_id, field, msg):
    WARNINGS.append({"applyId": apply_id, "field": field, "msg": msg})


def extract_values(ans):
    """从轻流 answer 提取每个值的完整字典列表（保留 value/dataValue/optionId/userId 等）"""
    out = []
    for v in ans.get("values", []) or []:
        if isinstance(v, dict):
            out.append(v)
    return out


def to_text(v):
    return str(v.get("value") if v.get("value") is not None else v.get("dataValue") or "")


def to_ms_timestamp(text):
    s = str(text).strip()
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        return int(n if n > 1e11 else n * 1000)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {s}")


def extract_raw(component_name, value_dicts, apply_id, field_name):
    """按宜搭组件类型从轻流值列表中取出「内部值」"""
    cn = (component_name or "").strip()
    texts = [to_text(v) for v in value_dicts if to_text(v) != ""]

    if cn in TEXT_TYPES:
        return texts[0] if texts else None

    if cn in NUM_TYPES:
        try:
            return float(texts[0])
        except (ValueError, IndexError):
            warn(apply_id, field_name, f"数字解析失败: {texts}")
            return None

    if cn == "DateField":
        if not texts:
            return None
        try:
            return to_ms_timestamp(texts[0])
        except ValueError as e:
            warn(apply_id, field_name, str(e))
            return None

    if cn == "CascadeDateField":
        out = []
        for t in texts:
            try:
                out.append(str(to_ms_timestamp(t)))
            except ValueError:
                pass
        return out or None

    if cn in ("CheckboxField", "MultiSelectField", "CascadeSelectField", "CitySelectField"):
        return texts or None

    if cn == "EmployeeField":
        users = [str(v.get("optionId") or v.get("userId") or v.get("value"))
                 for v in value_dicts
                 if (v.get("optionId") or v.get("userId") or v.get("value"))]
        if not users:
            warn(apply_id, field_name, "成员字段未取到 userId")
            return None
        return users

    if cn == "DepartmentSelectField":
        depts = [str(v.get("deptId") or v.get("departmentId") or v.get("value"))
                 for v in value_dicts
                 if (v.get("deptId") or v.get("departmentId") or v.get("value"))]
        return depts or None

    if cn == "AddressField":
        warn(apply_id, field_name, "地址以纯文本结构迁移，请试迁后人工核对")
        return {"address": ",".join(texts), "regionIds": [], "regionText": []}

    if cn == "CountrySelectField":
        # raw 应为国家代码（如 CN / PG）
        return texts[0] if texts else None

    if cn == "LinkField":
        # raw 应为 {link, text} 对象；这里轻流一般是链接字符串，先存字符串
        return texts[0] if texts else None

    if cn == "AssociationFormField":
        warn(apply_id, field_name, "关联表单需重建关联，本期跳过")
        return None

    if cn in SKIP_TYPES:
        note = {"ImageField": "图片", "AttachmentField": "附件",
                "SerialNumberField": "流水号", "TableField": "子表单"}
        warn(apply_id, field_name, f"{note.get(cn, cn)}跳过（本期不处理）")
        return None

    warn(apply_id, field_name, f"未识别的组件类型: {cn}，按文本兜底")
    return texts[0] if texts else None


def wrap(component_name, raw):
    """按宜搭组件类型把内部值包装为「formDataJson 裸值」。

    绝大多数类型：值本身就是裸值，直接返回。
    仅 国家/地区、超链接、关联表单 需要 [{}] 包装。
    """
    if raw is None or raw == "" or raw == []:
        return None
    cn = (component_name or "").strip()

    # 仅这三种需要 [{}] 包装
    if cn == "CountrySelectField":
        return [{"value": raw}]
    if cn == "LinkField":
        return [raw] if isinstance(raw, dict) else [{"link": str(raw), "text": str(raw)}]
    if cn == "AssociationFormField":
        return [raw] if isinstance(raw, dict) else None

    # 其余一律裸值
    if cn in NUM_TYPES:
        return raw  # 数字
    if cn == "DateField":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    # 文本类 / 列表类（成员、多选、部门、级联、子表单）/ 地址对象 等：原样返回
    return raw


def find_system_value(apply, keys, que_titles):
    for k in keys:
        if apply.get(k):
            return apply[k]
    for ans in apply.get("answers", []) or []:
        if ans.get("queTitle") in que_titles:
            vals = extract_values(ans)
            if vals:
                return to_text(vals[0])
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 03_transform.py <表单配置名>")
    form_name = sys.argv[1]
    cfg = load_form_config(form_name)
    cred = load_credentials()
    mapping = load_mapping(cfg["mappingFile"])

    que_map = {}
    for row in mapping:
        qid = (row.get("轻流queId") or "").strip()
        if qid and qid != "skip":
            que_map.setdefault(str(qid), []).append(row)
    if not que_map:
        sys.exit("[错误] 映射表中没有任何行填写了 轻流queId，请先完成映射（可用 02b_automap.py 自动对齐草稿）")

    raw = load_json(DATA_DIR / "raw" / f"{form_name}_raw.json")

    # 增量模式: 读取阶段二差异清单，只转换 create+update 差异集
    full = "--full" in sys.argv
    diff_path = DATA_DIR / "diff" / f"{form_name}_diff.json"
    if not full and diff_path.exists():
        diff = load_json(diff_path)
        wanted = set(map(str, (diff.get("create") or []) + (diff.get("update") or [])))
        before = len(raw)
        raw = [a for a in raw if str(a.get("applyId")) in wanted]
        print(f"[增量模式] 差异清单({diff.get('generatedAt')}): "
              f"新建{len(diff.get('create') or [])}+更新{len(diff.get('update') or [])}"
              f" -> 从 {before} 条源数据中筛出 {len(raw)} 条待转换")
    elif not full:
        print("[全量模式] 未找到差异清单(阶段二未运行)，转换全部源数据")
    else:
        print("[全量模式] --full 指定，转换全部源数据")
    print(f"待转换 {len(raw)} 条，已映射字段 {len(que_map)} 个")

    sys_cfg = cfg.get("systemFields", {})
    origin_time_cid = sys_cfg.get("originCreateTime", "")
    origin_user_cid = sys_cfg.get("originApplier", "")
    if str(origin_time_cid).startswith("填入"):
        origin_time_cid = ""
    if str(origin_user_cid).startswith("填入"):
        origin_user_cid = ""

    records = []
    for apply in raw:
        apply_id = apply.get("applyId")
        form_data = {}
        for ans in apply.get("answers", []) or []:
            rows = que_map.get(str(ans.get("queId")))
            if not rows:
                continue
            vals = extract_values(ans)
            if not vals:
                continue
            for row in rows:
                cn = row.get("componentName") or ""
                raw_v = extract_raw(cn, vals, apply_id, row.get("宜搭字段名"))
                wrapped = wrap(cn, raw_v)
                if wrapped is not None:
                    form_data[row["componentId"]] = wrapped

        if origin_time_cid:
            t = find_system_value(apply, ("createTime", "applyTime", "createDate"),
                                  ("提交时间", "创建时间", "申请时间"))
            if t is not None:
                try:
                    form_data[origin_time_cid] = int(to_ms_timestamp(t))
                except ValueError as e:
                    warn(apply_id, "原创建时间", str(e))
            else:
                warn(apply_id, "原创建时间", "未在轻流数据中找到创建时间")
        if origin_user_cid:
            u = find_system_value(apply, ("applyUserId", "creatorId", "createUserId", "userId"),
                                  ("提交人", "申请人", "创建人"))
            if u is not None:
                form_data[origin_user_cid] = [str(u)]
            else:
                warn(apply_id, "原提交人", "未在轻流数据中找到提交人")

        if form_data:
            records.append({"applyId": apply_id, "formData": form_data})
        else:
            warn(apply_id, "-", "整条记录无可迁移字段，已跳过")

    save_json(DATA_DIR / "transformed" / f"{form_name}_formdata.json", records)

    warn_path = DATA_DIR / "transformed" / f"{form_name}_warnings.csv"
    with open(warn_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["applyId", "field", "msg"])
        w.writeheader()
        w.writerows(WARNINGS)
    print(f"转换完成: {len(records)} 条可写入, {len(WARNINGS)} 条告警 -> {warn_path}")
    if WARNINGS:
        print("[提示] 请先查看告警清单，再执行 04 写入（少量可忽略，如关联/附件跳过）")


if __name__ == "__main__":
    main()
