# Changelog

本项目所有重要变更均记录在此文件中，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
