# Changelog

本项目所有重要变更均记录在此文件中，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- 凭证可在部署后通过网页「设置 → 凭证」页配置（`GET/POST /api/credentials`），支持只写不回显、留空保持原值、显式置空（`clear`）、占位符拒绝与环境变量优先；非本机部署自动进入脱敏模式（掩码展示，`?view=full` 拒绝）。
- 新增「测试连接」接口（`POST /api/credentials/test`），一键校验钉钉 / 宜搭 / 轻流三端连通性。
- 轻流应用包列表拉取（`GET /api/qingflow/apps`，基于 `GET /tags?userId=`，新增 `qingflow.userId` 凭证字段）。
- 宜搭指定应用下表单列表拉取（`GET /api/yida/forms`），表单弹窗支持从轻流/宜搭下拉选择并自动回填 `AppKey` / `formUuid`，实现用户手动关联。
- 文档契约：明确宜搭表单须在宜搭设计器中手动创建（宜搭无创建表单定义 API），字段标题（label）须与轻流字段名一致；新增 [docs/宜搭表单准备说明.md](docs/宜搭表单准备说明.md)。

### Planned
- 支持更多轻流字段类型的自动映射。
- 迁移任务的断点续跑与失败重试可视化。
- 增量同步模式（定时拉取新增/变更记录）。

## [0.1.0] - 2026-08-03

### Added
- 首次开源发布。
- 轻流 -> 宜搭四阶段数据迁移管线：拉取、对账、格式化、写入。
- 附件迁移：轻流附件下载缓存 -> 自有 VPS 静态托管 -> 宜搭公网 URL 直写。
- 本地 Web 控制台（Flask），支持表单中心式操作与任务状态查看。
- 增量迁移：按台账跳过已迁移记录，支持已迁移/待迁移统计。
- 写入限流与任务级并发控制，规避对官方 API 的请求冲击。
- 环境变量覆盖机制（`QINGFLOW_ACCESS_TOKEN` / `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` / `YIDA_SYSTEM_TOKEN` / `YIDA_VPS_UPLOAD_TOKEN`），凭证默认本地化，不硬编码、不中转。
