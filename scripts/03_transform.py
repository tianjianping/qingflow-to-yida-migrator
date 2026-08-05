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
                    iter_json_array, save_json, yida_context, BASE_DIR, DATA_DIR)

WARNINGS = []

# 关联组件(AssociationFormField)目标索引缓存: {targetForm: {"did2inst": {}, "did2title": {}}}
ASSOC_INDEX_CACHE = {}
# 当前表单的 associations 配置（main 中从 config 载入）: {componentId: {targetForm, titleField}}
ASSOC_CFG = {}

# 宜搭组件类型分类
TEXT_TYPES = {"TextField", "TextareaField", "RichText", "RadioField",
              "SelectField", "DropdownField"}
NUM_TYPES = {"NumberField", "RateField"}
LIST_TYPES = {"CheckboxField", "MultiSelectField", "CascadeSelectField",
              "CitySelectField", "DepartmentSelectField", "EmployeeField"}
SKIP_TYPES = {"ImageField", "AttachmentField", "SerialNumberField"}


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


def to_pinyin(text):
    """生成宜搭 regionText 所需的 en_US 拼音（空格分隔的无调音节，如 "shan xi sheng"）。
    pypinyin 为可选依赖：未安装时回退为原文（结构仍完整，仅展示时无拼音）。
    """
    try:
        from pypinyin import lazy_pinyin, Style
        return " ".join(lazy_pinyin(str(text), style=Style.NORMAL))
    except ImportError:
        return str(text)


def to_yida_address(value_dicts, apply_id, field_name):
    """轻流地址字段(queType=21) -> 宜搭 AddressField 裸值。

    轻流 values（按 id 有序）中每一项：value=区域名，otherInfo=6位行政区划代码(GB/T 2260)。
      - 有 otherInfo 且可解析为数字、value 非空  -> 结构化区域层：
            regionIds 追加 int(otherInfo)，regionText 追加 {"zh_CN": value, "en_US": 拼音}
      - 有 value 但无 otherInfo（通常为最末一级「详细地址」）-> 文本片段，拼接进 address
    宜搭目标格式: {"address": str, "regionIds": [int...], "regionText": [{"en_US", "zh_CN"}...]}
    纯文本兜底（整字段无任何行政区划代码）: 全部文本逗号拼接进 address，regionIds/regionText 留空，
    此时宜搭端将以纯文本形式展示（与旧版行为一致，但保留告警）。
    """
    region_ids, region_texts, detail_parts = [], [], []
    for v in value_dicts:
        text = str(v.get("value") or "").strip()
        code = v.get("otherInfo")
        if not text:
            continue
        if code is not None and str(code).strip().isdigit():
            region_ids.append(int(str(code).strip()))
            region_texts.append({"zh_CN": text, "en_US": to_pinyin(text)})
        else:
            detail_parts.append(text)
    if region_ids:
        return {"address": " ".join(detail_parts),
                "regionIds": region_ids, "regionText": region_texts}
    warn(apply_id, field_name, "地址字段无行政区划代码，按纯文本迁移，请人工核对")
    return {"address": ",".join(detail_parts), "regionIds": [], "regionText": []}


def find_title_queid(target_form):
    """从目标表单映射表找 titleField 对应的轻流 queId。
    返回 None 表示未配置/未找到（title 降级为数据ID）。
    """
    assoc = ASSOC_CFG.get("__title__", {})
    tf = assoc.get(target_form)
    if not tf:
        return None
    mp = BASE_DIR / "mappings" / f"{target_form}_mapping.csv"
    if not mp.exists():
        return None
    with open(mp, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("宜搭字段名") == tf.get("titleField"):
                qid = row.get("轻流queId", "").strip()
                return int(qid) if qid.isdigit() else None
    return None


def load_assoc_index(target_form, title_queid):
    """加载目标表单的关联索引（模块级缓存）：
      did2inst : {轻流数据ID: 宜搭instanceId}  来自 <目标>_yida_instances.json 的 didToInst
      did2title: {轻流数据ID: title字段值}     来自 <目标>_raw.json（title_queid 为 None 时跳过）
    """
    if target_form in ASSOC_INDEX_CACHE:
        return ASSOC_INDEX_CACHE[target_form]
    idx = {"did2inst": {}, "did2title": {}}

    inst_path = DATA_DIR / "raw" / f"{target_form}_yida_instances.json"
    if inst_path.exists():
        try:
            inst = load_json(inst_path)
            idx["did2inst"] = {str(k): v for k, v in (inst.get("didToInst") or {}).items()}
        except Exception:
            pass

    if title_queid:
        raw_path = DATA_DIR / "raw" / f"{target_form}_raw.json"
        if raw_path.exists():
            try:
                for rec in iter_json_array(raw_path):
                    did = None
                    title = None
                    for ans in rec.get("answers", []) or []:
                        qid = ans.get("queId")
                        vals = [v.get("value") for v in (ans.get("values") or [])
                                if v.get("value") is not None]
                        if qid == -17 and vals:
                            did = str(vals[0])
                        elif qid == title_queid and vals:
                            title = str(vals[0])
                    if did and title is not None:
                        idx["did2title"][did] = title
            except Exception:
                idx["did2title"] = {}
    ASSOC_INDEX_CACHE[target_form] = idx
    return idx


def to_yida_association(value_dicts, apply_id, field_name, component_id):
    """轻流关联组件 -> 宜搭 AssociationFormField 值。

    轻流侧值（queType=25 等关联类型）: value = 被关联表单记录的「数据ID」(=applyId)。
    宜搭侧目标: 关联表单组件，值格式（官方《创建或更新表单数据格式说明》）:
        [{"appType": 被关联表单应用编码, "formUuid": 被关联表单编码,
          "formType": "receipt", "instanceId": 被关联记录宜搭实例ID,
          "title": 展示标题, "subTitle": ""}]
    由于宜搭实例ID 是一次性生成的（跨系统不稳定），而轻流数据ID 才是跨系统业务键，
    因此以「轻流数据ID -> 宜搭instanceId」查找表（来自 <目标表单>_yida_instances.json 的
    didToInst，由 02c 阶段按数据ID 对账生成）完成值映射：轻流引用什么数据ID，就指向
    宜搭中该数据ID 对应的实例。

    未命中（目标记录未迁移 / 数据ID 对不上）-> 告警并返回 None（该关联不写入，
    避免写入指向不存在实例的脏关联）。

    field 的关联目标由表单 config 的 associations 段声明（见 to_yida_address 上方的
    ASSOC_CFG 注释），未配置的关联字段按旧行为跳过并告警。
    """
    texts = [str(v.get("value") or v.get("dataValue") or "").strip()
             for v in value_dicts if (v.get("value") or v.get("dataValue"))]
    if not texts:
        return None
    src_did = texts[0]

    assoc = ASSOC_CFG.get(component_id) or ASSOC_CFG.get(field_name)
    if not assoc:
        warn(apply_id, field_name, "关联组件未在 config associations 中配置目标表单，跳过")
        return None
    target_form = assoc.get("targetForm")
    if not target_form:
        warn(apply_id, field_name, "关联组件 associations.targetForm 未配置，跳过")
        return None

    try:
        t_cfg = load_form_config(target_form)
        t_ctx = yida_context(load_credentials(), t_cfg)
        t_form_uuid = (t_ctx.get("formUuid") or "").strip()
        t_app_type = (t_ctx.get("appType") or "").strip()
    except Exception as e:
        warn(apply_id, field_name, f"解析关联目标表单配置失败: {e}")
        return None
    if not t_form_uuid or not t_app_type or "填入" in t_form_uuid or "填入" in t_app_type:
        warn(apply_id, field_name,
             f"关联目标表单({target_form})的 formUuid/appType 未配置完整，跳过")
        return None

    title_queid = find_title_queid(target_form)
    idx = load_assoc_index(target_form, title_queid)
    inst_id = idx["did2inst"].get(src_did)
    if not inst_id:
        warn(apply_id, field_name,
             f"关联的联系人(数据ID={src_did})未在宜搭找到对应实例，"
             f"请确认目标表单({target_form})已迁移该记录后重跑 02c/03")
        return None

    title = idx["did2title"].get(src_did, src_did)
    return {"appType": t_app_type, "formUuid": t_form_uuid,
            "formType": "receipt", "instanceId": inst_id,
            "title": title, "subTitle": ""}


def extract_raw(component_name, value_dicts, apply_id, field_name, component_id=""):
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
        # 结构化地址转换：regionIds + regionText + address（详见 to_yida_address）
        return to_yida_address(value_dicts, apply_id, field_name)

    if cn == "CountrySelectField":
        # raw 应为国家代码（如 CN / PG）
        return texts[0] if texts else None

    if cn == "LinkField":
        # raw 应为 {link, text} 对象；这里轻流一般是链接字符串，先存字符串
        return texts[0] if texts else None

    if cn == "AssociationFormField":
        # 关联表单：按 config associations 配置将轻流数据ID 映射为宜搭 instanceId
        return to_yida_association(value_dicts, apply_id, field_name, component_id)

    if cn in SKIP_TYPES:
        note = {"ImageField": "图片", "AttachmentField": "附件(由03b_attachment.py独立阶段迁移)",
                "SerialNumberField": "流水号(宜搭系统生成,API传值无效)", "TableField": "子表单"}
        warn(apply_id, field_name, f"{note.get(cn, cn)}跳过（主管线不处理）")
        return None

    warn(apply_id, field_name, f"未识别的组件类型: {cn}，按文本兜底")
    return texts[0] if texts else None


def extract_table(table_values, apply_id, field_name, mapping):
    """轻流子表单(tableValues 行数组) -> 宜搭 TableField 值（对象数组）。

    轻流 tableValues 结构: list[list[子字段dict]]，每行是子表单一条记录，
    行内每个子字段 dict 含 queId/queTitle/queType/values[]（子字段值结构同顶层字段）。
    宜搭 TableField 目标格式（官方《创建或更新表单数据格式说明》）:
        [{子组件componentId: 值, ...}, ...]  // 每个元素是子表单一行

    子字段 -> 宜搭子组件的映射来自映射表中「父组件为 TableField 的子组件行」
    （02b 已按 queTitle 匹配轻流子字段 queId 并标 transform=assoc/text 等）。
    """
    rows_out = []
    for trow in table_values or []:
        row_obj = {}
        for sub in trow or []:
            sub_qid = str(sub.get("queId"))
            sub_vals = [v for v in (sub.get("values") or []) if isinstance(v, dict)]
            if not sub_vals:
                continue
            # 子组件行的轻流queId 即子字段 queId；顶层 answers 不含子字段，
            # 故 mapping[sub_qid] 只会命中子表单子组件行，无顶层冲突
            for mrow in mapping.get(sub_qid, []):
                mcn = mrow.get("componentName") or ""
                mcid = mrow.get("componentId") or ""
                mname = mrow.get("宜搭字段名") or ""
                raw_v = extract_raw(mcn, sub_vals, apply_id, mname, mcid)
                wrapped = wrap(mcn, raw_v)
                if wrapped is not None:
                    row_obj[mcid] = wrapped
        if row_obj:
            rows_out.append(row_obj)
    return rows_out or None


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

    # 关联组件配置: {componentId或字段名: {targetForm, titleField}}
    #   "__title__" 段存目标表单的 titleField 汇总，供 find_title_queid 使用
    global ASSOC_CFG
    assoc_cfg = cfg.get("associations") or {}
    ASSOC_CFG = {str(k): v for k, v in assoc_cfg.items() if isinstance(v, dict)}
    title_summary = {}
    for cid, a in ASSOC_CFG.items():
        tf = a.get("targetForm")
        if tf:
            title_summary.setdefault(str(tf), a)
    ASSOC_CFG["__title__"] = title_summary
    if assoc_cfg:
        print(f"[关联组件] 已配置 {len(assoc_cfg)} 个关联字段: "
              + ", ".join(f"{k}->{v.get('targetForm')}" for k, v in assoc_cfg.items()))

    que_map = {}
    for row in mapping:
        qid = (row.get("轻流queId") or "").strip()
        if qid and qid != "skip":
            que_map.setdefault(str(qid), []).append(row)
    if not que_map:
        sys.exit("[错误] 映射表中没有任何行填写了 轻流queId，请先完成映射（可用 02b_automap.py 自动对齐草稿）")

    raw_path = DATA_DIR / "raw" / f"{form_name}_raw.json"

    # 增量模式: 读取阶段二差异清单，只转换 create+update 差异集
    full = "--full" in sys.argv
    diff_path = DATA_DIR / "diff" / f"{form_name}_diff.json"
    if not full and diff_path.exists():
        diff = load_json(diff_path)
        wanted = set(map(str, (diff.get("create") or []) + (diff.get("update") or [])))
        print(f"[增量模式] 差异清单({diff.get('generatedAt')}): "
              f"新建{len(diff.get('create') or [])}+更新{len(diff.get('update') or [])}")
        # B3: 流式读取 raw，只反序列化需要的记录，内存 O(单条) 而非 390MB 全量
        sel = []
        try:
            for a in iter_json_array(raw_path):
                if str(a.get("applyId")) in wanted:
                    sel.append(a)
        except Exception:
            print("  [回退] 流式解析失败，改用全量解析（结果一致，仅内存开销增大）")
            raw_all = load_json(raw_path)
            sel = [a for a in raw_all if str(a.get("applyId")) in wanted]
        raw = sel
        print(f"  -> 从源数据中筛出 {len(raw)} 条待转换")
    elif not full:
        print("[全量模式] 未找到差异清单(阶段二未运行)，转换全部源数据")
        raw = load_json(raw_path)
    else:
        print("[全量模式] --full 指定，转换全部源数据")
        raw = load_json(raw_path)
    print(f"待转换 {len(raw)} 条，已映射字段 {len(que_map)} 个")

    # 系统字段处理：申请时间(queId=2)和申请人(queId=1)已在主循环中按映射表自动处理。
    # 仅当映射表中未包含这些 queId 时，才从 systemFields 配置或轻流顶层字段回退。
    origin_time_cid = ""
    origin_user_cid = ""
    sys_cfg = cfg.get("systemFields", {})
    # 映射表中已包含 queId=2 -> 跳过回退（主循环已写入）
    if "2" not in que_map:
        cid = sys_cfg.get("originCreateTime", "")
        if cid and not str(cid).startswith("填入"):
            origin_time_cid = cid
            print(f"[系统字段] 申请时间: 使用 systemFields 配置 componentId={cid}")
    else:
        print(f"[系统字段] 申请时间: 已通过映射表自动处理(queId=2)")
    # 映射表中已包含 queId=1 -> 跳过回退
    if "1" not in que_map:
        cid = sys_cfg.get("originApplier", "")
        if cid and not str(cid).startswith("填入"):
            origin_user_cid = cid
            print(f"[系统字段] 申请人: 使用 systemFields 配置 componentId={cid}")
    else:
        print(f"[系统字段] 申请人: 已通过映射表自动处理(queId=1)")

    records = []
    for apply in raw:
        apply_id = apply.get("applyId")
        form_data = {}
        for ans in apply.get("answers", []) or []:
            rows = que_map.get(str(ans.get("queId")))
            if not rows:
                continue
            # 子表单主字段（queType=18 + tableValues）：整表转换
            table_rows = [r for r in rows if (r.get("componentName") or "") == "TableField"]
            if table_rows:
                tv = ans.get("tableValues") or []
                for row in table_rows:
                    raw_v = extract_table(tv, apply_id, row.get("宜搭字段名"), que_map)
                    wrapped = wrap("TableField", raw_v)
                    if wrapped is not None:
                        form_data[row["componentId"]] = wrapped
                continue
            vals = extract_values(ans)
            if not vals:
                continue
            for row in rows:
                cn = row.get("componentName") or ""
                raw_v = extract_raw(cn, vals, apply_id, row.get("宜搭字段名"),
                                    row.get("componentId") or "")
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
        print("[提示] 请先查看告警清单，再执行 04 写入（少量可忽略：关联未命中走告警、附件由 03b 独立处理）")


if __name__ == "__main__":
    main()
