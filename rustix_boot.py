#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动开机 —— 二合一方案
- 浏览器登录 (Playwright) 过 DDoS-Guard 盾
- 登录后在页面内 fetch 直接发 POST 开机 (不走点击流)
- 自动拉取账号下所有服务器, 逐个开机
- 无状态: 每次干净登录, 不缓存 cookie
- 可选 Telegram 通知

站点语言: 俄语 / 英语
账号配置: 环境变量 ACCOUNTS="邮箱:密码,邮箱:密码" 或 accounts.json
"""

import json
import os
import sys
import time
import logging
import argparse
import urllib.request

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ---------------- 日志 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("run.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("rustix-boot")

BASE_URL = "https://my.rustix.me"
LOGIN_URL = f"{BASE_URL}/auth/login"
# 盾/SPA 渲染等待 (ms)
SHIELD_WAIT = 6000
STEP_WAIT = 3000
# 开机后查状态的重试
STATE_RETRY = 5
STATE_INTERVAL = 4  # 秒
# 浏览器代理 (由 sing-box 提供的本地 http 代理), 空则直连
# 由 workflow 里 convert_proxy.py 生成配置、sing-box 监听 127.0.0.1:8080
BROWSER_PROXY = os.environ.get("BROWSER_PROXY", "").strip()


# ---------------- 账号加载 ----------------
def parse_accounts_string(raw: str):
    """解析 '邮箱:密码,邮箱:密码' —— 逗号分账号, 首个冒号分邮箱/密码。"""
    accounts = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        email, password = item.split(":", 1)
        email, password = email.strip(), password.strip()
        if email and password:
            accounts.append({"email": email, "password": password})
    return accounts


def load_accounts():
    """优先级: 环境变量 ACCOUNTS > accounts.json。"""
    env = os.environ.get("ACCOUNTS", "").strip()
    if env:
        accounts = parse_accounts_string(env)
        if accounts:
            logger.info(f"从环境变量 ACCOUNTS 加载 {len(accounts)} 个账号")
            return accounts

    path = os.environ.get("ACCOUNTS_FILE", "accounts.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        logger.info(f"从 {path} 加载 {len(data)} 个账号")
        return data

    raise RuntimeError("未配置账号: 设置环境变量 ACCOUNTS 或创建 accounts.json")


# ---------------- Telegram 通知 (内置, 可选) ----------------
def tg_enabled():
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))


def tg_send(text: str):
    if not tg_enabled():
        return
    token = os.environ["TG_BOT_TOKEN"]
    chat_id = os.environ["TG_CHAT_ID"]
    try:
        data = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        logger.warning(f"Telegram 通知失败: {e}")


# ---------------- 登录 ----------------
def _first_visible(page: Page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("登录页加载超时, 继续")

    # 等 DDoS-Guard 挑战 JS 跑完 + SPA 渲染
    page.wait_for_timeout(SHIELD_WAIT)
    if "gorizontal-vertikal" in page.content():
        logger.info("检测到盾挑战页, 再等待放行...")
        page.wait_for_timeout(SHIELD_WAIT)

    email_loc = _first_visible(page, [
        'input[name="username"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="username"]',
    ])
    pwd_loc = _first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])
    if not email_loc or not pwd_loc:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录表单")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    btn = _first_visible(page, [
        'button[type="submit"]',
        'button:has-text("Войти")',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'input[type="submit"]',
    ])
    if not btn:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录按钮")
        return False

    logger.info("点击登录")
    try:
        btn.click()
    except Exception:
        btn.click(force=True)

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        logger.warning("登录后 networkidle 超时, 继续")
    page.wait_for_timeout(STEP_WAIT)

    if "/auth/login" in page.url:
        body = (page.inner_text("body") or "")[:500].lower()
        if any(k in body for k in ["incorrect", "invalid", "неверн", "ошибк"]):
            logger.error("登录失败: 账号或密码错误")
        else:
            logger.error("登录后仍停留在登录页")
        return False

    logger.info("登录成功")
    return True


# ---------------- 页面内 fetch ----------------
def _fetch_json(page: Page, path: str, method: str = "GET", body: dict = None):
    """在浏览器上下文发请求, 自动带 cookie/UA/盾token。返回 {status, body}。"""
    return page.evaluate(
        """async ({path, method, body}) => {
            const headers = { 'Accept': 'application/json' };
            const opts = { method, headers };
            if (body !== null) {
                headers['Content-Type'] = 'application/json';
                const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
                if (m) headers['X-XSRF-TOKEN'] = decodeURIComponent(m[1]);
                opts.body = JSON.stringify(body);
            }
            const r = await fetch(path, opts);
            const text = await r.text();
            return { status: r.status, body: text.slice(0, 300) };
        }""",
        {"path": path, "method": method, "body": body},
    )


def list_servers(page: Page):
    """GET /api/client, 返回所有服务器 identifier 列表。"""
    res = _fetch_json(page, "/api/client")
    if res["status"] != 200:
        logger.error(f"拉取服务器列表失败 HTTP {res['status']}: {res['body'][:120]}")
        return []
    if "gorizontal-vertikal" in res["body"]:
        logger.error("拉取列表被盾拦截, 登录态未过盾")
        return []
    try:
        # body 被截断到 300 字, 列表可能不完整 —— 重新完整取一次
        full = page.evaluate(
            """async () => {
                const r = await fetch('/api/client', { headers: { 'Accept': 'application/json' } });
                return await r.text();
            }"""
        )
        data = json.loads(full)
        ids = [d["attributes"]["identifier"] for d in data.get("data", [])]
        logger.info(f"账号下共 {len(ids)} 台服务器: {ids}")
        return ids
    except Exception as e:
        logger.error(f"解析服务器列表失败: {e}")
        return []


def power_start(page: Page, server_id: str) -> str:
    """发 start 信号。返回 started / already_running / suspended / failed。"""
    # 先看当前状态
    res = _fetch_json(page, f"/api/client/servers/{server_id}/resources")
    if res["status"] == 200:
        try:
            state = json.loads(res["body"]).get("attributes", {}).get("current_state")
        except Exception:
            state = None
        if state in ("running", "starting"):
            logger.info(f"[{server_id}] 已在运行 ({state}), 跳过")
            return "already_running"

    # 发 start
    res = _fetch_json(page, f"/api/client/servers/{server_id}/power",
                      method="POST", body={"signal": "start"})
    logger.info(f"[{server_id}] power=start -> HTTP {res['status']}")
    if "gorizontal-vertikal" in res["body"]:
        logger.error(f"[{server_id}] 被盾拦截")
        return "failed"
    if res["status"] not in (204, 200):
        logger.error(f"[{server_id}] 开机指令失败: {res['body'][:120]}")
        return "failed"

    # 轮询确认真起来了
    for i in range(STATE_RETRY):
        time.sleep(STATE_INTERVAL)
        r = _fetch_json(page, f"/api/client/servers/{server_id}/resources")
        if r["status"] == 200:
            try:
                state = json.loads(r["body"]).get("attributes", {}).get("current_state")
            except Exception:
                state = None
            logger.info(f"[{server_id}] 状态检查 {i+1}/{STATE_RETRY}: {state}")
            if state in ("running", "starting"):
                return "started"
    logger.warning(f"[{server_id}] 发送成功但未确认到 running")
    return "failed"


# ---------------- 单账号处理 ----------------
def process_account(account: dict, pw, headless: bool = True):
    email = (account.get("email") or "").strip()
    password = (account.get("password") or "").strip()
    result = {"email": email, "servers": [], "error": ""}

    if not email or not password:
        result["error"] = "账号或密码为空"
        logger.error(result["error"])
        return result

    logger.info(f"========== 处理账号: {email} ==========")
    browser = None
    try:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx_opts = {
            "viewport": {"width": 1366, "height": 800},
            "locale": "en-US",
        }
        if BROWSER_PROXY:
            ctx_opts["proxy"] = {"server": BROWSER_PROXY}
            logger.info(f"浏览器走代理: {BROWSER_PROXY}")
        ctx = browser.new_context(**ctx_opts)
        page = ctx.new_page()

        if not do_login(page, email, password):
            result["error"] = "登录失败"
            return result

        # 允许账号自带 server_ids, 否则自动拉取
        ids = account.get("server_ids") or list_servers(page)
        if not ids:
            result["error"] = "未获取到任何服务器"
            return result

        for sid in ids:
            status = power_start(page, sid)
            result["servers"].append({"id": sid, "status": status})

        return result

    except Exception as e:
        result["error"] = f"异常: {e}"
        logger.exception("处理账号异常")
        return result
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        logger.info(f"========== 账号 {email} 处理结束 ==========\n")


# ---------------- 汇总通知 ----------------
def build_summary(results):
    ok_states = ("started", "already_running")
    lines = ["<b>🚀 Rustix 开机汇总</b>"]
    total_srv = 0
    ok_srv = 0
    for r in results:
        if r["error"]:
            lines.append(f"❌ <code>{r['email']}</code>: {r['error']}")
            continue
        for s in r["servers"]:
            total_srv += 1
            mark = "✅" if s["status"] in ok_states else "❌"
            if s["status"] in ok_states:
                ok_srv += 1
            lines.append(f"{mark} <code>{r['email']}</code> {s['id']} — {s['status']}")
    lines.append(f"\n共 {ok_srv}/{total_srv} 台在线")
    return "\n".join(lines)


# ---------------- 主入口 ----------------
def main():
    parser = argparse.ArgumentParser(description="Rustix 服务器自动开机 (登录+发包 二合一)")
    parser.add_argument("--headed", action="store_true", help="非无头模式 (调试)")
    parser.add_argument("--only", help="只处理指定邮箱")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.only:
        accounts = [a for a in accounts if a.get("email") == args.only]
        if not accounts:
            logger.error(f"未找到账号: {args.only}")
            sys.exit(1)

    logger.info(f"共 {len(accounts)} 个账号待处理")
    if tg_enabled():
        logger.info("已启用 Telegram 通知")

    results = []
    with sync_playwright() as pw:
        for idx, acc in enumerate(accounts, 1):
            logger.info(f"--- 第 {idx}/{len(accounts)} 个账号 ---")
            results.append(process_account(acc, pw, headless=not args.headed))
            if idx < len(accounts):
                time.sleep(5)

    # 汇总
    ok_states = ("started", "already_running")
    logger.info("================ 结果汇总 ================")
    all_ok = True
    for r in results:
        if r["error"]:
            all_ok = False
            logger.info(f"[FAIL] {r['email']} | {r['error']}")
            continue
        for s in r["servers"]:
            flag = "OK" if s["status"] in ok_states else "FAIL"
            if s["status"] not in ok_states:
                all_ok = False
            logger.info(f"[{flag}] {r['email']} | {s['id']} | {s['status']}")

    if tg_enabled():
        tg_send(build_summary(results))

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
