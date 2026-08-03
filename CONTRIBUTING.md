# 贡献指南（Contributing）

感谢你愿意参与贡献！请先阅读本指南与 [README](README.md)，并遵守 [行为准则](docs/CODE_OF_CONDUCT.md)（如存在）。

## 保密红线（必读）

本项目为第三方数据迁移工具，仓库内**禁止出现**以下内容，违反者 PR 会被直接关闭：

- 任何真实 API 密钥、Token、AppKey、AppSecret、systemToken、上传凭证；
- 任何真实业务域名、应用 ID、表单 ID、字段 ID、用户 ID；
- 任何真实业务数据样本（发票、客户、公司信息等）。

涉及凭证的功能一律通过环境变量或 `config/credentials.example.json` 占位符实现。

## 环境准备

```bash
# Python 3.10+
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## 开发与调试

- Web 控制台：`python unified_server.py`，默认 `http://127.0.0.1:8766`。
- 命令行管线：见 `scripts/`，每步脚本均支持 `--help`，格式为 `python <脚本>.py <表单名> [选项]`。
- 本地自检推荐使用 `--peek` 模式（只拉取、构造并打印，不写入目标平台）。

## 代码规范

- Python 代码遵循 [PEP 8](https://peps.python.org/pep-0008/)，行宽约 100。
- 提交前请通过本地检查：

```bash
ruff check --select E9,F63,F7,F82 scripts/ vps/ unified_server.py
python -m compileall -q scripts/ vps/ web/ unified_server.py
```

- 新增脚本时在 `scripts/` 下登记，并在 `unified_server.py` 的 `STEP_DEFS` / `STEP_ORDER` 中补充管线定义。

## 提交规范

- 提交信息使用简洁的祈使句，可带前缀：`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`。
- 示例：`feat: add rate limit for attachment migration`、`docs: clarify env var override behavior`。

## PR 流程

1. Fork 本仓库并创建特性分支（`feat/xxx` 或 `fix/xxx`）。
2. 编写代码与必要的文档，补充/更新 `CHANGELOG.md`。
3. 通过全部 CI 检查（ruff + compileall + gitleaks）。
4. 发起 PR，描述变更动机、实现方式与验证结果。
5. 维护者 review 后合并。
