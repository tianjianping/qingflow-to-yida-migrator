# -*- coding: utf-8 -*-
"""一键全自动迁移编排（四阶段）：

  阶段一 拉取   00 生成配置 -> 01 轻流数据 -> 02 宜搭组件 -> 02b 字段对齐 -> 02c 宜搭存量
  阶段二 对比   02d 三方对账，产出差异清单(待新建/待更新/跳过/源已删除)
  阶段三 格式化 03 只转换差异集为宜搭裸值格式
  阶段四 写入   04 按差异清单执行(新建 batchSave / 更新 insertOrUpdate)

写入宜搭是真实外部操作，默认只 dry-run 预览；加 --commit 才真实写入。

用法:
  python run_all.py <表单配置名>                      # 跑完四阶段, 04 仅预览(不落库)
  python run_all.py <表单配置名> --commit             # 全量真实写入宜搭
  python run_all.py <表单配置名> --commit --limit 5   # 仅试迁前 5 条
  python run_all.py <表单配置名> --force              # 02d 强制把已存在记录全部标记更新

前置:
  - config/表单对照表.csv 已加该表单行（轻流appKey + 宜搭formUuid）
  - credentials.json 凭证已填
"""
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CHECKPOINT_PATH = SCRIPT_DIR.parent / "data" / "checkpoint.json"

# 步骤 -> 断点续跑时的建议起点提示（P2-2）
STEP_HINT = {
    "00": "python run_all.py {form}",
    "01": "python 01_fetch_qingflow.py {form}",
    "02": "python 02_fetch_yida_schema.py {form}",
    "02b": "python 02b_automap.py {form}",
    "02c": "python 02c_fetch_yida_instances.py {form}",
    "02d": "python 02d_compare.py {form}",
    "03": "python 03_transform.py {form}",
    "04": "python 04_batch_create.py {form} --commit",
}


def _load_checkpoint():
    if CHECKPOINT_PATH.exists():
        try:
            with open(CHECKPOINT_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def mark_step(form, step, status):
    """P2-2: 记录每个表单每个步骤的最后结果，中断后可据此判断从哪一步续跑。
    原子写入，避免中断损坏检查点文件本身。"""
    try:
        data = _load_checkpoint()
        entry = data.setdefault(form, {})
        entry[step] = {"status": status, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        entry["_last"] = {"step": step, "status": status,
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHECKPOINT_PATH.with_name(CHECKPOINT_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        import os as _os
        _os.replace(str(tmp), str(CHECKPOINT_PATH))
    except Exception:
        pass  # 检查点是辅助信息，绝不因它失败而影响主流程


def run(step_name, args, form=None, step=None):
    print(f"\n{'='*60}\n>>> {step_name}\n{'='*60}")
    cmd = [sys.executable, "-X", "utf8", str(SCRIPT_DIR / args[0])] + args[1:]
    r = subprocess.run(cmd)
    if form and step:
        mark_step(form, step, "ok" if r.returncode == 0 else f"failed({r.returncode})")
    if r.returncode != 0:
        hint = STEP_HINT.get(step or "", "")
        tip = f"\n  修复后可从该步续跑: {hint.format(form=form)}" if hint and form else ""
        sys.exit(f"[中断] {step_name} 执行失败（退出码 {r.returncode}），请排查后重试。"
                 f"\n  进度已记录: {CHECKPOINT_PATH}{tip}")
    return r


def banner(title):
    print(f"\n{'#'*60}\n## {title}\n{'#'*60}")


def show_resume_hint(form):
    """开跑前打印上次进度，让中断续跑不再靠人工翻日志。"""
    data = _load_checkpoint().get(form) or {}
    last = data.get("_last")
    if not last:
        return
    state = "成功" if last["status"] == "ok" else f"失败({last['status']})"
    print(f"[上次进度] {form} 最后执行步骤 {last['step']} -> {state}  @ {last['at']}")
    if last["status"] != "ok":
        hint = STEP_HINT.get(last["step"], "")
        if hint:
            print(f"           可单独重跑该步: {hint.format(form=form)}")


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python run_all.py <表单配置名> [--commit] [--limit N] [--force]")
    form = sys.argv[1]
    commit = "--commit" in sys.argv
    force = "--force" in sys.argv
    limit_args = []
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            limit_args = ["--limit", sys.argv[i + 1]]

    show_resume_hint(form)

    banner("阶段一 · 拉取（轻流数据 / 宜搭组件+字段对齐 / 宜搭存量）")
    run("00 生成表单配置", ["00_gen_form_configs.py"], form, "00")
    run("01 拉取轻流数据", ["01_fetch_qingflow.py", form], form, "01")
    run("02 拉取宜搭组件", ["02_fetch_yida_schema.py", form], form, "02")
    run("02b 自动字段对齐", ["02b_automap.py", form], form, "02b")  # 不强制, 保护已存在的手工映射(如 skip 标记)
    run("02c 拉取宜搭存量", ["02c_fetch_yida_instances.py", form], form, "02c")

    banner("阶段二 · 数据对比（产出差异清单）")
    run("02d 数据对比", ["02d_compare.py", form] + (["--force"] if force else []), form, "02d")

    banner("阶段三 · 格式化（只转换差异集）")
    run("03 格式化转换", ["03_transform.py", form], form, "03")

    banner("阶段四 · 写入宜搭" + ("（真实写入）" if commit else "（预览，未落库）"))
    cmd = [sys.executable, "-X", "utf8", str(SCRIPT_DIR / "04_batch_create.py"), form] \
        + (["--commit"] if commit else []) + limit_args
    r = subprocess.run(cmd)
    mark_step(form, "04", "ok" if r.returncode == 0 else f"failed({r.returncode})")
    if r.returncode != 0:
        sys.exit(f"[中断] 04 写入失败（退出码 {r.returncode}），进度已记录: {CHECKPOINT_PATH}")
    if commit:
        print("\n✅ 四阶段全部完成（已写入宜搭）")
    else:
        print("\n以上为预览。确认无误后加 --commit 真实写入：")
        print(f"  python run_all.py {form} --commit")


if __name__ == "__main__":
    main()
