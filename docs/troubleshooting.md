# 排障

先跑只读检查，它会告诉你缺什么、下一步做什么，且不会打印密钥：

```bash
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py" --check --json
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
```

---

## 插件没有加载

**症状**：`hermes plugins list` 里没有 `wechatpadpro-platform`，或平台不在线。

1. `hermes plugins list` 确认状态是 enabled；
2. `hermes --version` 必须 `>= 0.21.5`（插件依赖 `env_enablement_fn`）；
3. 看网关日志：

   ```bash
   journalctl --user -u hermes-gateway -n 100 --no-pager
   # 或
   tail -n 100 "$HERMES_HOME/logs/gateway.log"
   ```

4. 检查 `plugin.yaml` 是否在 `$HERMES_HOME/plugins/wechatpadpro-platform/plugin.yaml`；
5. 如果日志说缺 `aiohttp` / `websockets` / `pymysql`，把它们装进 **Hermes 自己的解释器**：

   ```bash
   <hermes-python> -m pip install 'aiohttp>=3.9,<4' 'websockets>=12,<16' 'pymysql>=1.1,<2'
   ```

---

## 平台已注册但不收消息

按顺序排查：

1. **账号是否连接**：`--check --json` 的 `ready`，以及日志里的连接行；
2. **凭据是否被别处占用**：一个客户端密钥只能给一个客户端用。同一凭据在另一台机器/
   另一个 profile 上跑着，会把当前连接踢下线；
3. **webhook 是否可达**：vendor 侧配置的回调地址要能访问到
   `WECHATPRO_WEBHOOK_HOST:webhookPort`；反向代理要转发对应路径；
4. **签名是否一致**：`WECHATPRO_WEBHOOK_SECRET` 必须与 vendor 侧配置一致，否则 403；
5. **白名单**：私聊用户不在 `allowFrom` / `WECHATPRO_ALLOWED_USERS` 里会被静默丢弃；
6. **群策略**：`groupPolicy` 为 `disabled` / `closed`，或 `allowlist` 但群不在
   `groupAllowFrom` 里，都会不回复；拼错的值按 fail-closed 处理。

WebSocket 是兜底通道；webhook 配错时它仍可能收到消息，所以"webhook 不通"不一定表现为完全静默。

---

## 日志里一直有 webhook 403

- 检查 vendor 是否发送了 `TimestampMs` / `DeliveryID` / 签名头；
- `WECHATPRO_WEBHOOK_SECRET` 两边是否逐字一致（不要带引号或多余空格）；
- 服务器时间是否准确（允许偏移默认 ±300 秒，`WPP_WEBHOOK_MAX_SKEW_SEC` 可调）；
- 路径 token 是否包含在 URL 中，且代理没有改写路径。

临时诊断可设 `WPP_WEBHOOK_DEBUG=1`，它会把签名头写进日志（含凭据风险），**验完立刻关闭**。

---

## 工具数量不对 / 工具没注册

- `plugin.yaml` 的 `provides_tools` 必须与实际注册逐名一致；
- 注册失败不会影响其他工具：日志里会有 `注册失败` 和模块名；
- 在仓库里跑契约测试：

  ```bash
  python -m pytest plugin/tests/test_registration.py -q
  ```

---

## 语音没有转成文字

需要两样东西：

1. silk 解码器：`WPP_SILK_DECODER_PATH`（默认 `~/silk_decoder/silk/decoder`）；
2. STT key：`WECHATPRO_STT_API_KEY` 或 `SILICONFLOW_API_KEY`。

缺任意一样，第一条无法转写的语音会打一条 WARNING，之后降为 DEBUG。模型看到的是
`[语音]` 一行。厂商自带转写（`voice.transcript`）存在时不会走本地转写。

出站语音需要 `WPP_SILK_ENCODER_PATH`；缺失时降级为文件发送。

---

## 数据库相关

- 不配数据库时，消息收发正常，历史检索 / 心流落库 / 审计降级；
- `WECHATPRO_DB_HOST` 默认只允许本机；远程必须显式
  `WECHATPRO_DB_ALLOW_REMOTE=1`；
- 启动日志会打印 schema 漂移检测结果；插件**不会自动改列**；
- 测试库名必须以 `_test` 结尾，否则测试护栏直接拒绝。

---

## 升级后行为异常

1. 确认 `hermes plugins doctor` 通过；
2. 确认 Python 依赖版本满足 `plugin.yaml` 的上下界；
3. 回滚：把升级前的插件目录备份换回来并重启；
4. 报告问题时附上：Hermes 版本、`--check --json` 输出（它不含密钥）、相关日志片段。

---

## 收集诊断信息

```bash
hermes --version
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py" --check --json
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
journalctl --user -u hermes-gateway -n 200 --no-pager > /tmp/hermes-gateway.log
```

发送日志前先自行检查是否包含 wxid、群名、URL 等隐私信息。
