import os
import platform
import time
import random
import re
from typing import List, Dict, Optional, Tuple

import requests
from seleniumbase import SB
from pyvirtualdisplay import Display

"""
批量登录 https://betadash.lunes.host/login?next=/
登录成功后：
  0) 从登录成功后的"Manage Servers"界面里，找到 <a href="/servers/63585" class="server-card">
     - 提取 href 里的数字作为 server_id（例如 63585）
     - 点击该 a（或 open 对应 URL），进入 server 控制台页（等 "Now managing" 出现）
  1) server 页停留 4-6 秒
  2) 返回 https://betadash.lunes.host/ 页面，停留 3-5 秒
  3) 点击退出按钮 /logout 退出（不做 JS 强制点击、不做重试）

环境变量：ACCOUNTS_BATCH（多行，每行一套，英文逗号分隔）
  1) 不发 TG：email,password
  2) 发 TG：email,password,tg_bot_token,tg_chat_id

示例：
export ACCOUNTS_BATCH='a1@example.com,pass1
a2@example.com,pass2,123456:AAxxxxxx,123456789
'
"""

LOGIN_URL = "https://betadash.lunes.host/login?next=/"
HOME_URL = "https://betadash.lunes.host/"
LOGOUT_URL = "https://betadash.lunes.host/logout"
SERVER_URL_TPL = "https://betadash.lunes.host/servers/{server_id}"

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ✅ 登录表单选择器
EMAIL_SEL = "#email"
PASS_SEL = "#password"
SUBMIT_SEL = 'button.submit-btn[type="submit"]'

# ✅ 登录成功后出现的退出按钮
LOGOUT_SEL = 'a[href="/logout"].action-btn.ghost'

# ✅ server 页面加载成功标志：出现 "Now managing"
NOW_MANAGING_XPATH = 'xpath=//p[contains(normalize-space(.), "Now managing")]'

# ✅ 服务器卡片：<a href="/servers/63585" class="server-card">
SERVER_CARD_LINK_SEL = 'a.server-card[href^="/servers/"]'


def mask_email_keep_domain(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    name, domain = e.split("@", 1)
    if len(name) <= 1:
        name_mask = name or "*"
    elif len(name) == 2:
        name_mask = name[0] + name[1]
    else:
        name_mask = name[0] + ("*" * (len(name) - 2)) + name[-1]
    return f"{name_mask}@{domain}"


def setup_xvfb():
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        os.environ["DISPLAY"] = display.new_display_var
        print("🖥️ Xvfb 已启动")
        return display
    return None


def screenshot(sb, name: str):
    path = f"{SCREENSHOT_DIR}/{name}"
    sb.save_screenshot(path)
    print(f"📸 {path}")


def tg_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None):
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"⚠️ TG 发送失败：{e}")


def build_accounts_from_env() -> List[Dict[str, str]]:
    batch = (os.getenv("ACCOUNTS_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量：请设置 ACCOUNTS_BATCH（即使只有一个账号也用它）")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]

        if len(parts) not in (2, 4):
            raise RuntimeError(
                f"❌ ACCOUNTS_BATCH 第 {idx} 行格式不对（必须是 email,password 或 "
                f"email,password,tg_bot_token,tg_chat_id）：{raw!r}"
            )

        email, password = parts[0], parts[1]
        tg_token = parts[2] if len(parts) == 4 else ""
        tg_chat = parts[3] if len(parts) == 4 else ""

        if not email or not password:
            raise RuntimeError(f"❌ ACCOUNTS_BATCH 第 {idx} 行存在空字段：{raw!r}")

        accounts.append(
            {
                "email": email,
                "password": password,
                "tg_token": tg_token,
                "tg_chat": tg_chat,
            }
        )

    if not accounts:
        raise RuntimeError("❌ ACCOUNTS_BATCH 里没有有效账号行（空行/注释行不算）")

    return accounts


def _has_cf_clearance(sb: SB) -> bool:
    try:
        cookies = sb.get_cookies()
        cf_clearance = next((c["value"] for c in cookies if c.get("name") == "cf_clearance"), None)
        print("🧩 cf_clearance:", "OK" if cf_clearance else "NONE")
        return bool(cf_clearance)
    except Exception:
        return False


def _try_click_captcha(sb: SB, stage: str):
    try:
        sb.uc_gui_click_captcha()
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ captcha 点击异常（{stage}）：{e}")


def _is_logged_in(sb: SB) -> Tuple[bool, Optional[str]]:
    welcome_text = None
    try:
        if sb.is_element_visible("h1.hero-title"):
            welcome_text = (sb.get_text("h1.hero-title") or "").strip()
            if "welcome back" in welcome_text.lower():
                return True, welcome_text
    except Exception:
        pass

    try:
        if sb.is_element_visible(LOGOUT_SEL):
            return True, welcome_text
    except Exception:
        pass

    return False, welcome_text


def _extract_server_id_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    m = re.search(r"/servers/(\d+)", href)
    return m.group(1) if m else None


def _find_server_id_and_go_server_page(sb: SB) -> Tuple[Optional[str], bool]:
    try:
        sb.wait_for_element_visible(SERVER_CARD_LINK_SEL, timeout=25)
    except Exception:
        screenshot(sb, f"server_card_not_found_{int(time.time())}.png")
        return None, False

    try:
        href = sb.get_attribute(SERVER_CARD_LINK_SEL, "href") or ""
    except Exception:
        href = ""

    server_id = _extract_server_id_from_href(href)

    if not server_id:
        screenshot(sb, f"server_id_extract_failed_{int(time.time())}.png")
        return None, False

    try:
        print(f"🧭 提取到 server_id={server_id}，点击 server-card 跳转...")
        sb.scroll_to(SERVER_CARD_LINK_SEL)
        time.sleep(0.3)
        sb.click(SERVER_CARD_LINK_SEL)
        sb.wait_for_element_visible(NOW_MANAGING_XPATH, timeout=30)
        return server_id, True
    except Exception:
        try:
            server_url = SERVER_URL_TPL.format(server_id=server_id)
            print(f"⚠️ 点击跳转失败，改为直接打开：{server_url}")
            sb.open(server_url)
            sb.wait_for_element_visible(NOW_MANAGING_XPATH, timeout=30)
            return server_id, True
        except Exception:
            screenshot(sb, f"goto_server_failed_{int(time.time())}.png")
            return server_id, False


# ✅ 修复：更健壮的退出逻辑，增加直接访问 /logout URL 的兜底
def _do_logout(sb: SB) -> bool:
    """
    尝试退出登录，返回是否成功。
    策略：
      1) 先尝试点击 LOGOUT_SEL 按钮
      2) 失败则直接 open LOGOUT_URL（/logout）
    成功标志：URL 包含 /login 或 登录表单可见
    """
    logged_out = False

    # 策略1：点击按钮
    try:
        sb.wait_for_element_visible(LOGOUT_SEL, timeout=10)
        sb.scroll_to(LOGOUT_SEL)
        time.sleep(0.3)
        sb.click(LOGOUT_SEL)
        sb.wait_for_element_visible("body", timeout=20)
        time.sleep(1.5)
        url_now = (sb.get_current_url() or "").lower()
        if "/login" in url_now:
            logged_out = True
        elif sb.is_element_visible(EMAIL_SEL) and sb.is_element_visible(PASS_SEL):
            logged_out = True
    except Exception as e:
        print(f"⚠️ 点击退出按钮失败：{e}")

    # 策略2：直接访问 /logout URL 兜底
    if not logged_out:
        try:
            print(f"🔄 兜底：直接访问 {LOGOUT_URL}")
            sb.open(LOGOUT_URL)
            sb.wait_for_element_visible("body", timeout=20)
            time.sleep(1.5)
            url_now = (sb.get_current_url() or "").lower()
            if "/login" in url_now:
                logged_out = True
            elif sb.is_element_visible(EMAIL_SEL) and sb.is_element_visible(PASS_SEL):
                logged_out = True
        except Exception as e:
            print(f"⚠️ 直接访问 /logout 也失败：{e}")

    if not logged_out:
        screenshot(sb, f"logout_verify_failed_{int(time.time())}.png")

    return logged_out


def _post_login_visit_then_logout(sb: SB) -> Tuple[Optional[str], bool]:
    """
    登录成功后：
      0) 从 Manage Servers 卡片中提取 server_id，并进入 server 页（等待 Now managing）
      1) server 页停留 4-6 秒
      2) 返回首页 / 停留 3-5 秒
      3) 退出登录（点击按钮 + 直接访问 /logout 兜底）
    返回 (server_id, logout_ok)
    """
    # 0) 提取 server_id 并进 server 页
    server_id, entered_ok = _find_server_id_and_go_server_page(sb)
    if not entered_ok:
        return server_id, False

    # 1) server 页停留
    stay1 = random.randint(4, 6)
    print(f"⏳ 服务器页停留 {stay1} 秒...")
    time.sleep(stay1)

    # 2) 回首页
    try:
        print(f"↩️ 返回首页：{HOME_URL}")
        sb.open(HOME_URL)
        # 等待首页核心元素（logout 按钮或 body）出现
        sb.wait_for_element_visible("body", timeout=30)
        # 额外等 logout 按钮确认已登录态渲染
        try:
            sb.wait_for_element_visible(LOGOUT_SEL, timeout=15)
        except Exception:
            print("⚠️ 首页未检测到退出按钮，仍继续尝试退出...")
    except Exception:
        screenshot(sb, f"back_home_failed_{int(time.time())}.png")
        # 首页打开失败，直接尝试 /logout 兜底退出
        print("⚠️ 回首页失败，直接尝试 /logout...")
        logout_ok = _do_logout(sb)
        return server_id, logout_ok

    stay2 = random.randint(3, 5)
    print(f"⏳ 首页停留 {stay2} 秒...")
    time.sleep(stay2)

    # 3) 退出
    logout_ok = _do_logout(sb)
    return server_id, logout_ok


def login_then_flow_one_account(email: str, password: str) -> Tuple[str, Optional[str], bool, str, Optional[str], bool]:
    """
    返回：
      (status, welcome_text, has_cf_clearance, current_url, server_id, logout_ok)

    status:
      - "OK"   登录成功（无论 logout 是否成功）
      - "FAIL" 登录失败
    """
    with SB(uc=True, locale="en", test=True) as sb:
        print("🚀 浏览器启动（UC Mode）")

        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5.0)
        time.sleep(2)

        # ===== 业务：填写并提交登录表单 =====
        try:
            sb.wait_for_element_visible(EMAIL_SEL, timeout=25)
            sb.wait_for_element_visible(PASS_SEL, timeout=25)
            sb.wait_for_element_visible(SUBMIT_SEL, timeout=25)
        except Exception:
            url_now = sb.get_current_url() or ""
            return "FAIL", None, _has_cf_clearance(sb), url_now, None, False

        sb.clear(EMAIL_SEL)
        sb.type(EMAIL_SEL, email)
        sb.clear(PASS_SEL)
        sb.type(PASS_SEL, password)

        _try_click_captcha(sb, "提交前")

        sb.click(SUBMIT_SEL)
        sb.wait_for_element_visible("body", timeout=30)
        time.sleep(2)

        _try_click_captcha(sb, "提交后")

        has_cf = _has_cf_clearance(sb)
        current_url = (sb.get_current_url() or "").strip()

        # ===== 业务：判定登录成功 =====
        welcome_text = None
        logged_in = False
        for _ in range(10):
            logged_in, welcome_text = _is_logged_in(sb)
            if logged_in:
                break
            time.sleep(1)

        if not logged_in:
            return "FAIL", welcome_text, has_cf, current_url, None, False

        # ===== 业务：登录后提取 server_id -> 进 server 页 -> 回首页 -> 退出 =====
        server_id, logout_ok = _post_login_visit_then_logout(sb)

        try:
            current_url = (sb.get_current_url() or "").strip()
        except Exception:
            pass

        return "OK", welcome_text, has_cf, current_url, server_id, logout_ok


def main():
    accounts = build_accounts_from_env()
    display = setup_xvfb()

    ok = 0
    fail = 0
    logout_ok_count = 0
    tg_dests = set()

    # ✅ 修复：收集每个账号的结果消息，最后统一发送一条 TG 通知
    account_lines: List[str] = []

    try:
        for i, acc in enumerate(accounts, start=1):
            email = acc["email"]
            password = acc["password"]
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat = (acc.get("tg_chat") or "").strip()
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))

            safe_email = mask_email_keep_domain(email)

            print("\n" + "=" * 70)
            print(f"👤 [{i}/{len(accounts)}] 账号：{safe_email}")
            print("=" * 70)

            try:
                status, welcome_text, has_cf, url_now, server_id, logout_ok = login_then_flow_one_account(
                    email, password
                )

                if status == "OK":
                    ok += 1
                    if logout_ok:
                        logout_ok_count += 1
                    line = (
                        f"✅ {safe_email} | server:{server_id or '?'} | "
                        f"退出:{'✅' if logout_ok else '❌'} | "
                        f"cf:{'OK' if has_cf else 'NONE'}"
                    )
                else:
                    fail += 1
                    line = (
                        f"❌ {safe_email} | 登录失败 | "
                        f"cf:{'OK' if has_cf else 'NONE'} | 页:{url_now}"
                    )

                print(line)
                account_lines.append(line)

            except Exception as e:
                fail += 1
                line = f"❌ {safe_email} | 脚本异常：{e}"
                print(line)
                account_lines.append(line)

            # 账号之间冷却
            time.sleep(5)
            if i < len(accounts):
                time.sleep(5)

        summary_header = (
            f"📌 Lunes BetaDash 批量保活完成\n"
            f"登录成功 {ok} / 失败 {fail} | 退出成功 {logout_ok_count}/{ok}\n"
            f"{'─' * 30}"
        )
        summary_body = "\n".join(account_lines)
        full_summary = f"{summary_header}\n{summary_body}"

        print("\n" + full_summary)
        for token, chat in sorted(tg_dests):
            tg_send(full_summary, token, chat)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
