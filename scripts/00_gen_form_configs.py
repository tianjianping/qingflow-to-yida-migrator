# -*- coding: utf-8 -*-
"""步骤0：根据 config/表单对照表.csv 批量生成/更新表单配置文件

对照表列: 表单名, 轻流appKey, 宜搭formUuid, 启用(Y/N), 备注
  - 宜搭 appType 是应用级编码，已统一放在 credentials.json 的 yida.appType，不在对照表中
  - 新增要搬迁的表单：在 CSV 加一行（启用=Y），运行本脚本即可生成 config/forms/<表单名>.json
  - 已存在的配置文件：只更新 appKey/formUuid 两个字段，其余（映射文件、批量参数、系统字段等）保持不变

用法: python 00_gen_form_configs.py
"""
import csv
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from common import CONFIG_DIR, save_json

REGISTRY = CONFIG_DIR / "表单对照表.csv"
FORMS_DIR = CONFIG_DIR / "forms"


def _read_xlsx_rows(path):
    """读取被 Excel/WPS 存成 xlsx 格式的文件（仅第一个工作表），返回 [dict]"""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
        sheet_name = next(n for n in z.namelist() if re.match(r"xl/worksheets/sheet1\.xml$", n))
        root = ET.fromstring(z.read(sheet_name))
        matrix = []
        for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
            cells = {}
            for c in row.findall("s:c", ns):
                ref = c.get("r", "")
                col = re.match(r"[A-Z]+", ref).group(0)
                idx = 0
                for ch in col:
                    idx = idx * 26 + (ord(ch) - 64)
                v = c.find("s:v", ns)
                val = v.text if v is not None else ""
                if c.get("t") == "s" and val != "":
                    val = shared[int(val)]
                cells[idx - 1] = val or ""
            width = max(cells) + 1 if cells else 0
            matrix.append([cells.get(i, "") for i in range(width)])
    if not matrix:
        return []
    header = [h.strip() for h in matrix[0]]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in matrix[1:]]


def load_registry_rows(path):
    """兼容三种情况：utf-8 CSV / GBK CSV / 被 Excel 另存为 xlsx 格式的 .csv"""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"PK":
        return _read_xlsx_rows(path)
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    sys.exit(f"[错误] 无法识别对照表编码: {path}")

PLACEHOLDER_HINTS = ("填入", "XXXXXX", "xxx")


def is_placeholder(value):
    v = (value or "").strip()
    return (not v) or any(h in v for h in PLACEHOLDER_HINTS)


def default_config(name, row):
    return {
        "formName": name,
        "qingflow": {
            "appKey": row["轻流appKey"],
            "pageSize": 100,
            "type": 8,
        },
        "yida": {
            "formUuid": row["宜搭formUuid"],
        },
        "mappingFile": f"mappings/{name}_mapping.csv",
        "batchSize": 200,
        "noExecuteExpression": True,
        "asynchronousExecution": False,
        "systemFields": {
            "originCreateTime": "填入宜搭「原创建时间」日期组件的componentId，不需要则留空",
            "originApplier": "填入宜搭「原提交人」成员组件的componentId，不需要则留空",
        },
    }


def main():
    if not REGISTRY.exists():
        sys.exit(f"[错误] 对照表不存在: {REGISTRY}")

    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
            for r in load_registry_rows(REGISTRY)]

    created, updated, skipped = [], [], []
    for row in rows:
        name = row.get("表单名", "")
        if not name:
            continue
        if row.get("启用", "Y").upper() != "Y":
            skipped.append(name)
            continue
        if is_placeholder(row.get("轻流appKey")):
            print(f"[警告] {name}: 轻流appKey 未填写，跳过")
            skipped.append(name)
            continue

        path = FORMS_DIR / f"{name}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("qingflow", {})["appKey"] = row["轻流appKey"]
            yida = cfg.setdefault("yida", {})
            yida["formUuid"] = row["宜搭formUuid"]
            updated.append(name)
        else:
            cfg = default_config(name, row)
            created.append(name)

        FORMS_DIR.mkdir(parents=True, exist_ok=True)
        save_json(path, cfg, quiet=True)  # 原子写入 + 重试，免疫瞬时文件锁

        warn = []
        if is_placeholder(row.get("宜搭formUuid")):
            warn.append("宜搭formUuid 未填写")
        tag = "（" + "，".join(warn) + "，不影响第1步拉取，第2步前需补齐）" if warn else ""
        print(f"  {'新建' if name in created else '更新'}: {path.name}{tag}")

    print(f"\n完成: 新建 {len(created)} 个，更新 {len(updated)} 个，跳过 {len(skipped)} 个")
    if created:
        print("下一步可直接拉取数据，例如: python 01_fetch_qingflow.py " + created[0])


if __name__ == "__main__":
    main()
