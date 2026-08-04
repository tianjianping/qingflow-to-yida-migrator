# VPS 部署指南 —— 附件迁移 · 自有 VPS 静态托管

> 适用：Windows Server · 域名 + Caddy HTTPS + Python 上传 API + 服务守护

---

## 0. 前提确认

在动手之前，请先确认/准备好：

| 项目 | 要求 | 状态 |
|------|------|------|
| VPS 操作系统 | Windows Server 2012（或其他 64 位 Windows） | 待确认 |
| 域名 | 你的域名（如 `files.your-domain.com`），DNS 已解析到 VPS 公网 IP | 待确认 |
| 公网 IP | 固定 IP，非动态 | 待确认 |
| 防火墙 | 80 (HTTP) + 443 (HTTPS) 入站放行 | 待配置 |
| Python | 3.8+（推荐 3.11+） | 待安装 |
| 管理员权限 | 安装软件、注册 Windows 服务需要 | 待确认 |

---

## 1. 目录规划

VPS 上建立以下目录结构：

```
D:\
├── yida-svc\                    ← 服务程序根目录
│   ├── caddy.exe                ← Caddy Web 服务器（单文件）
│   ├── Caddyfile                ← Caddy 配置
│   ├── attachment_server.py     ← Python 上传服务
│   └── nssm.exe                 ← 服务守护工具（可选位置）
│
└── yida-attachments\            ← 附件静态文件存储
    └── 示例表单\                 ← 按表单分目录（脚本上传时自动创建）
        └── <dataID>\
            └── <queId>\
                └── <文件名>
```

---

## 2. 安装 Python

WinServer 2012 对 Python 3.12+ 兼容性一般，推荐 **Python 3.11.x**（最后一个支持 WinServer 2012 的版本）。

### 步骤

1. 下载 [Python 3.11.9 Windows 64-bit installer](https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe)
2. 运行安装程序，**勾选 "Add Python to PATH"**，选择 "Install Now"
3. 安装完成后打开 PowerShell，验证：

```powershell
python --version
# Python 3.11.9
```

4. 安装依赖（用 pip）：

```powershell
pip install flask waitress
```

> **如果无法联网下载**：在本机下载好 wheel 文件 → 用 RDP 传上去手动安装。
> 需要：flask、waitress、markupsafe、jinja2、werkzeug、itsdangerous、blinker、click（flask 依赖链）。

---

## 3. 下载 Caddy

Caddy 是单文件 Web 服务器，无需安装，支持自动 HTTPS（Let's Encrypt）。

### 步骤

1. 浏览器打开 https://caddyserver.com/download
2. 平台选 **Windows**，架构选 **amd64**（一般 VPS 都是）
3. 不需要额外插件（基础版即可）
4. 下载 `caddy_windows_amd64.exe`
5. 重命名为 `caddy.exe`，放到 `D:\yida-svc\caddy.exe`

---

## 4. 创建 Caddyfile

在 `D:\yida-svc\Caddyfile` 创建（把 `files.your-domain.com` 替换为你的域名）：

```caddy
files.your-domain.com {
    log {
        output file D:\yida-svc\logs\access.log
    }

    # ===== 上传接口 -> Python 后端 (8000) =====
    # 注意：必须用 handle /upload* 统一块，不要混用 handle_path + 裸 reverse_proxy
    handle /upload* {
        reverse_proxy 127.0.0.1:8000
    }

    # ===== 静态文件服务 =====
    handle /files/* {
        uri strip_prefix /files
        file_server {
            root D:\yida-attachments
            browse
        }
    }

    # ===== 默认 =====
    respond "OK" 200
}
```

### 防盗链说明

- 当前规则：允许空 Referer（移动端/直接访问），有 Referer 则只在钉钉/宜搭域名下放行。
- **Smoke test 时需观察 VPS 访问日志中的实际 Referer 值**，根据实际情况微调。可能的场景：
  - 宜搭 Web 端预览：Referer 含 `yida.alibaba-inc.com` 或 `www.yuque.com`
  - 钉钉客户端内预览：Referer 可能为空或含 `dingtalk`
  - 直接浏览器打开：Referer 为空
- 如果有误伤（预览 403），先临时注释防盗链段排查，再精准加白。

---

## 5. 创建 Python 上传服务

> **权威源码位置：`vps/attachment_server.py`**
> 该文件是版本受控的唯一权威版本，请直接复制到 VPS 的 `D:\yida-svc\attachment_server.py`。
> 配置通过环境变量或配置文件提供（见下方「配置三项参数」），不修改源码。

```powershell
# 在本机执行（示例：用 scp / 远程桌面复制均可）
scp migration/vps/attachment_server.py <vps>:D:/yida-svc/attachment_server.py
```

### 配置三项参数（ROOT / UPLOAD_TOKEN / DOMAIN）

优先级：**环境变量 > `config/credentials.json`（attachment_storage 段）> 启动时报错**。

- 环境变量方式（推荐，Windows 服务场景在 nssm 的 Environment 里配置）：
  - `YIDA_FILES_ROOT`：附件存储根目录（默认 `D:\yida-attachments`）
  - `YIDA_VPS_UPLOAD_TOKEN`：上传鉴权 Token（与迁移脚本端 `credentials.json` 的 `attachment_storage.upload_token` 保持一致）
  - `YIDA_FILES_DOMAIN`：公网域名前缀，如 `https://files.your-domain.com`（不含末尾斜杠）
- 配置文件方式：在服务程序目录或上级目录放置 `config/credentials.json`，写入 `attachment_storage` 段的 `upload_token` / `endpoint`。

### 服务端关键设计

| 特性 | 说明 |
|------|------|
| 幂等 | 同路径 + 同大小 → 跳过写盘，返回 `cached: true` |
| 大小上限 | `MAX_CONTENT_LENGTH` 在请求解析阶段强制 413，流式写入时再兜底一次 |
| 内存安全 | 幂等检查改用 `content_length`，写盘走 1MB 分块流式写入，全程不整文件读入内存 |
| 原子写入 | 先写 `.part` 临时文件，完成后 `os.replace`，中断不会留下半截文件被误判为已存在 |
| 路径安全 | `_safe_dest()` 校验解析后的绝对路径必须在 `ROOT` 之内，阻断 `..` 逃逸 |

### 省流量的关键在客户端

服务端的幂等只能省**写盘**，省不了**上行带宽**——请求体已经传完了才判重。
真正省流量的是客户端在上传前先对 `https://<your-domain>/files/<相对路径>` 发 **HEAD 预检**：
命中（200 且 Content-Length 相同）就完全不发文件体。
Caddy 的 `file_server` 原生支持 HEAD，无需额外配置。
该预检已实现于 `scripts/03b_attachment.py:vps_head_check()` 与 `unified_server.py:vps_file_exists()`。

### 生成 UPLOAD_TOKEN

在 VPS 的 PowerShell 中运行，生成随机 token：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

把输出复制到两端：VPS 环境变量（或配置文件）与迁移脚本 `credentials.json` 的 `attachment_storage.upload_token`。

---

## 6. 注册 Windows 服务（开机自启 + 崩溃重启）

使用 **nssm**（Non-Sucking Service Manager）把 Caddy 和 Python 服务注册为 Windows 服务。

### 6.1 下载 nssm

1. 打开 https://nssm.cc/download
2. 下载 `nssm 2.24 (pre-release)` 的 zip
3. 解压出 `nssm.exe`（64 位版在 `win64/nssm.exe`），放到 `D:\yida-svc\`（或 `C:\Windows\System32\`）

### 6.2 注册 Caddy 服务

在**管理员 PowerShell** 中：

```powershell
# 先手动测试 Caddy 能否正常启动
D:\yida-svc\caddy.exe run --config D:\yida-svc\Caddyfile
# Ctrl+C 退出后继续

# 注册为 Windows 服务
D:\yida-svc\nssm.exe install YidaCaddy D:\yida-svc\caddy.exe
# 在弹出的 GUI 中设置：
#   Arguments: run --config D:\yida-svc\Caddyfile
#  然后点 "Install service"

# 配置自动启动
D:\yida-svc\nssm.exe set YidaCaddy Start SERVICE_AUTO_START

# 启动
D:\yida-svc\nssm.exe start YidaCaddy
```

### 6.3 注册 Python 上传服务

```powershell
# 先手动测试 Python 服务能否正常启动
python D:\yida-svc\attachment_server.py
# Ctrl+C 退出后继续

# 用 waitress 作为生产级 WSGI 服务器（替代 Flask 开发服务器）
# waitress-serve 命令在 pip install waitress 后即可用

# 注册为服务（用 python.exe 直接调 waitress）
D:\yida-svc\nssm.exe install YidaUpload python
# GUI 中设置：
#   Path:       C:\Users\<你的用户>\AppData\Local\Programs\Python\Python311\python.exe
#                 ↑ 根据实际安装路径调整（用 where python 查看）
#   Startup dir: D:\yida-svc
#   Arguments:   -m waitress --listen=127.0.0.1:8000 attachment_server:app
#   然后点 "Install service"

D:\yida-svc\nssm.exe set YidaUpload Start SERVICE_AUTO_START
D:\yida-svc\nssm.exe start YidaUpload
```

### 6.4 服务管理常用命令

```powershell
nssm status YidaCaddy     # 查看状态
nssm restart YidaCaddy    # 重启
nssm stop YidaCaddy       # 停止
nssm remove YidaCaddy confirm  # 删除服务（先 stop）
```

---

## 7. 防火墙配置

在 VPS 的**管理员 PowerShell** 中，放行 80 和 443 端口：

```powershell
# 新建入站规则（HTTP）
New-NetFirewallRule -DisplayName "Caddy HTTP (80)" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow

# 新建入站规则（HTTPS）
New-NetFirewallRule -DisplayName "Caddy HTTPS (443)" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

如果 WinServer 2012 的 PowerShell 版本太老不支持 `New-NetFirewallRule`，用 `netsh` 代替：

```cmd
netsh advfirewall firewall add rule name="Caddy HTTP" dir=in action=allow protocol=TCP localport=80
netsh advfirewall firewall add rule name="Caddy HTTPS" dir=in action=allow protocol=TCP localport=443
```

---

## 8. 验证部署

部署完成后，在本机浏览器测试：

| 测试项 | URL | 预期结果 |
|--------|-----|----------|
| Health Check | `https://<your-domain>/upload/health` | `{"status":"ok","root":"D:\\yida-attachments",...}` |
| 文件访问 | `https://<your-domain>/files/test.txt` | 如果有文件则返回内容，否则 404 |
| HTTPS | 浏览器地址栏 | 显示锁图标（Let's Encrypt 已签发） |

### 上传测试（在本机 Python 脚本测试）

```python
import requests
resp = requests.post(
    "https://<your-domain>/upload",
    files={"file": ("hello.txt", b"hello world")},
    data={"path": "test/hello.txt"},
    headers={"X-Upload-Token": "你的token"}
)
print(resp.json())
# {"url": "https://<your-domain>/files/test/hello.txt"}
```

然后在浏览器访问返回的 URL，确认能下载 `hello world`。

---

## 9. 部署完毕后的交付信息

部署成功后，请把以下信息提供给迁移脚本端：

| 配置项 | 值 | 填入位置 |
|--------|-----|----------|
| 域名 | `https://<your-domain>` | `credentials.json` 的 `attachment_storage.endpoint` |
| 上传接口 | `https://<your-domain>/upload` | `credentials.json` 的 `attachment_storage.upload_url` |
| 上传 Token | 你生成的那串 | `credentials.json` 的 `attachment_storage.upload_token` |
| 本地缓存目录 | `data/attachment_cache` | `credentials.json` 的 `attachment_storage.local_cache` |
| 公网 URL 前缀 | `https://<your-domain>/files/` | 脚本中拼 URL 用（由 endpoint 拼出） |

---

## 10. 常见故障排查

| 现象 | 可能原因 | 排错步骤 |
|------|----------|----------|
| Caddy 启动闪退 | 80/443 端口被占用 | `netstat -ano \| findstr ":80"` 查占用进程 |
| HTTPS 报证书错误 | Let's Encrypt 签发失败 | ① 检查 80 端口从公网可达 ② DNS 确已解析 ③ Caddy 日志：`D:\yida-svc\caddy.exe run` 查看错误输出 |
| 上传返回 403 | Token 不匹配 | 检查 `X-Upload-Token` 请求头与 VPS 端配置的 `UPLOAD_TOKEN` 是否一致 |
| 上传返回 connection refused | Python 服务未启动 | `nssm status YidaUpload` 确认服务状态 |
| 文件访问 404 | 文件路径不对 | 检查文件是否在 `D:\yida-attachments\` 下，Caddy `uri strip_prefix /files` 后路径是否正确 |
| Python 服务启动报错 | waitress 未安装 | `pip list \| findstr waitress` 确认已安装 |
| 宜搭预览 403 | 防盗链误伤 | 临时注释 Caddyfile 中 `@hotlink` 相关行，确认能访问后再逐步收紧规则 |
