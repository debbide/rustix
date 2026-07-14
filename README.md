# Rustix 自动开机（二合一）

浏览器登录过 DDoS-Guard 盾，登录后**在页面内直接 fetch 发 POST** 开机——不走点击流，比模拟点击快且更稳（不依赖页面按钮 DOM）。无状态：每次干净登录，不缓存 cookie。

## 原理

1. Playwright 开真 Chromium 登录 → 真浏览器天然过 DDoS-Guard 盾（执行挑战 JS、真实 TLS 指纹）
2. 登录后用 `page.evaluate` 在浏览器上下文里 `fetch` 打翼龙 API
   - `GET /api/client` 自动拉出账号下所有服务器
   - 对每台先查状态，离线的发 `POST /api/client/servers/{id}/power` `{"signal":"start"}`
   - 请求由浏览器发出，自动带全套 cookie / UA / 盾放行 token，无需手动搬运
3. 轮询 `/resources` 确认真的 `running` 才算成功

## 为什么不导出 cookie 到 requests

导出后 UA/盾 token 要精确搬运，且 `__js_p_` 约 1 天就过期，麻烦又易失效。页面内 fetch 完全规避这些——请求就在浏览器里发，天然带齐。

## 本地运行

```bash
pip install -r requirements.txt
playwright install --with-deps chromium

# 方式一：环境变量
export ACCOUNTS="a@x.com:pass1,b@x.com:pass2"
python rustix_boot.py

# 方式二：账号文件（cp accounts.example.json accounts.json 后编辑）
python rustix_boot.py

# 调试：显示浏览器窗口 / 只跑某个账号
python rustix_boot.py --headed --only a@x.com
```

## GitHub Actions（无人值守）

仓库 **必须为 private**（日志/截图可能含敏感信息）。

Settings → Secrets and variables → Actions 添加：

| Secret | 说明 | 必填 |
|--------|------|------|
| `ACCOUNTS` | `邮箱:密码,邮箱:密码` | ✅ |
| `PROXY_STR` | 代理节点链接(vless/tuic/hy2/ss…)或完整 sing-box JSON | 可选 |
| `TG_BOT_TOKEN` | Telegram 机器人 token | 可选 |
| `TG_CHAT_ID` | Telegram 接收 chat id | 可选 |

### 代理（可选）

配了 `PROXY_STR` 时，workflow 会用 [convert_proxy.py](convert_proxy.py) 把它转成 sing-box 配置，后台起一个本地 `http://127.0.0.1:8080` 代理，浏览器全程走它出网（换出口 IP）。没配则直连。

- 走 HTTP（sing-box 的 `mixed` 入站，比 SOCKS 更稳），脚本读环境变量 `BROWSER_PROXY` 生效
- 启动后会 curl 探测代理直到就绪，起不来直接 fail 并把 `singbox.log` 传成 artifact
- 本地调试走代理：`export BROWSER_PROXY=http://127.0.0.1:8080` 后再跑

默认每 6 小时跑一次，也可在 Actions 页手动 `Run workflow`。改频率编辑 [.github/workflows/rustix.yml](.github/workflows/rustix.yml) 里的 cron。

## 账号配置

- 一般无需填 server_id，脚本登录后自动拉取账号下全部服务器
- 若只想开特定几台，在 accounts.json 里给该账号加 `"server_ids": ["uuid1","uuid2"]`

## 排查

- 失败时 Actions 会上传 `debug_*.png` 截图和 `run.log`（artifact，保留 3 天）
- 日志出现 `被盾拦截` → 盾没过，多为登录等待不足，调大 `SHIELD_WAIT`
- `登录失败: 账号或密码错误` → 检查 ACCOUNTS
- `发送成功但未确认到 running` → 指令到了但服务器没起，可能被挂起(suspended)需先续费
