# 贡献指南

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 提交前

```bash
.venv/bin/ruff check plugin
.venv/bin/python -m pytest plugin/tests -q
.venv/bin/python scripts/check_public_tree.py
```

或者一次跑完：

```bash
bash scripts/check.sh
```

三项都必须通过。CI 会跑同样的命令（Python 3.11 / 3.12）。

## 代码约定

- **注册期只用标准库**：`plugin/toolkit/`、`plugin/registration.py` 等注册路径
  模块级不得 import `aiohttp` / `websockets` / `pymysql`。Hermes 在隔离解释器里
  执行 `register()`，模块级第三方 import 会让工具**静默不注册**。需要时在函数体内
  延迟导入。
- **工具 handler 签名是 `handle(args, **kw)`**：不要用 `**params` 或位置参数默认值。
- **单点失败不中断整批注册**：平台、工具、hook 三段各自 try；工具面按子域隔离。
- **热路径零 DB**：入站判定只读内存缓存，DB 调用放后台任务。
- **角色边界**：`toolkit → platform → inbound → data → core`，禁止反向依赖。

## 测试约定

- 默认**禁网**；需要网络的用例加 `@pytest.mark.network`。
- 数据库用例只允许 `WECHATPRO_DB_NAME=*_test`；生产库名会直接失败。
- 不要提交真实 wxid、群号、vendor 域名、密钥或本机绝对路径。
  `scripts/check_public_tree.py` 会拦截常见形态。
- 新增工具时同步更新 `plugin/plugin.yaml` 的 `provides_tools` 和
  `plugin/tests/test_registration.py` 的期望数量。

## 提交信息

使用 Conventional Commits 风格，例如：

```
feat(toolkit): add wpp_xxx tool
fix(platform): dedupe replayed inbound messages
docs: clarify webhook setup
```

一个提交只做一件事；行为变更请附测试。

## 报告问题

- 功能缺陷 / 使用问题：开 Issue，附 Hermes 版本、`setup_wizard.py --check --json`
  （不含密钥）和相关日志。
- 安全漏洞：不要开公开 Issue，见 [SECURITY.md](SECURITY.md)。
