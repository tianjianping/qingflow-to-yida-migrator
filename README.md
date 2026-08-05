# qingflow-to-yida-migrator

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](requirements.txt)
[![Release](https://img.shields.io/badge/Release-v0.2.0-2f6fed.svg)](CHANGELOG.md)

> **Disclaimer / 免责声明**
>
> 本项目为**第三方开源工具**，非轻流（QingFlow）或宜搭（Yida）官方出品，也未获得任何一方官方授权、背书或合作。项目涉及的「轻流」「宜搭」「钉钉」等商标归各自权利人所有。
>
> 1. 本工具仅供用户在其合法拥有权限的数据范围内进行合规的数据备份与迁移使用。
> 2. 使用本工具产生的任何数据丢失、服务中断或与原平台服务协议相关的纠纷，开发者概不承担任何直接或间接责任。
> 3. 请在迁移前做好原平台数据的完整备份。

在**轻流（QingFlow）与宜搭（Yida）之间**进行增量、可审计、可回滚的数据迁移工具：通过官方开放 API 拉取轻流数据，三方对账后按字段映射写入宜搭，并提供本地 Web 控制台与附件迁移能力。全程**本地直连、凭证本地化、无任何中转服务器**。

## 功能特性

- **四阶段迁移管线**：拉取（轻流数据 / 宜搭组件 / 宜搭存量）→ 三方对账 → 差异格式化 → 写入宜搭。
- **增量迁移**：按台账记录已迁移状态，跳过已迁移记录，实时显示已迁移 / 待迁移数量。
- **表单类型自动识别**：自动探测宜搭表单类型（普通表单 / 流程表单），普通表单走 `batchSave` + `insertOrUpdate`，流程表单走 `processes/instances/start` + `PUT processes/instances`，无需手工区分。
- **字段自动映射**：按「宜搭字段标题 == 轻流字段名」自动生成映射表，支持标题别名、系统字段匹配、重名字段保护（优先沿用旧映射）、子表单子组件映射；宜搭结构变化时自动重映射并继承手工调整。
- **迁移预检**：迁移前输出需要手工调整宜搭表单的工作清单（字段缺失 / 类型不符 / 重名提示 / 未匹配字段等），并可在本地保存 MD 一键复制。
- **附件迁移**：轻流附件下载 → 本地缓存 → 上传自有 VPS 静态托管 → 公网长期 URL 直写宜搭（绕开宜搭外链不转存限制）。
- **本地 Web 控制台**：表单中心式操作，数据迁移与附件迁移一体化，任务状态与日志实时可见。
- **安全设计**：写入限流与任务并发控制；凭证通过环境变量或本地配置文件提供，不硬编码、不中转。

## 快速开始

### 环境要求

- Python 3.10+
- Windows / Linux / macOS 均可运行（Web 控制台依赖 Flask）

### 安装

```bash
git clone https://github.com/tianjianping/qingflow-to-yida-migrator.git
cd qingflow-to-yida-migrator
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

### 配置凭证

凭证支持**两种方式**：编辑 `config/credentials.json`（部署时用），或在**控制台网页「设置 → 凭证」页**配置（部署后无需改文件；留空的输入框保存时保持原值，「清除」= 显式置空）。

复制示例配置并按需填写（或直接使用环境变量覆盖，推荐 CI/服务器场景）：

```bash
cp config/credentials.example.json config/credentials.json
```

需要准备的凭证：

| 配置项 | 说明 | 环境变量覆盖 |
|---|---|---|
| `qingflow.accessToken` | 轻流开放平台 accessToken | `QINGFLOW_ACCESS_TOKEN` |
| `qingflow.baseUrl` | 轻流 OpenAPI 基址（默认 `https://api.ding.qingflow.com`） | - |
| `qingflow.userId` | 轻流成员 ID（拉取应用包列表必填，轻流后台「个人中心」获取） | - |
| `dingtalk.appKey` / `appSecret` | 钉钉应用（宜搭载体）密钥 | `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` |
| `yida.systemToken` | 宜搭 systemToken | `YIDA_SYSTEM_TOKEN` |
| `yida.appType` | 宜搭应用类型（如未配置应用列表可在此提供默认值） | - |
| `yida.userId` | 宜搭操作人 userId | - |
| `attachment_storage.upload_token` | VPS 附件上传 Token（可选） | `YIDA_VPS_UPLOAD_TOKEN` |

> 凭证仅保存在本机，程序直接调用官方开放 API，数据不经任何第三方中转。
>
> **安全说明**：服务默认只监听 `127.0.0.1`。当以非本机地址部署时（`--host 0.0.0.0` 等），必须设置 `MIGRATION_API_TOKEN` 环境变量，且自动进入**脱敏模式**——凭证接口只写不回显（密钥以掩码 `a***xxxx` 形式展示，`?view=full` 被拒绝），网页配置凭证后无法再读取明文。

### 前置准备：宜搭表单

宜搭**不提供创建表单定义的 API**（钉钉 OA 审批模板接口与宜搭表单是两个独立体系），目标表单必须**在宜搭设计器中手动创建**，且**字段标题（label）须与轻流字段名（queTitle）完全一致**（含空格与全半角差异），自动映射（`02b`）才能正确对齐。详见 [docs/宜搭表单准备说明.md](docs/宜搭表单准备说明.md)。

### 在控制台关联表单

启动控制台后，在「+ 新建表单」弹窗中可：

- **从轻流拉取应用**：下拉选择目标应用，自动回填 `AppKey`；
- **从宜搭拉取表单**：下拉选择已手动创建的表单，自动回填 `formUuid`（名称留空时默认取表单标题）。

保存后系统自动生成表单配置（`AppKey` + `formUuid` 关联关系），即可开始迁移。表单详情会展示**表单类型徽标**（普通表单 / 流程表单）。

### 启动 Web 控制台

```bash
python unified_server.py            # 默认 http://127.0.0.1:8766
python unified_server.py --port 9000
```

Windows 下也可直接双击 `start_console.bat`。

## 使用流程

### 数据迁移（四阶段管线）

1. **拉取**：`00` 生成表单配置 → `01` 拉取轻流数据 → `02` 拉取宜搭组件 → `02b` 自动映射 → `02c` 拉取宜搭存量。
2. **对账**：`02d` 三方对账，产出差异清单（待新建 / 待更新 / 跳过 / 源已删除）。
3. **格式化**：`03` 将差异集按映射表转为宜搭裸值格式。
4. **写入**：`04` 按差异清单写入宜搭。写入方式由表单类型自动决定：普通表单 `batchSave` 新建 / `insertOrUpdate` 更新；流程表单 `processes/instances/start` 发起 / `PUT processes/instances` 更新（更新定位复用 02c 生成的 `didToInst` 索引）。

各脚本支持命令行直跑（推荐先 `--peek` 自检；`--form-type normal|process|auto` 可强制指定表单类型，默认 `auto` 自动探测）：

```bash
python scripts/03_transform.py 示例表单 --peek
python scripts/04_batch_create.py 示例表单 --form-type auto --peek
```

### 附件迁移（可选）

1. VPS 端部署上传服务（Caddy 静态托管 + 防盗链 + 反代上传），详见 [docs/vps-deploy-guide.md](docs/vps-deploy-guide.md)。
2. 配置 `config/credentials.json` 的 `attachment_storage` 段。
3. 控制台附件迁移面板或命令行执行：

```bash
python scripts/03b_attachment.py 示例表单 --peek      # 安全自检
python scripts/03b_attachment.py 示例表单 --limit 10  # 小批量
```

## 项目结构

```text
.
├── unified_server.py        # Web 控制台主服务（Flask）
├── start_console.bat        # Windows 一键启动
├── scripts/                 # 命令行迁移管线（00~04）
│   ├── common.py            # 凭证加载 / HTTP 封装 / 环境变量覆盖
│   └── form_type.py         # 宜搭表单类型自动探测（config > 缓存 > 接口）
├── web/                     # 控制台前端（原生 HTML/CSS/JS）
├── vps/                     # VPS 附件上传服务（attachment_server.py）
├── config/                  # 配置（真实凭证不入库，示例见 *.example.*）
├── mappings/                # 字段映射表（真实映射不入库，示例见 *.example.*）
├── docs/                    # 文档：宜搭准备 / 转换规则 / VPS 部署 / 附件迁移
└── .github/workflows/ci.yml # 持续集成（ruff + 编译 + 冒烟 + 密钥扫描）
```

## 安全与合规

- **官方 API 优先**：仅使用轻流 / 宜搭 / 钉钉官方开放平台公开 API，频率受限流保护。
- **凭证本地化**：密钥只存在于你的 `config/credentials.json` 或环境变量，不硬编码、不提交仓库。
- **数据直连**：迁移过程在「你的本机 / 私有服务器」与官方 API 之间直接发生，开发方不接触、不留存任何数据。
- **附件安全**：上传接口强制 Token 校验、扩展名白名单、路径穿越防护；文件公网访问带防盗链。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/宜搭表单准备说明.md](docs/宜搭表单准备说明.md) | 宜搭表单手动创建要求与字段标题匹配契约 |
| [docs/字段转换总览.md](docs/字段转换总览.md) | 已支持的字段转换能力总览（含地址/关联/附件专项） |
| [docs/地址字段转换规则.md](docs/地址字段转换规则.md) | 轻流地址字段 → 宜搭 AddressField 转换规则 |
| [docs/关联组件转换规则.md](docs/关联组件转换规则.md) | 轻流关联字段 → 宜搭 AssociationFormField 跨系统键解析规则 |
| [docs/vps-deploy-guide.md](docs/vps-deploy-guide.md) | VPS 附件托管部署指南 |
| [docs/attachment-migration-plan.md](docs/attachment-migration-plan.md) | 附件迁移架构与实施方案 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南（含保密红线） |
| [SECURITY.md](SECURITY.md) | 安全政策与漏洞报告 |

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源，附带 [NOTICE](NOTICE)。

**再次声明：本项目与轻流（QingFlow）、宜搭（Yida）、钉钉（DingTalk）均无任何官方关联。**
