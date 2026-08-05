# -*- coding: utf-8 -*-
"""阶段四 / 写入：纯执行器 —— 按阶段二差异清单把转换结果写入宜搭 -> data/result/
本步骤不再自行判定"该建还是该更"，全部决策来自 data/diff/<表单>_diff.json（02d 产物）：
  - diff.create 中的 applyId -> 新建（普通表单 batchSave；流程表单 processes/instances/start）
  - diff.update 中的 applyId -> 更新（普通表单 insertOrUpdate 按 dataID 定位；
                                流程表单 PUT /v1.0/yida/processes/instances 按 processInstanceId 定位）
  - diff.skip   中的 applyId -> 不发送任何请求
表单类型（普通/流程）自动判断：config/forms/<表单>.json 的 formType 优先，
否则读取 02c 探测缓存（data/raw/<表单>_form_type.json），也可用 --form-type 显式指定。
台账指纹: 写入成功后 result.json 记录 {inst, hash=源指纹(diff.srcHash)}，供下轮 02d 变更检测。
接口文档:
  - 批量创建(普通): POST /v1.0/yida/forms/instances/batchSave
  - 新增或更新(普通): POST /v2.0/yida/forms/instances/insertOrUpdate
  - 发起流程(流程): POST /v1.0/yida/processes/instances/start
  - 更新流程(流程): PUT /v1.0/yida/processes/instances
用法:
  python 04_batch_create.py 示例表单                 # 默认 dry-run（只构建并打印请求，不发送）
  python 04_batch_create.py 示例表单 --commit        # 真实写入（按差异清单创建/更新）
  python 04_batch_create.py 示例表单 --commit --limit 5   # 仅试迁前 5 条
  python 04_batch_create.py 示例表单 --form-type process # 显式按流程表单处理
前置: 需先完成 阶段二(02d 对比) 与 阶段三(03 格式化)；「强制全量更新」改在 02d --force。
安全: 默认 dry-run；必须加 --commit 才会真正写数据。
"""
import csv
import hashlib
import json
import sys
import atexit
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
from common import (load_credentials, load_form_config, http_request, load_json,
                    save_json, get_dingtalk_token, yida_context, DATA_DIR, DINGTALK_API, BASE_DIR)
from form_type import detect_form_type, FORM_TYPE_LABEL

BATCH_SAVE_URL = f"{DINGTALK_API}/v1.0/yida/forms/instances/batchSave"
INSERT_UPDATE_URL = f"{DINGTALK_API}/v2.0/yida/forms/instances/insertOrUpdate"
PROCESS_START_URL = f"{DINGTALK_API}/v1.0/yida/processes/instances/start"
PROCESS_UPDATE_URL = f"{DINGTALK_API}/v1.0/yida/processes/instances"

# 去重/定位键(轻流系统字段 queId=-17 数据ID)在不同组件类型下的检索条件映射
CONDITION_MAP = {
    "TextField": ("TEXT", "eq", "TextField"),
    "TextAreaField": ("TEXT", "eq", "TextAreaField"),
    "NumberField": ("NUMBER", "eq", "NumberField"),
    "DateField": ("DATE", "eq", "DateField"),
    "SelectField": ("TEXT", "eq", "SelectField"),
    "DropdownField": ("TEXT", "eq", "DropdownField"),
    "RadioField": ("TEXT", "eq", "RadioField"),
}


def find_dedup_field(form_name):
    """从映射表找轻流 queId=-17(数据ID)对应的宜搭组件，作为去重/更新定位键。
    返回 (componentId, componentName) 或 None(找不到或 componentId 为空)。"""
    mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
    if not mp.exists():
        return None
    with open(mp, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("轻流queId", "").strip() == "-17":
                cid = row.get("componentId", "").strip()
                if cid:
                    return (cid, row.get("componentName") or "TextField")
    return None


def find_bianhao_field(form_name):
    """从映射表找轻流 queId=0(编号)对应的宜搭组件。
    返回 (componentId, componentName) 或 None。"""
    mp = BASE_DIR / "mappings" / f"{form_name}_mapping.csv"
    if not mp.exists():
        return None
    with open(mp, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("轻流queId", "").strip() == "0":
                cid = row.get("componentId", "").strip()
                if cid:
                    return (cid, row.get("componentName") or "TextField")
    return None


def check_required_fields(form_name, has_updates, form_type="normal"):
    """强制校验：若有待更新记录，数据ID(queId=-17)必须已映射；
    普通表单的更新还需要编号(queId=0)（insertOrUpdate 定位依赖）。
    流程表单更新按 processInstanceId 定位（02c 已采集），编号非必需。
    即使无更新记录，也提前检查并提示缺失字段。"""
    missing = []
    dedup = find_dedup_field(form_name)
    bianhao = find_bianhao_field(form_name)
    if not dedup:
        missing.append("数据ID(queId=-17)")
    if form_type != "process" and not bianhao:
        missing.append("编号(queId=0)")
    if missing:
        if has_updates:
            sys.exit(
                f"[拦截] 缺少必需的定位字段: {', '.join(missing)}\n"
                f"有 {has_updates} 条待更新记录，但缺少定位字段无法执行更新。\n"
                f"请在宜搭表单中创建标题为「编号」和「数据ID」的 TextField，"
                f"然后重新运行 02b 自动映射 + 02d 对比 + 03 格式化后再写入。\n"
                f"（流程表单更新按 processInstanceId 定位，编号非必需；仅新建记录不受此限制，"
                f"但仍建议建立定位字段以便后续增量更新）")
        else:
            print(f"[提示] 缺少定位字段: {', '.join(missing)} —— 本次仅新建记录，不受影响。"
                  f"建议在宜搭建立「编号」和「数据ID」字段以便后续增量更新。")


def rec_hash(form_data):
    """(备用)对转换后数据生成指纹；正常流程使用 diff.srcHash（源指纹）。"""
    return hashlib.md5(
        json.dumps(form_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def build_body(ctx, cfg, form_data_list):
    """按文档构造 batchSave 请求体。formDataJsonList 必须是「JSON 字符串」数组。"""
    return {
        "appType": ctx["appType"],
        "systemToken": ctx["systemToken"],
        "userId": ctx["userId"],
        "formUuid": ctx["formUuid"],
        "noExecuteExpression": cfg.get("noExecuteExpression", True),
        "asynchronousExecution": cfg.get("asynchronousExecution", False),
        "keepRunningAfterException": True,
        "env": "vpc",
        "formDataJsonList": [json.dumps(d, ensure_ascii=False) for d in form_data_list],
    }


def batch_save(token, body):
    resp = http_request(BATCH_SAVE_URL, headers={"x-acs-dingtalk-access-token": token},
                        body=body, min_interval=0.5)
    if resp.get("success") is False:
        raise RuntimeError(f"宜搭返回 success=false: {json.dumps(resp, ensure_ascii=False)}")
    return resp.get("result") or []


def insert_or_update(token, ctx, cfg, dedup, rec):
    """按数据ID定位：命中则更新，未命中则新增。返回实例ID(或None)。
    注意: 检索键组件不能同时出现在更新值中(宜搭限制)，故从 formData 剔除该键。"""
    comp_id, comp_name = dedup
    value = rec["formData"].get(comp_id)
    if value is None or value == "":
        raise RuntimeError(f"去重键 {comp_id} 在记录 {rec['applyId']} 中无值，无法定位更新目标")
    sc_type, sc_op, sc_comp = CONDITION_MAP.get(comp_name, ("TEXT", "eq", comp_name))
    search_condition = json.dumps([{
        "key": comp_id,
        "value": str(value),
        "type": sc_type,
        "operator": sc_op,
        "componentName": sc_comp,
    }], ensure_ascii=False)
    form_data = {k: v for k, v in rec["formData"].items() if k != comp_id}
    body = {
        "appType": ctx["appType"],
        "systemToken": ctx["systemToken"],
        "userId": ctx["userId"],
        "formUuid": ctx["formUuid"],
        "noExecuteExpression": cfg.get("noExecuteExpression", True),
        "searchCondition": search_condition,
        "formDataJson": json.dumps(form_data, ensure_ascii=False),
        "useAlias": False,
        "env": "vpc",
    }
    resp = http_request(INSERT_UPDATE_URL, headers={"x-acs-dingtalk-access-token": token},
                        body=body, min_interval=0.3)
    if resp.get("success") is False:
        raise RuntimeError(f"宜搭返回 success=false: {json.dumps(resp, ensure_ascii=False)}")
    ids = resp.get("result") or []
    return ids[0] if ids else None


def start_process_instance(token, ctx, cfg, rec):
    """发起一条流程实例（流程表单新建）。
    返回 processInstanceId（宜搭 result 字段，字符串）。
    注意: 若表单绑定多个流程，请配置 processCode（config/forms/<表单>.json 的 processCode
    或 yida.processCode）；未配置时使用表单默认流程。"""
    body = {
        "appType": ctx["appType"],
        "systemToken": ctx["systemToken"],
        "userId": ctx["userId"],
        "formUuid": ctx["formUuid"],
        "formDataJson": json.dumps(rec["formData"], ensure_ascii=False),
        "language": "zh_CN",
    }
    pc = str(cfg.get("processCode") or (cfg.get("yida") or {}).get("processCode") or "").strip()
    if pc:
        body["processCode"] = pc
    dept = str(cfg.get("departmentId") or (cfg.get("yida") or {}).get("departmentId") or "").strip()
    if dept:
        body["departmentId"] = dept
    resp = http_request(PROCESS_START_URL, headers={"x-acs-dingtalk-access-token": token},
                        body=body, min_interval=0.5)
    if resp.get("success") is False:
        raise RuntimeError(f"宜搭返回 success=false: {json.dumps(resp, ensure_ascii=False)}")
    return resp.get("result") or None


def update_process_instance(token, ctx, cfg, rec, process_instance_id):
    """更新流程实例（流程表单更新）：PUT /v1.0/yida/processes/instances。
    按 processInstanceId 定位（来源 02c 采集的 didToInst），updateFormDataJson 传全量 formData。"""
    if not process_instance_id:
        raise RuntimeError(f"记录 {rec['applyId']} 缺少 processInstanceId，无法执行流程更新")
    body = {
        "processInstanceId": str(process_instance_id),
        "appType": ctx["appType"],
        "systemToken": ctx["systemToken"],
        "userId": ctx["userId"],
        "updateFormDataJson": json.dumps(rec["formData"], ensure_ascii=False),
        "language": "zh_CN",
    }
    resp = http_request(PROCESS_UPDATE_URL, method="PUT",
                        headers={"x-acs-dingtalk-access-token": token},
                        body=body, min_interval=0.3)
    if resp.get("success") is False:
        raise RuntimeError(f"宜搭返回 success=false: {json.dumps(resp, ensure_ascii=False)}")
    return resp.get("result") or process_instance_id


def load_process_inst_map(form_name):
    """读取 02c 产物 data/raw/<表单>_yida_instances.json 的 didToInst（轻流数据ID -> 宜搭流程实例ID）。
    流程表单更新按 processInstanceId 定位，需要该映射。"""
    p = DATA_DIR / "raw" / f"{form_name}_yida_instances.json"
    if not p.exists():
        return {}
    d = load_json(p)
    m = d.get("didToInst") or {}
    return {str(k): v for k, v in m.items()}


def normalize_done(result):
    """兼容旧版: 原 done[applyId] 存的是字符串(实例ID)，归一化为 {inst, hash}。
    hash=None 表示"未知是否变化"，下次运行会按更新处理，确保数据最新。"""
    done = result.get("done", {})
    for aid, v in list(done.items()):
        if isinstance(v, str):
            done[aid] = {"inst": v, "hash": None}
    result["done"] = done
    return result


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python 04_batch_create.py <表单配置名> [--commit] [--limit N] [--only <applyId>]"
                 " [--form-type normal|process|auto] [--force]")
    form_name = sys.argv[1]
    commit = "--commit" in sys.argv
    if "--force" in sys.argv:
        print("[提示] --force 已迁移至阶段二: 请改用 python 02d_compare.py <表单> --force 后重跑 03/04")
    # 表单类型: auto=自动探测（config 覆盖 > 02c 探测缓存 > 接口探测），也可显式指定
    type_override = "auto"
    if "--form-type" in sys.argv:
        idx = sys.argv.index("--form-type")
        if idx + 1 >= len(sys.argv):
            sys.exit("[参数错误] --form-type 需要一个取值: normal | process | auto")
        type_override = sys.argv[idx + 1].strip().lower()
    if type_override not in ("normal", "process", "auto"):
        sys.exit(f"[参数错误] --form-type 取值无效: {type_override}（可选 normal / process / auto）")
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
        print(f"[定向更新] 仅处理 applyId={only}")

    cred = load_credentials()
    cfg = load_form_config(form_name)
    ctx = yida_context(cred, cfg)
    for k in ("appType", "systemToken", "userId", "formUuid"):
        if not ctx.get(k):
            sys.exit(f"[配置缺失] 宜搭 {k} 未填写（请在 credentials.json 或 forms/{form_name}.json 配置）")

    if type_override == "auto":
        form_type, type_src = detect_form_type(form_name)
    else:
        form_type, type_src = type_override, "cli"
    print(f"[表单类型] {form_name}: {FORM_TYPE_LABEL[form_type]}（来源 {type_src}）")
    if form_type == "process":
        print("  流程表单写入规则: 新建=POST processes/instances/start，更新=PUT processes/instances（按 processInstanceId）")
    else:
        print("  普通表单写入规则: 新建=batchSave，更新=insertOrUpdate（按数据ID 定位）")

    records = load_json(DATA_DIR / "transformed" / f"{form_name}_formdata.json")
    if only:
        records = [r for r in records if str(r.get("applyId")) == only]
        if not records:
            sys.exit(f"[错误] 转换结果中不存在 applyId={only}，请先确认 03 已转换该记录")
    elif limit:
        records = records[:limit]
        print(f"[试迁模式] 仅处理前 {limit} 条")

    result_path = DATA_DIR / "result" / f"{form_name}_result.json"
    result = {"done": {}, "failed": {}}
    if result_path.exists():
        result = load_json(result_path)
    result = normalize_done(result)

    # C1: 台账增量落盘 —— 每 FLUSH_EVERY 条 flush 一次，进程退出时 atexit 保底。
    # 此前每条/每批全量重写整个台账（序列化+fsync），更新 N 条 = N 次全量写盘。
    FLUSH_EVERY = 10
    _pending = {"n": 0}

    def flush_ledger(force=False):
        if _pending["n"] >= FLUSH_EVERY or (force and _pending["n"] > 0):
            save_json(result_path, result, quiet=True)
            _pending["n"] = 0

    atexit.register(flush_ledger, True)

    # 去重/定位键: 轻流 dataID(queId=-17) 对应的宜搭组件
    dedup = find_dedup_field(form_name)
    bianhao = find_bianhao_field(form_name)
    if dedup:
        print(f"[定位键] 数据ID: {dedup[0]}({dedup[1]}) (queId=-17)")
    if bianhao:
        print(f"[定位键] 编号: {bianhao[0]}({bianhao[1]}) (queId=0)")
    if not dedup and not bianhao:
        print("[警告] 编号和数据ID字段均未映射，无法定位更新；待更新记录将被跳过")

    # 差异清单(阶段二 02d 产物): 本步骤唯一的决策来源
    diff_path = DATA_DIR / "diff" / f"{form_name}_diff.json"
    if not diff_path.exists():
        sys.exit(f"[缺产物] {diff_path} 不存在 —— 请先执行 阶段二(02d 数据对比) 与 阶段三(03 格式化)")
    # 防重复写入: 差异清单必须生成于上次写入台账之后。
    # 若直接复用旧清单，create 列表会把已迁移记录再 batchSave 一遍（重复插入）。
    if result_path.exists():
        try:
            dm = diff_path.stat().st_mtime
            rm = result_path.stat().st_mtime
            if dm < rm - 1:  # 1s 容差
                sys.exit(
                    f"[拦截] 差异清单({diff_path.name}) 早于上次写入台账({result_path.name})，"
                    f"直接执行会重复创建已迁移记录。\n"
                    f"请先重跑 阶段二(02d)+阶段三(03) 刷新差异清单（02c 会重新核对宜搭存量，"
                    f"清单将只保留真正需要新建/更新的记录）。")
        except OSError:
            pass
    diff = load_json(diff_path)
    create_ids = set(map(str, diff.get("create") or []))
    update_ids = set(map(str, diff.get("update") or []))
    skipped = len(diff.get("skip") or [])
    src_hash_map = diff.get("srcHash") or {}
    print(f"[差异清单] {diff.get('generatedAt')} 生成: "
          f"新建{len(create_ids)} / 更新{len(update_ids)} / 跳过{skipped}")

    # 按差异清单拆分转换结果（03 增量模式下 records 本就只含差异集）
    to_create, to_update, stale = [], [], 0
    for r in records:
        aid = str(r["applyId"])
        if aid in create_ids:
            to_create.append(r)
        elif aid in update_ids:
            to_update.append(r)
        else:
            stale += 1  # 不在差异清单中(转换结果比清单旧/全量转换)，不发送
    if stale:
        print(f"[提示] 转换结果中有 {stale} 条不在差异清单内，已忽略(如非预期请重跑 02d+03)")
    missing = (len(create_ids) + len(update_ids)) - (len(to_create) + len(to_update))
    if not limit and not only and missing > 0:
        print(f"[警告] 差异清单有 {missing} 条记录未出现在转换结果中，请重跑 阶段三(03) 后再写入")

    print(f"待创建 {len(to_create)} 条 | 待更新 {len(to_update)} 条 | 跳过(无变化) {skipped} 条")

    # 强制校验：有更新记录时，数据ID(queId=-17)必须映射；普通表单还需要编号(queId=0)
    check_required_fields(form_name, len(to_update), form_type)

    # 流程表单更新需按 processInstanceId 定位（02c 已把 轻流数据ID -> 宜搭流程实例ID 写入 didToInst）
    process_inst_map = load_process_inst_map(form_name) if form_type == "process" else {}

    if not to_create and not to_update:
        print("没有需要写入的数据")
        return

    if not commit:
        if to_create:
            if form_type == "process":
                r0 = to_create[0]
                body = {
                    "appType": ctx["appType"], "systemToken": ctx["systemToken"],
                    "userId": ctx["userId"], "formUuid": ctx["formUuid"],
                    "formDataJson": json.dumps(r0["formData"], ensure_ascii=False),
                    "language": "zh_CN",
                }
                pc = str(cfg.get("processCode") or (cfg.get("yida") or {}).get("processCode") or "").strip()
                if pc:
                    body["processCode"] = pc
                print("\n--- 创建请求预览(流程 start) ---")
                print(json.dumps(body, ensure_ascii=False)[:600])
            else:
                sample = build_body(ctx, cfg, [to_create[0]["formData"]])
                print("\n--- 创建请求预览(formDataJsonList[0]) ---")
                print(json.dumps(sample["formDataJsonList"][0], ensure_ascii=False)[:600])
        if to_update:
            if form_type == "process":
                r0 = to_update[0]
                pid = process_inst_map.get(str(r0["applyId"]))
                print("\n--- 更新请求预览(流程 PUT) ---")
                print(json.dumps({
                    "processInstanceId": pid or "<未在宜搭流程实例中找到该数据ID>",
                    "updateFormDataJson": json.dumps(r0["formData"], ensure_ascii=False)[:600],
                }, ensure_ascii=False)[:700])
            elif dedup:
                r = to_update[0]
                try:
                    inst_id = insert_or_update("__DRYRUN__", ctx, cfg, dedup, r)
                except Exception:
                    pass  # dry-run 不发送，仅展示将要构造的条件
                comp_id, comp_name = dedup
                sc_type, sc_op, sc_comp = CONDITION_MAP.get(comp_name, ("TEXT", "eq", comp_name))
                sc = json.dumps([{"key": comp_id, "value": str(r["formData"].get(comp_id)),
                                  "type": sc_type, "operator": sc_op, "componentName": sc_comp}],
                                ensure_ascii=False)
                fd = {k: v for k, v in r["formData"].items() if k != comp_id}
                print("\n--- 更新请求预览(insertOrUpdate) ---")
                print(f"searchCondition: {sc}")
                print(f"formDataJson  (已剔除去重键): {json.dumps(fd, ensure_ascii=False)[:600]}")
        print(f"\n[dry-run] 未实际发送。确认格式无误后加 --commit 执行。")
        return

    token = get_dingtalk_token(cred)
    total_ok, total_fail = 0, 0

    def ledger_hash(rec):
        """台账指纹 = 阶段二记录的源指纹（下轮 02d 据此判变化）"""
        return src_hash_map.get(str(rec["applyId"])) or rec_hash(rec["formData"])

    # 1) 新记录: 普通表单 batchSave 批量创建；流程表单逐条 start 发起流程
    if to_create:
        if form_type == "process":
            print(f"\n>>> 创建阶段(流程表单): {len(to_create)} 条 "
                  f"(POST /v1.0/yida/processes/instances/start 逐条发起)")
            for rec in to_create:
                aid = str(rec["applyId"])
                try:
                    pid = start_process_instance(token, ctx, cfg, rec)
                    result["done"][aid] = {"inst": pid, "hash": ledger_hash(rec)}
                    total_ok += 1
                    if total_ok <= 3 or total_ok % 10 == 0:
                        print(f"  [创建成功] {aid} -> processInstanceId={pid}")
                except Exception as e:
                    result["failed"][aid] = str(e)[:500]
                    total_fail += 1
                    print(f"  [创建失败] {aid}: {e}")
                _pending["n"] += 1
                flush_ledger()
        else:
            no_exec = cfg.get("noExecuteExpression", True)
            cap = 100 if no_exec else 5000
            cfg_bs = cfg.get("batchSize", 100)
            batch_size = min(cfg_bs, cap)
            print(f"\n>>> 创建阶段: {len(to_create)} 条，批次大小 {batch_size}"
                  + (f"（配置 batchSize={cfg_bs} 超过接口上限 {cap}"
                     f"[noExecuteExpression={no_exec}]，已按上限收敛）" if cfg_bs > cap else ""))
            for i in range(0, len(to_create), batch_size):
                batch = to_create[i:i + batch_size]
                print(f"  批次 {i // batch_size + 1}: {len(batch)} 条 ...")
                body = build_body(ctx, cfg, [r["formData"] for r in batch])
                try:
                    ids = batch_save(token, body)
                    for rec, inst_id in zip(batch, ids):
                        result["done"][str(rec["applyId"])] = {"inst": inst_id, "hash": ledger_hash(rec)}
                    total_ok += len(ids)
                except Exception as e:
                    print(f"  [批次失败] {e}\n  降级为逐条写入 ...")
                    for rec in batch:
                        try:
                            one = build_body(ctx, cfg, [rec["formData"]])
                            inst_id = batch_save(token, one)
                            if isinstance(inst_id, list):
                                inst_id = inst_id[0] if inst_id else None
                            result["done"][str(rec["applyId"])] = {"inst": inst_id, "hash": ledger_hash(rec)}
                            total_ok += 1
                        except Exception as e2:
                            result["failed"][str(rec["applyId"])] = str(e2)[:500]
                            total_fail += 1
                _pending["n"] += len(batch)
                flush_ledger()

    # 2) 已知且变化: 普通表单逐条 insertOrUpdate(按 dataID 定位)；流程表单逐条 PUT(按 processInstanceId)
    if to_update:
        if form_type == "process":
            print(f"\n>>> 更新阶段(流程表单): {len(to_update)} 条 "
                  f"(PUT /v1.0/yida/processes/instances 按 processInstanceId 定位)")
            no_pid = 0
            for rec in to_update:
                aid = str(rec["applyId"])
                pid = process_inst_map.get(aid)
                if not pid:
                    no_pid += 1
                    result["failed"][aid] = "宜搭流程实例中未找到该数据ID（02c 未采集到），无法按 processInstanceId 更新"
                    total_fail += 1
                    print(f"  [更新失败] {aid}: 未找到对应流程实例")
                    _pending["n"] += 1
                    flush_ledger()
                    continue
                try:
                    inst_id = update_process_instance(token, ctx, cfg, rec, pid)
                    result["done"][aid] = {"inst": inst_id, "hash": ledger_hash(rec)}
                    total_ok += 1
                except Exception as e:
                    result["failed"][aid] = str(e)[:500]
                    total_fail += 1
                    print(f"  [更新失败] {aid}: {e}")
                _pending["n"] += 1
                flush_ledger()
            if no_pid:
                print(f"[提示] {no_pid} 条更新记录未在宜搭流程实例中匹配到数据ID。"
                      f"可能原因: 02c 扫描的是旧类型缓存（用 --form-type process --force 重跑 02c），"
                      f"或这些记录在宜搭已被删除。可改用普通表单接口或人工核对。")
        elif not dedup:
            print("\n>>> 更新阶段被跳过(缺少去重键，无法定位)")
        else:
            print(f"\n>>> 更新阶段: {len(to_update)} 条 (insertOrUpdate 按数据ID定位)")
            for rec in to_update:
                aid = str(rec["applyId"])
                try:
                    inst_id = insert_or_update(token, ctx, cfg, dedup, rec)
                    result["done"][aid] = {"inst": inst_id, "hash": ledger_hash(rec)}
                    total_ok += 1
                except Exception as e:
                    result["failed"][aid] = str(e)[:500]
                    total_fail += 1
                    print(f"  [更新失败] {aid}: {e}")
                _pending["n"] += 1
                flush_ledger()

    flush_ledger(force=True)

    print(f"\n完成: 成功 {total_ok} 条, 失败 {total_fail} 条")
    print(f"结果文件: {result_path}")
    if result["failed"]:
        print(f"[提示] 有 {len(result['failed'])} 条失败，详见结果文件 failed 部分，修复后重跑本脚本即可")


if __name__ == "__main__":
    main()
