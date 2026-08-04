# -*- coding: utf-8 -*-
"""步骤2：拉取宜搭表单组件定义 -> data/raw/<表单名>_宜搭组件.json，并生成映射草稿 CSV
接口文档: 获取表单内的组件信息 (GET /v1.0/yida/forms/formFields)
用法: python 02_fetch_yida_schema.py <表单配置名> [--no-draft]

前置要求: 宜搭目标表单须在宜搭设计器中手动创建（宜搭不提供创建表单定义的 API），
且字段标题(label) 需与轻流字段名(queTitle) 完全一致，自动映射(02b) 才能对齐。
详细说明见 docs/宜搭表单准备说明.md。
"""
import csv
import json
import sys
import urllib.parse
from common import (load_credentials, load_form_config, http_request, save_json,
                    get_dingtalk_token, yida_context, require_non_placeholder,
                    BASE_DIR, DATA_DIR, DINGTALK_API)

# 宜搭组件类型 -> 中文含义（用于草稿备注，辅助人工映射）
COMPONENT_CN = {
    "DateField": "日期", "TextField": "单行文本", "TextareaField": "多行文本",
    "NumberField": "数值", "RateField": "评分", "RadioField": "单选",
    "CheckboxField": "复选", "SelectField": "下拉单选", "MultiSelectField": "下拉复选",
    "CascadeSelectField": "级联选择", "CascadeDateField": "日期区间",
    "ImageField": "图片上传", "AttachmentField": "附件", "EmployeeField": "成员",
    "TableField": "子表单(明细)", "DepartmentSelectField": "部门",
}


def parse_label(label):
    """解析宜搭 label 字段：可能是 i18n JSON 字符串，也可能是普通文本，返回中文名"""
    if label is None:
        return ""
    if isinstance(label, dict):
        return label.get("zh_CN") or str(label.get("value", "")).strip('"').strip("'") or ""
    if isinstance(label, str):
        s = label.strip()
        if not s:
            return ""
        try:
            obj = json.loads(s)
        except Exception:
            return s
        if isinstance(obj, dict):
            if obj.get("zh_CN"):
                return obj["zh_CN"]
            if obj.get("value"):
                return str(obj["value"]).strip('"').strip("'")
        if isinstance(obj, str):
            return obj
        return s
    return str(label)


def flatten_components(result):
    """把 result 拍平为一维列表，子表单(TableField)的 children 解析后附带 parentId/parentName。"""
    flat = []

    def walk(items, parent=None):
        for c in items:
            node = {
                "fieldId": c.get("fieldId") or c.get("componentId") or "",
                "componentName": c.get("componentName") or c.get("componentType") or "",
                "label": parse_label(c.get("label")),
                "behavior": c.get("behavior") or "",
                "parentId": parent["fieldId"] if parent else "",
                "parentName": parent["label"] if parent else "",
                "isSub": bool(parent),
            }
            flat.append(node)
            children = c.get("children")
            if node["componentName"] == "TableField" and children:
                try:
                    kids = json.loads(children) if isinstance(children, str) else children
                    if isinstance(kids, list):
                        walk(kids, parent=node)
                except Exception as e:
                    print(f"  [警告] 解析子表单 {node['fieldId']} 的 children 失败: {e}")
        return flat

    return walk(result or [])


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 02_fetch_yida_schema.py <表单配置名> [--no-draft]")
    form_name = sys.argv[1]
    make_draft = "--no-draft" not in sys.argv[1:]

    cred = load_credentials()
    cfg = load_form_config(form_name)

    # 合并宜搭上下文（表单配置优先，凭证兜底）：appType/formUuid/systemToken/userId
    ctx = yida_context(cred, cfg)
    app_type = require_non_placeholder(ctx.get("appType"), "宜搭 appType(应用编码)")
    system_token = require_non_placeholder(ctx.get("systemToken"), "宜搭 systemToken(应用密钥)")
    user_id = require_non_placeholder(ctx.get("userId"), "钉钉 userId(操作人)")
    form_uuid = require_non_placeholder(ctx.get("formUuid"), "宜搭 formUuid(表单编码)")

    # 1) 获取钉钉企业内部应用 accessToken（用于本接口的 x-acs-dingtalk-access-token 头）
    token = get_dingtalk_token(cred)

    # 2) 调用 获取表单内的组件信息 接口
    params = urllib.parse.urlencode({
        "appType": app_type,
        "systemToken": system_token,
        "userId": user_id,
        "formUuid": form_uuid,
        "language": "zh_CN",
    })
    url = f"{DINGTALK_API}/v1.0/yida/forms/formFields?{params}"
    resp = http_request(url, method="GET", headers={"x-acs-dingtalk-access-token": token}, min_interval=0.2)

    if not resp.get("success"):
        sys.exit(f"[错误] 宜搭接口返回 success=false，原始返回: {json.dumps(resp, ensure_ascii=False)}")
    result = resp.get("result") or []
    if not result:
        sys.exit(f"[错误] 未获取到组件定义，原始返回: {json.dumps(resp, ensure_ascii=False)}")

    # 3) 保存全量原始组件
    save_json(DATA_DIR / "raw" / f"{form_name}_宜搭组件.json", resp)

    # 4) 拍平 + 生成映射草稿
    flat = flatten_components(result)
    print(f"共解析到 {len(flat)} 个组件（含子表单内子组件），其中子组件 {sum(1 for x in flat if x['isSub'])} 个")

    if make_draft:
        draft = BASE_DIR / "mappings" / f"{form_name}_mapping_draft.csv"
        with open(draft, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["componentId", "宜搭字段名", "componentName", "组件类型中文",
                        "是否子组件", "父组件componentId", "轻流queId", "轻流字段名",
                        "transform", "behavior", "备注"])
            for c in flat:
                cn = COMPONENT_CN.get(c["componentName"], "")
                note = ""
                if c["componentName"] == "TableField":
                    note = "子表单：其 children 已展开为下方子组件行"
                elif c["componentName"] in ("ImageField", "AttachmentField"):
                    note = "附件/图片：二期处理（需下载后重传）"
                elif c["componentName"] == "EmployeeField":
                    note = "成员：需钉钉 userId 映射"
                w.writerow([c["fieldId"], c["label"], c["componentName"], cn,
                            "Y" if c["isSub"] else "N", c["parentId"],
                            "", "", "", c["behavior"], note])
        print(f"映射草稿已生成: {draft}")
        print("下一步: 对照 data/raw/<表单名>_轻流字段清单.json，在草稿里填写「轻流queId/轻流字段名/transform」")
    else:
        # 仅打印结构概览
        for c in flat:
            prefix = f"  └─[{c['parentName']}] " if c["isSub"] else ""
            print(f"{prefix}{c['fieldId']}  <{c['componentName']}>  {c['label']}")


if __name__ == "__main__":
    main()
