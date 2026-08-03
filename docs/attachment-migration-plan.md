# 附件迁移实现规划（路1：自有 VPS 静态托管）

> 目标：把轻流附件（24h 签名 URL）迁移到宜搭，规避「宜搭不自动转存外链」的限制。
> 方案：轻流下载 → 上传到自有阿里云 VPS（WinServer 2012）静态托管 → 写公网长期 URL 到宜搭。
> 决策已确认：① 存储=自有 VPS；② 访问控制=公网+防盗链白名单；③ 本地先缓存再上传；④ 上线=单条→小批量→全量；⑤ Web+上传=Caddy(自动HTTPS)+自建Python上传API；⑥ 域名HTTPS=已有域名+Caddy免费证书。

---

## 1. 总体架构

```
[轻流]  value 签名URL (24h 有效)
   │  requests.get 下载字节
   ▼
[迁移脚本 03b_attachment.py]  (可跑在本机或 VPS)
   │ 1. 下载到 data/attachment_cache/<表单>/<dataID>/<queId>/<文件名>   (先缓存 ✅)
   │ 2. POST https://域名/upload  →  上传到 VPS
   │ 3. 得公网 URL https://域名/files/<表单>/<dataID>/<queId>/<文件名>
   ▼
[构造宜搭附件数组]  [{downloadUrl, name, previewUrl, url, ext}]   (url 全指向 VPS)
   ▼
[insertOrUpdate]  按 textField_<去重键> 定位，只写附件字段，payload 不含去重键
   ▼
[宜搭]  预览/下载时浏览器直连 https://域名/files/...（Caddy 托管 + 防盗链校验）
```

关键事实（来自真实数据核对）：宜搭附件字段真实引用格式是
`/ossFileHandle?appType=...&fileName=APP_..._base64$$`（无 spaceId、指纹不可复刻），
**外部无法构造**。因此不自己造 url，而是直接填我们 VPS 的公网 URL——宜搭只存 URL、UI 直连预览，
与字段里"外部公网 URL 未转存"的形态一致（已实证可用）。

---

## 2. VPS 端（一次性搭建，WinServer 2012）

### 2.1 Caddy（HTTPS + 静态托管 + 防盗链 + 反代上传）

- 下载 `caddy.exe`（单文件，无需安装）放到 `D:\yida-svc\caddy.exe`。
- `D:\yida-svc\Caddyfile`：

```caddy
你的域名 {
    # —— 静态文件托管（宜搭拉取用）——
    handle /files/* {
        uri strip_prefix /files
        file_server {
            root D:\yida-attachments
        }
        # 防盗链：只允许宜搭/钉钉/本站来源，其余 403
        # 注意：Caddy v2 表达式语法，smoke test 时按实际 referer 微调
        @hotlink expression {http.request.header.Referer} not_matches "(dingtalk\.com|yida.*\.com|你的域名)"
        respond @hotlink 403
    }

    # —— 上传接口反代到 Python 服务 ——
    reverse_proxy /upload 127.0.0.1:8000
}
```

- Caddy 自动向 Let's Encrypt 申请并续期证书（解决免费证书问题），无需手动管理。
- 以 Windows 服务方式常驻：用 `winsw` 或 `nssm` 把 `caddy.exe run` 注册为服务。
- 防火墙：放行 80/443（Let's Encrypt 校验需 80 临时开放）。

### 2.2 自建 Python 上传服务（attachment_server.py）

跑在 VPS，监听 `127.0.0.1:8000`，只负责接收上传（静态 GET 由 Caddy 处理）。

```python
# D:\yida-svc\attachment_server.py  (示意骨架)
import os, secrets, hashlib
from flask import Flask, request, jsonify

app = Flask(__name__)
ROOT = r"D:\yida-attachments"
UPLOAD_TOKEN = os.environ["YIDA_UPLOAD_TOKEN"]   # 随机长 token，防任意上传
ALLOWED_EXT = {".pdf",".xlsx",".xls",".doc",".docx",".png",".jpg",".jpeg",".zip",".csv"}

@app.post("/upload")
def upload():
    if request.headers.get("X-Upload-Token") != UPLOAD_TOKEN:
        return jsonify(err="forbidden"), 403
    rel = request.form.get("path", "").replace("\\", "/").strip("/")
    if ".." in rel or not rel:
        return jsonify(err="bad path"), 400
    f = request.files.get("file")
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(err="ext blocked"), 400
    dest = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # 幂等：已存在直接返回已有 URL
    if os.path.exists(dest) and os.path.getsize(dest) == (len(f.read()) or 0):
        f.seek(0)
    f.save(dest)
    return jsonify(url=f"https://你的域名/files/{rel}")

@app.get("/health")
def health():
    return "ok"

# 生产用 waitress:  waitress-serve --listen=127.0.0.1:8000 attachment_server:app
```

- 依赖：`flask`、`waitress`（VPS 上 `pip install flask waitress`）。
- 启动：waitress 监听本机 8000，Caddy 反代 `/upload`；同样注册为 Windows 服务。
- **安全**：上传必须带 `X-Upload-Token`；扩展名白名单；路径禁 `..` 穿越。

---

## 3. 迁移脚本端（03b_attachment.py）

### 3.1 配置（common.py credentials 新增段）

```ini
[attachment_storage]
endpoint    = https://你的域名
upload_url  = https://你的域名/upload
upload_token= <随机长token，与 VPS 端一致>
local_cache = data/attachment_cache
```

### 3.2 主流程

1. 读 `data/raw/<表单>_raw.json`；按 mapping CSV 找所有 `AttachmentField` 类组件的 queId
   （如 `示例表单` 的附件字段；有多个表单时按各自 mapping 表枚举）。
2. 每条记录取去重键 `dataID`（textField_<去重键>）。
3. 对附件 `values` 逐个：
   a. `value` = 轻流签名公网 URL → `requests.get`（带超时+重试）下载字节。
   b. **落本地缓存** `data/attachment_cache/<表单>/<dataID>/<queId>/<sanitized_name>`（先缓存再上传）。
   c. 上传 VPS：`POST upload_url`，body=文件字节 + `path=<表单>/<dataID>/<queId>/<sanitized_name>` + `X-Upload-Token`。
   d. 取返回的公网 URL。
4. 构造数组 `[{downloadUrl:url, name, previewUrl:url, url:url, ext}]`。
5. `insertOrUpdate` 只写附件字段，按 `textField_<去重键>` 定位，payload 不含去重键（沿用现有逻辑）。
6. 幂等：本地缓存命中→不重拉轻流；VPS 同名存在→上传接口返回已有 URL，不重复写。

### 3.3 CLI

```
python 03b_attachment.py <表单> [--limit N] [--apply <dataID>] [--peek]
  --peek   只下载+构造 payload 并打印，不上传/不写宜搭（安全自检）
  --apply  指定单条 dataID
  --limit  小批量条数
```

---

## 4. 验证（阶段 C，按已确认节奏）

- **C1 单条 smoke test**
  `python 03b_attachment.py 示例表单 --apply <某dataID> --limit 1`
  → `ids/query` 回查 `attachmentField_<附件字段>` 的 url 是否为 `https://域名/files/...`；
  → 宜搭表单 UI 打开该记录，确认附件能预览+下载。
  → **同时观察 VPS 访问日志的 Referer**，据此微调 Caddy 防盗链白名单（必要时放宽允许空 referer 兜底）。
- **C2 长期有效性**：延迟 >24h 复测（静态文件天然不过期，应仍可用）。
- **C3 小批量**：`--limit 10`。
- **C4 全量**：283 条，监控失败/重试。
- **C5 备份**：全量前用 `ids/query` 批量导出宜搭现有附件字段值，便于回滚。

---

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 轻流签名 URL 24h 过期 | 本地缓存命中则不重拉；缓存未建时需在 24h 内完成该记录上传 |
| VPS 带宽/容量 | 单文件多为发票/表格（KB~MB 级），283×≤2 量级很小 |
| 防盗链误伤宜搭预览 | smoke test 观察 referer 后微调；必要时允许空 referer 兜底 |
| VPS 单点 | 定期备份 `D:\yida-attachments\`；宜搭存的是 URL，换存储只需改 URL |
| 回滚 | 保留宜搭原值备份；如需还原，`insertOrUpdate` 写回备份值 |

---

## 6. 待办清单

- [x] 存储 = 自有 VPS（WinServer 2012）
- [x] 访问控制 = 公网 + 防盗链白名单
- [x] 缓存策略 = 先缓存再上传
- [x] 上线节奏 = 单条 → 小批量 → 全量
- [x] Web+上传 = Caddy(自动HTTPS) + 自建 Python 上传 API
- [x] 域名HTTPS = 已有域名 + Caddy 免费证书
- [ ] 实现 VPS 端：`Caddyfile` + `attachment_server.py` + 服务化
- [ ] 实现迁移脚本：`03b_attachment.py`（下载/缓存/上传/构造/写入）
- [ ] 跑 C1 单条 smoke test，按 referer 微调防盗链
- [ ] C2→C3→C4 递进全量

---

## 附：为什么不用之前评估的钉钉 storage API

`提交文件/获取文件上传信息/添加文件夹/获取空间列表/获取文件下载信息` 均属**钉钉通用企业存储（spaceId+dentryUuid）体系**，
而宜搭附件字段用的是 **`/ossFileHandle?appType=&fileName=APP_..._base64$$`（appType+内部指纹）体系**，
两套不互通；用 storage API 拿到的 `dentry` 无法变成宜搭能识别的附件 url（真实格式无 dentryUuid、指纹不可复刻）。
故路1 采用「自有 VPS 公网 URL」直写，完全绕开该体系，且经真实数据（外链未转存形态）佐证可行。
