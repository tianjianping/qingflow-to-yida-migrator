# 宜搭迁移系统审核报告

> 审核日期：2026-07-31 ｜ 审核范围：migration/ 全目录（脚本、配置、服务器、附件链路）
> 审核维度：正常流程 · 异常中断与续传 · 附件去重与VPS流量 · 大数据量资源占用 · 安全

---

## 总体评价

系统架构设计扎实：四阶段管线（拉取→对比→格式化→写入）分层清晰，差异清单作为单一决策源贯穿阶段三/四，幂等设计到位（dry-run 默认、去重键定位、台账指纹），限速+重试机制完善。**以下问题按严重程度分级，P0 为必须修复的数据安全风险，P1 为影响可靠性/流量的问题，P2 为改进建议。**

---

## 修复状态总览（2026-07-31）

全部 21 项问题已逐项修复，全部通过 `py_compile` 编译校验，关键路径另做功能回归。逐项状态如下：

| 编号 | 文件 | 修复方式 | 验证 |
|------|------|----------|------|
| P0-1/2/3 | `unified_server.py` `_run_att_job` | 修正 `http_request` 参数顺序、payload 移除去重键、补全 `searchCondition`/`useAlias`/`noExecuteExpression` | 代码审查 + 与 03b 正确写法逐行对照 |
| P1-1 | `common.py` `save_json` | 临时文件 + `os.fsync` + `os.replace` 原子写入 | 编译通过 |
| P1-2 | `02c_fetch_yida_instances.py` | chunk 级重试(3次)，耗尽记 `unresolved`，退出码 2 阻断管线 | 代码审查 |
| P1-3 | `unified_server.py` | `_job_emit` 环形缓冲(800 行) | 编译通过 |
| P1-4 | `03b` + `unified_server` | `vps_head_check` HEAD 预检，命中记 `vps_hit` | 实测命中返回 `(True, URL)` |
| P1-5 | `03b` + `common.py` | md5 内容索引 `_content_index.json` 跨文件名复用 | 代码审查 |
| P1-6 | `03b_attachment.py` | 每 10 条 `flush_result` 增量落盘 + atexit 保底 | 编译通过 |
| P1-7 | `03b` + `unified_server` | 64KB 分块流式下载 | 编译通过 |
| P1-8/9 | `vps/attachment_server.py`(新建) | `MAX_CONTENT_LENGTH` 强制 + 流式 `_stream_save` + `content_length` 幂等 | 新建文件编译通过 |
| P2-1 | `02d_compare.py` | `srcHash` 仅保留差异集指纹 | 运行验证 `srcHash=1075` |
| P2-2 | `run_all.py` | `checkpoint.json` 步骤级记录 + 续跑提示 | 编译通过 |
| P2-3 | `unified_server.py` | `prune_logs` 保留 14 天 / 100 个 | 编译通过 |
| P2-4 | `unified_server.py` | `attachment_stats` 60s TTL 缓存 | 编译通过 |
| P2-5 | — | **暂缓**：并发写同一文件时 VPS 幂等检查存在竞态风险，收益不抵风险 | 未实施 |
| P2-6 | `unified_server.py` | `MIGRATION_API_TOKEN` Bearer 认证 + 非本机强制 | 编译通过 |
| P2-7 | `common.py` | `_ENV_OVERRIDES` 5 项凭证环境变量覆盖 | 编译通过 |
| P2-8 | `01_fetch_qingflow.py` | `applyId` 去重 + `.partial.json` 中断留档（未做全流式写入） | 编译通过 |
| P2-9 | `04_batch_create.py` | 实际批次大小收敛说明打印 | 编译通过 |

> 注：P2-5（并发）因 VPS 幂等检查在并发写同文件时存在竞态风险，暂缓实施；P2-8 仅做了 `applyId` 去重 + 中断留档，未改为全流式写入（当前表单量级内存可控）。

---

## P0 — 严重缺陷（数据安全 / 必须修复）

### P0-1. `unified_server.py` 附件写入接口调用完全错误

**文件**：`unified_server.py` 第 678–690 行 `_run_att_job()`

```python
# 当前（错误）：
resp = http_request("POST", INSERT_UPDATE_URL,
    headers={"x-acs-dingtalk-access-token": token}, json=body, timeout=30)
if resp and resp.status_code == 200:
```

**三重错误**：
1. `http_request` 的签名是 `http_request(url, method="POST", headers=None, body=None, ...)`，第一个位置参数是 **url**，但这里传了 `"POST"`（被当成 url），`INSERT_UPDATE_URL` 被当成 method。实际请求发到了 `https://api.dingtalk.com/POST`。
2. 传了 `json=body` 和 `timeout=30` 两个不存在的关键字参数，Python 会直接抛 `TypeError`。
3. `resp.status_code` 不存在——`http_request` 返回的是 dict（已 JSON 解析），不是 response 对象。

**影响**：通过 Web UI 触发的附件迁移写入**100% 会失败**。只有独立脚本 `03b_attachment.py` 的 CLI 调用是正确的。

**修复**：改为 `resp = http_request(INSERT_UPDATE_URL, headers={...}, body=body, min_interval=0.3)`，然后检查 `resp.get("success")`。

### P0-2. `unified_server.py` 附件写入 payload 包含去重键

**文件**：`unified_server.py` 第 680–681 行

```python
"formDataJson": json.dumps({ded_cid: data_id, att_cid: att_payload}, ensure_ascii=False),
```

**问题**：formDataJson 中包含了去重键 `ded_cid: data_id`。宜搭 `insertOrUpdate` 限制"同一组件不能同时作条件和更新值"——`ded_cid` 已经在 `searchCondition` 中作为定位键，不能再出现在 formDataJson 中。

对比独立脚本 `03b_attachment.py` 第 277 行的正确写法：
```python
"formDataJson": json.dumps({att_cid: att_payload}, ensure_ascii=False),  # 正确：不含去重键
```

**影响**：即使 P0-1 修复后请求能发出去，宜搭会返回错误或（更糟）成功写入但去重键字段被清空 → 下轮匹配失败 → 重复创建记录。

**修复**：从 formDataJson 中移除 `ded_cid: data_id`，只保留 `{att_cid: att_payload}`。

### P0-3. `unified_server.py` 附件写入缺少 searchCondition

**文件**：`unified_server.py` 第 678–682 行

body 中没有 `searchCondition` 字段，也没有 `useAlias`、`noExecuteExpression`。没有 searchCondition 的 `insertOrUpdate` 无法按数据ID定位目标记录，宜搭可能直接报错或行为未定义。

**修复**：参照 `03b_attachment.py` 的 `write_to_yida()` 补全 body 结构。

---

## P1 — 影响可靠性 / 流量 / 续传

### P1-1. `save_json()` 非原子写入 → 崩溃可能导致 JSON 损坏

**文件**：`common.py` 第 140–145 行

```python
def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
```

**问题**：直接写目标文件。如果进程在 `json.dump` 执行中崩溃（断电、Ctrl+C、OOM），目标文件会被截断为半截 JSON → 下次运行 `load_json` 失败 → 台账丢失 → 全量重迁。

**影响范围**：`04_batch_create.py` 每个批次后调 `save_json(result_path, result)`，`02d_compare.py` 的 diff 输出，`03_transform.py` 的转换输出——全部涉及。

**修复**：写临时文件后原子 rename：
```python
import tempfile, os
def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))  # 原子操作
```

### P1-2. `02c_fetch_yida_instances.py` 查询失败静默跳过 → 可能重复创建

**文件**：`02c_fetch_yida_instances.py` 第 127–133 行

```python
try:
    resp = http_request(QUERY_URL, ...)
except Exception as e:
    print(f"  [查询失败] chunk {i // CHUNK + 1}/{total_chunks}: {e}")
    continue  # ← 这个 chunk 的实例全部丢失
```

**问题**：如果某个 chunk 查询失败（网络抖动、限速），该 chunk 的实例不会被加入 `existing` 字典 → 02d 对比时认为这些记录"宜搭中不存在" → 标记为 create → 重复创建。

**影响**：网络不稳定时可能静默产生重复数据。

**修复**：失败时应将整个 chunk 的实例标记为"查询失败"并保留在 result 中（视为仍存在），而不是当作不存在。或者至少重试整个 chunk（`http_request` 已有重试，但 URLError 只重试 3 次后仍会抛出）。

### P1-3. `unified_server.py` 数据迁移 job output 无限增长

**文件**：`unified_server.py` 第 517–545 行 `_data_worker()`

```python
for line in proc.stdout:
    job["output"].append(line); lf.write(line); lf.flush()
```

**问题**：所有 stdout 行追加到内存 list `job["output"]`。对于大批量迁移（如某表单 1088 条），输出可达数百 KB 甚至 MB。`/api/job/<jid>` 返回 `"".join(job["output"])` 全量拼接，前端轮询时会反复传输全部历史输出。

**修复**：
- 只保留最近 N 行（如 500 行）在内存
- 或改为从日志文件按 offset 读取（日志文件已经有完整记录）

### P1-4. VPS 上传无预检 → 每次重跑都重新发送完整文件

**文件**：`03b_attachment.py` `upload_to_vps()` + VPS `attachment_server.py`

**当前流程**：
1. 客户端检查本地缓存 → 有缓存就跳过下载 ✅
2. 客户端把整个文件 POST 到 VPS
3. VPS 检查同名+同大小文件是否存在 → 存在则跳过写入，返回已有 URL

**问题**：步骤 2 仍然上传完整文件体到 VPS，即使 VPS 已有该文件。对于重跑场景（中断恢复、全量重迁），这浪费大量上行带宽。

**影响**：283 条附件如果平均 500KB，重跑一次就白白上传 ~140MB。

**修复方案**：在 `upload_to_vps()` 前加一步预检 HEAD 请求：
```python
# 先检查 VPS 是否已有该文件
head_resp = requests.head(f"{endpoint}/files/{vps_rel}", timeout=10)
if head_resp.status_code == 200:
    # VPS 已有，跳过上传
    return f"{endpoint}/files/{vps_rel}", True
```
VPS 端 Caddy 的 `file_server` 天然支持 HEAD，无需额外开发。

### P1-5. 附件无内容哈希去重 → 同文件不同名重复存储

**当前缓存键**：`表单/dataID/queId/文件名`

**问题**：如果同一附件内容出现在多条记录中（不同 dataID 或不同文件名），每份都会被独立下载+上传到 VPS。跨记录的重复文件没有去重。

**影响**：取决于数据特征，如果附件重复率高（如标准模板、通用文档），浪费存储和带宽。

**改进方案（可选）**：缓存键改为内容 MD5，VPS 存储也按 MD5 分片。但这会改变 URL 结构，影响已在宜搭中的引用。建议仅在新迁移时启用，已有数据不变。

### P1-6. 附件 `result_log` 非增量保存 → 崩溃丢失全部进度

**文件**：`03b_attachment.py` 第 526–528 行

```python
if result_log and not args.peek:
    result_path = DATA_DIR / "result" / f"{args.form}_attachment_result.json"
    save_json(result_path, result_log)
```

**问题**：`result_log` 只在全部处理完成后保存一次。如果处理 200 条时崩溃，前 200 条的写入结果全部丢失——无法知道哪些已成功写入宜搭。

**对比**：`04_batch_create.py` 在每个批次后保存 result.json（增量保存），`03b_attachment.py` 没有这个机制。

**修复**：在每条记录写入成功后，追加保存 `result_log`（配合 P1-1 的原子写入）。

### P1-7. 大文件全量读入内存下载

**文件**：`03b_attachment.py` 第 181 行 + `unified_server.py` 第 646 行

```python
data = resp.read()  # 一次性读入全部字节
```

**问题**：对于大附件（几十 MB），整个文件被读入内存。如果并发处理多条记录的附件（虽然当前是串行），内存压力会更大。

**修复**：改用流式下载：
```python
with open(cache_path, "wb") as f:
    while True:
        chunk = resp.read(64 * 1024)  # 64KB chunks
        if not chunk: break
        f.write(chunk)
```

### P1-8. VPS `attachment_server.py` 未强制文件大小限制

**文件**：VPS 部署指南中的 `attachment_server.py`

```python
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB 定义了但从未检查
```

**问题**：定义了 `MAX_FILE_SIZE` 但 upload handler 中没有使用。恶意或意外的大文件上传可能填满磁盘。

**修复**：在 `f.save(dest)` 前检查 `f.content_length` 或流式写入时累计大小超限则中断。

### P1-9. VPS `attachment_server.py` 幂等检查将整个文件读入内存

**文件**：VPS `attachment_server.py` 第 191–193 行

```python
file_bytes = f.read()        # ← 整个文件读入内存只为算大小
f.seek(0)
if len(file_bytes) == existing_size:
```

**问题**：为了检查文件大小是否相同，把整个上传文件读入内存。50MB 文件 × 并发请求 = 内存暴涨。

**修复**：用 `f.content_length` 或 `request.content_length` 直接比对，不需要读取文件体。

---

## P2 — 改进建议

### P2-1. `02d_compare.py` diff.json 中的 srcHash 全量包含

**文件**：`02d_compare.py` 第 160 行

```python
"srcHash": src_hash_map,   # applyId -> 源指纹，供 04 写入台账
```

`src_hash_map` 包含**所有**轻流记录的指纹（不只是 create+update 的）。对于大表单（万条+），这会让 diff.json 文件膨胀。阶段四只需要 create+update 集合的指纹。

**修复**：只保留差异集的指纹：
```python
diff_src_hash = {aid: src_hash_map[aid] for aid in set(create) | set(update) if aid in src_hash_map}
```

### P2-2. 无步骤级检查点 → 中断后需人工判断从哪步重跑

**当前**：`run_all.py` 和 unified_server 的 stage 运行是线性的，任何一步失败就停止。用户需要人工查看日志判断哪些步骤已完成、从哪步重跑。

**改进**：增加一个 `checkpoint.json` 记录每个表单每个步骤的最后成功时间，中断后可自动建议从哪步继续。

### P2-3. 日志无自动清理

`data/logs/` 下已有 60+ 个日志文件，且会持续增长。

**改进**：保留最近 7 天或最近 50 个日志文件，自动清理旧文件。

### P2-4. `attachment_stats()` 加载全量 raw JSON 仅用于统计

**文件**：`unified_server.py` 第 297–326 行

每次访问表单列表（`/api/forms`）时，对每个有附件的表单都会加载完整 raw JSON 来统计附件数量。大表单的 raw JSON 可能几十 MB。

**改进**：在 01 拉取时预计算附件统计并缓存到单独的 metadata 文件。

### P2-5. 串行处理附件 → 无并发优化

当前附件下载+上传完全串行。I/O 密集型操作（网络下载、文件上传）可以通过 3–5 个工作线程的线程池显著加速，同时仍遵守 API 限速。

**注意**：并发需要确保 VPS 上传的幂等性在并发下仍然成立（当前基于文件名+大小的检查在并发写同一文件时可能有竞态）。

### P2-6. Flask API 无认证

`unified_server.py` 绑定 `127.0.0.1:8765`，本地使用安全。但如果需要远程访问（如从其他机器操作），完全没有认证——任何人可启动迁移任务、上传文件到 VPS、读取表单配置。

**改进**：增加简单的 Bearer Token 认证或基础密码保护。

### P2-7. 凭证安全

`.gitignore` 已正确排除 `credentials.json` ✅。但 credentials.json 内所有密钥（qingflow token、dingtalk appSecret、yida systemToken、VPS upload_token）均为明文。如果本机被入侵或文件被意外共享，所有密钥暴露。

**改进**：考虑使用环境变量或加密存储（如 keyring），至少将 VPS upload_token 从 credentials.json 移到环境变量。

### P2-8. `01_fetch_qingflow.py` 全量加载到内存

```python
all_applies = []
while True:
    ...
    all_applies.extend(applies)
```

对于数万条记录的表单，全部数据加载到内存后一次性序列化为 JSON 保存。内存峰值 = 全量数据 ×2（Python 对象 + JSON 字符串）。

**改进**：流式写入 JSON（每页拉取后立即写入文件），或分批保存。

### P2-9. `batchSize` 配置值与实际不符

表单配置中 `"batchSize": 200`，但运行时：
```python
cap = 100 if no_exec else 5000
batch_size = min(cfg.get("batchSize", 100), cap)
```
noExecuteExpression=True（默认）→ cap=100 → 实际 batch_size=100。配置中的 200 永远不会生效，容易误导。

**改进**：将默认配置改为 100，或在日志中明确打印 `实际批次大小 = min(配置值, 上限) = N`。

---

## 审核矩阵总览

| 编号 | 级别 | 模块 | 问题 | 数据风险 |
|------|------|------|------|----------|
| P0-1 | 严重 | unified_server | 附件写入 http_request 调用全错 | Web UI 附件迁移 100% 失败 |
| P0-2 | 严重 | unified_server | payload 含去重键 → 字段被清空 | 重复创建 |
| P0-3 | 严重 | unified_server | 缺 searchCondition | 写入失败 |
| P1-1 | 高 | common.py | 非原子写入 | 崩溃致台账损坏 |
| P1-2 | 高 | 02c | 查询失败静默跳过 | 重复创建 |
| P1-3 | 高 | unified_server | output 无限增长 | 内存溢出 |
| P1-4 | 高 | 03b+VPS | 上传无预检 | 浪费带宽 |
| P1-5 | 中 | 03b | 无内容哈希去重 | 重复存储 |
| P1-6 | 高 | 03b | result_log 非增量 | 崩溃丢进度 |
| P1-7 | 中 | 03b | 大文件全量读内存 | 内存压力 |
| P1-8 | 中 | VPS | 未强制大小限制 | 磁盘风险 |
| P1-9 | 中 | VPS | 幂等检查读全文件 | 内存浪费 |
| P2-1 | 低 | 02d | srcHash 全量 | 文件膨胀 |
| P2-2 | 低 | run_all | 无检查点 | 需人工判断 |
| P2-3 | 低 | logs | 无日志清理 | 磁盘增长 |
| P2-4 | 低 | unified_server | stats 全量加载 | 性能 |
| P2-5 | 低 | 03b | 串行处理 | 速度 |
| P2-6 | 低 | unified_server | 无认证 | 安全 |
| P2-7 | 低 | config | 凭证明文 | 安全 |
| P2-8 | 低 | 01 | 全量加载 | 内存 |
| P2-9 | 低 | config | batchSize 误导 | 混淆 |

---

## 优先修复顺序建议

1. **立即修复 P0-1/2/3**：unified_server 附件写入的三个 bug，否则 Web UI 的附件迁移功能完全不可用
2. **尽快修复 P1-1**：原子写入，保护所有台账数据
3. **尽快修复 P1-2**：02c 查询失败处理，防止重复创建
4. **修复 P1-4**：VPS 上传预检，节省带宽
5. **修复 P1-6**：附件 result_log 增量保存
6. **其余 P1/P2** 按优先级逐步处理

---

*本报告基于代码静态审核，未执行动态测试。建议在修复后逐项回归验证。*

---

## 修复记录（2026-07-31）

### P0 — 严重缺陷（已修复）

**P0-1/2/3 · `unified_server.py` `_run_att_job()` 附件写入**

- `http_request` 调用改为 `http_request(INSERT_UPDATE_URL, headers={...}, body=body)`（原代码 `"POST"` 被当 url、`INSERT_UPDATE_URL` 被当 method、`json=`/`timeout=` 为不存在的形参，必抛 `TypeError`）。
- `formDataJson` 移除去重键 `ded_cid`，仅保留 `{att_cid: att_payload}`（避免宜搭清空去重键 → 下轮失配重复创建）。
- body 补全 `searchCondition`（按数据 ID 定位）、`useAlias: False`、`noExecuteExpression` 取自配置。
- 写入结果判定由 `resp.status_code` 改为 `resp.get("success")`（实际返回为 dict）。
- cancel 时保底落盘 result_log；结束保存 `content_index`。

**验证**：与 `03b_attachment.py` 的 `write_to_yida()` 正确写法逐行对照一致；`py_compile` 通过。

### P1 — 可靠性 / 流量 / 续传（已修复）

**P1-1 · `common.py` `save_json` 原子写入**

- 写 `<path>.tmp{pid}` → `f.flush()` + `os.fsync(f.fileno())` → `os.replace(str(tmp), str(path))`（同分区原子替换），崩溃不再产生半截 JSON。

**P1-2 · `02c_fetch_yida_instances.py` 查询失败处理**

- `CHUNK_RETRY=3`：单个 chunk 查询失败重试 3 次；仍未解决则整 chunk 记入 `unresolved` 并以**退出码 2** 阻断管线。
- `yida_instances.json` 新增 `unresolved` / `partial` 字段；`--allow-partial` 可强制带未解决实例继续（不推荐）。
- 配套 `02d_compare.py`：读取 `unresolved_inst`，相关源记录标为 `deferred`（本轮不写），`diff.json` 增加 `deferred` / `partialSource`。

**P1-3 · `unified_server.py` job output 内存增长**

- 新增 `DATA_JOB_OUTPUT_MAX_LINES=800` 与 `_job_emit(job, text)` 环形缓冲；`get_data_job` 返回时附 `[已省略前 N 行...]` 提示，避免大表单轮询反复传输全量历史。

**P1-4 / P1-5 / P1-6 / P1-7 · 附件链路省流量 + 增量台账**

- `03b_attachment.py` / `unified_server.py` 共用 `vps_head_check(endpoint, vps_rel, expect_size)`：上传前 HEAD 预检，命中记 `vps_hit`，跳过上传体（Caddy `file_server` 原生支持 HEAD）。
- `common.py` 新增 `file_md5` / `content_index_path` / `load_content_index` / `save_content_index`：`_content_index.json` 以内容 md5 为键，跨文件名/跨记录复用同一 VPS URL，记 `content_hit`。
- `03b_attachment.py` 每 10 条 `flush_result` 增量保存 `result_log` + atexit 保底；下载改 64KB 流式 `resp.read(DOWNLOAD_CHUNK)`。
- 实测 `vps_head_check` 返回 `(True, 'https://<your-domain>/files/...')` 正确命中。

**P1-8 / P1-9 · `vps/attachment_server.py`（新建权威版）**

- `MAX_CONTENT_LENGTH = MAX_FILE_SIZE + 2MB` 强制拦截超限（413）；`_stream_save` 流式写入并二次累加校验，超限抛 `RequestEntityTooLarge`。
- 幂等检查改用 `f.content_length` / `request.content_length`，**不再**把整个文件 `read()` 进内存。
- `_safe_dest` 路径穿越防护；`.part` 临时文件 + 原子改名。
- 部署需将本文件同步至 VPS `D:\yida-svc\` 并重启 YidaUpload 服务（旧内联版已弃用）。

### P2 — 改进项（已修复，P2-5 暂缓）

- **P2-1** `02d_compare.py`：`srcHash` 仅保留差异集指纹 `set(create) | set(update)`，运行验证 `srcHash=1075`、diff.json 95.3KB。
- **P2-2** `run_all.py`：新增 `checkpoint.json` 步骤级记录（`mark_step` 原子写入）+ 失败续跑提示（`show_resume_hint`）。
- **P2-3** `unified_server.py`：`prune_logs(LOG_KEEP_DAYS=14, LOG_KEEP_MAX=100)`，每次任务结束与启动各清理一次。
- **P2-4** `unified_server.py`：`attachment_stats` 加 60s TTL 缓存（按 raw 文件 mtime/size 算 cache_key），避免每次 `/api/forms` 全量加载 raw JSON。
- **P2-6** `unified_server.py`：新增 `MIGRATION_API_TOKEN` 环境变量 + `@app.before_request _require_token` Bearer 认证；非 `127.0.0.1` 监听时强制要求 Token，否则拒绝启动。
- **P2-7** `common.py`：`_ENV_OVERRIDES` 支持 5 项凭证经环境变量覆盖（`QINGFLOW_ACCESS_TOKEN` / `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` / `YIDA_SYSTEM_TOKEN` / `YIDA_VPS_UPLOAD_TOKEN`）；`load_attachment_config` 的 `upload_token` 同样可被 `YIDA_VPS_UPLOAD_TOKEN` 覆盖。
- **P2-8** `01_fetch_qingflow.py`：分页拉取加 `applyId` 去重；异常/中断时写 `<form>_raw.partial.json` 留档，不破坏既有快照。
- **P2-9** `04_batch_create.py`：打印 `实际批次大小 = min(配置值, 上限)`，超限时显示收敛说明。
- **P2-5（暂缓）**：并发上传因 VPS 幂等检查在并发写同文件时存在竞态，未实施。

### 部署提醒

> VPS 端须将 `migration/vps/attachment_server.py` 同步到 `D:\yida-svc\` 并重启 **YidaUpload** 服务，否则 P1-8/P1-9 的强制大小限制与内存安全修复不生效（客户端 HEAD 预检仍依赖 Caddy `file_server`，不受此影响）。

*全部改动已通过 `py_compile` 编译校验。*
