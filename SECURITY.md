# 安全策略

## 报告漏洞

请不要开公开 Issue。通过以下方式私下联系维护者：

- GitHub Security Advisory（仓库 **Security → Report a vulnerability**）
- 或仓库主页公开的维护者邮箱

请附：影响版本、复现步骤、影响范围。我们会在确认后给出修复计划，并在修复发布后
再公开细节。

## 不要提交的内容

- 真实 `authcode` / `tokenKey` / 数据库口令 / LLM key；
- 生产 bot 的 wxid、真实群号、客户微信号；
- vendor 域名、内网 IP、本机绝对路径；
- 填好的 `accounts/*.json` 或 `.env`。

仓库只允许 `*.example.json` 和 `.env.example`，`scripts/check_public_tree.py`
会扫描常见泄漏形态。

## 密钥泄漏后的处理

1. 立即在 vendor / 数据库 / LLM 服务侧**轮换**该凭据；
2. 检查网关日志是否有异常出站；
3. 如果已提交到 Git，按平台流程清理历史并强制推送；
4. 在 Issue（不含密钥）或私下告知维护者。

## 安全护栏（部署侧）

- `WECHATPRO_PROTECTED_WXIDS` / `WECHATPRO_PROTECTED_FINGERPRINTS`：防止测试实例
  误连生产账号；
- `WECHATPRO_SEND_ALLOWLIST`：限制出站目标；
- `WECHATPRO_DB_ALLOW_REMOTE`：默认只允许本机数据库；
- webhook 双因素：路径 token + HMAC secret。
