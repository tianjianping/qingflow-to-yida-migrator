# -*- coding: utf-8 -*-
"""步骤1：分页拉取轻流表单数据 -> data/raw/<表单名>_raw.json

接口（已按实际文档核对）:
  POST {baseUrl}/app/{appKey}/apply/filter
  Header: accessToken
  Body:   {"pageSize": N, "pageNum": N, "type": N}   # type: 数据范围，8=全部数据
  返回:   errCode / errMsg / result{pageAmount, pageNum, pageSize, resultAmount,
          result[ {applyId, answers[ {queId, queTitle, queType, values[], tableValues[]} ]} ]}

用法: python 01_fetch_qingflow.py 示例表单
"""
import sys
from common import load_credentials, load_form_config, http_request, save_json, DATA_DIR


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


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 01_fetch_qingflow.py <表单配置名>")
    form_name = sys.argv[1]
    cred = load_credentials()
    cfg = load_form_config(form_name)

    qf = cred["qingflow"]
    qf_cfg = cfg["qingflow"]
    url = f"{qf['baseUrl']}/app/{qf_cfg['appKey']}/apply/filter"
    headers = {"accessToken": qf["accessToken"]}
    page_size = qf_cfg.get("pageSize", 100)

    all_applies = []
    seen_ids = set()      # applyId 去重：分页期间源数据变动可能导致同一条落在两页
    dup = 0
    page_num = 1
    page_amount = None   # 总页数
    result_amount = None  # 数据总量
    partial_path = DATA_DIR / "raw" / f"{form_name}_raw.partial.json"

    try:
        while True:
            body = {
                "pageSize": page_size,
                "pageNum": page_num,
                "type": qf_cfg.get("type", 8),
            }
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
                print(f"  数据总量: {result_amount}，总页数: {page_amount}")
                if result_amount == 0:
                    print("[警告] 该应用下没有数据（或 type 取值范围不含目标数据）")
                    break

            if not applies or page_num >= page_amount:
                break
            page_num += 1
    except BaseException as e:
        # 中断/异常时把已拉到的页落到 .partial.json，保留现场供排查；
        # 正式的 _raw.json 保持上一次的完整快照不被破坏。
        if all_applies:
            save_json(partial_path, all_applies)
            print(f"[中断] 已拉取 {len(all_applies)} 条，暂存至 {partial_path}（正式快照未被覆盖）")
        raise

    print(f"共拉取 {len(all_applies)} 条" + (f"（去重丢弃 {dup} 条重复 applyId）" if dup else ""))
    if result_amount and len(all_applies) != result_amount:
        print(f"[警告] 拉取条数({len(all_applies)}) 与接口报告的总量({result_amount}) 不一致，请检查")

    save_json(DATA_DIR / "raw" / f"{form_name}_raw.json", all_applies)
    # 拉取成功后清理残留的中断暂存文件，避免误用
    try:
        if partial_path.exists():
            partial_path.unlink()
    except Exception:
        pass

    # 输出字段清单（queId / queTitle / queType / 是否子表单），辅助填写映射表
    fields = {}
    for apply in all_applies:
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
                }
            elif ans.get("tableValues"):
                fields[qid]["hasTableValues"] = True
    field_list = sorted(fields.values(), key=lambda x: str(x["queId"]))
    save_json(DATA_DIR / "raw" / f"{form_name}_轻流字段清单.json", field_list)
    print(f"字段清单共 {len(field_list)} 个字段，请据此将 轻流queId/字段名 填入映射表 CSV")


if __name__ == "__main__":
    main()
