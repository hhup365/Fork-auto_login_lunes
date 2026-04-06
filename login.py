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
  0) 从"Manage Servers"界面找到 server-card，提取 server_id
  1) 进入 server 页，等待 "Now managing" 出现，停留 4-6 秒
  2) 返回首页，停留 3-5 秒
  3) 直接访问 /logout 退出

环境变量：ACCOUNTS_BATCH（多行，每行一套，英文逗号分隔）
  不发 TG：email,password
  发  TG：email,password,tg_bot_token,tg_chat_id
"""

LOGIN_URL  = "https://betadash.lunes.host/login?next=/"
HOME_URL   = "https://betadash.lunes.host/"
LOGOUT_URL = "https://betadash.lunes.host/logout"
SERVER_URL_TPL = "https://betadash.lunes.host/servers/{server_id}"

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

EMAIL_SEL  = "#email"
PASS_SEL   = "#password"
SUBMIT_SEL = 'button.submit-btn[type="submit"]'
LOGOUT_SEL = 'a[href="/logout"].action-btn.ghost'
NOW_MANAGING_XPATH  = 'xpath=//p[contains(normalize-space(.), "Now managing")]'
SERVER_CARD_LINK_SEL = 'a.server-card[href^="/servers/"]'


def mask_email_keep_domain(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    name, domain = e.split("@", 1)
    if len(name) <= 2:
        name_mask = name
    else:
        name_mask = name[0] + "*" * (len(name) - 2) + name[-1]
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
    token   = (token   or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"⚠️ TG 发送失败：{e}")


def build_accounts_from_env() -> List[Dict[str, str]]:
    batch = (os.getenv("ACCOUNTS_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量 ACCOUNTS_BATCH")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) not in (2, 4):
            raise RuntimeError(f"❌ 第 {idx} 行格式错误：{raw!r}")
        email, password = parts[0], parts[1]
        if not email or not password:
            raise RuntimeError(f"❌ 第 {idx} 行存在空字段：{raw!r}")
        accounts.append({
            "email":    email,
            "password": password,
            "tg_token": parts[2] if len(parts) == 4 else "",
            "tg_chat":  parts[3] if len(parts) == 4 else "",
        })

    if not accounts:
        raise RuntimeError("❌ ACCOUNTS_BATCH 无有效账号")
    return accounts


def _has_cf_clearance(sb) -> bool:
    try:
        cookies = sb.get_cookies()
        ok = any(c.get("name") == "cf_clearance" for c in cookies)
        print("🧩 cf_clearance:", "OK" if ok else "NONE")
        return ok
    except Exception:
        return False


def _try_click_captcha(sb, stage: str):
    try:
        sb.uc_gui_click_captcha()
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ captcha（{stage}）：{e}")


def _is_logged_in(sb) -> Tuple[bool, Optional[str]]:
    try:
        if sb.is_element_visible("h1.hero-title"):
            t = (sb.get_text("h1.hero-title") or "").strip()
            if "welcome back" in t.lower():
                return True, t
    except Exception:
        pass
    try:
        if sb.is_element_visible(LOGOUT_SEL):
            return True, None
    except Exception:
        pass
    return False, None


def _extract_server_id(href: str) -> Optional[str]:
    m = re.search(r"/servers/(\d+)", href or "")
    return m.group(1) if m else None


def _find_server_id_and_go(sb) -> Tuple[Optional[str], bool]:
    try:
        sb.wait_for_element_visible(SERVER_CARD_LINK_SEL, timeout=25)
    except Exception:
        screenshot(sb, f"server_card_not_found_{int(time.time())}.png")
        return None, False

    href = ""
    try:
        href = sb.get_attribute(SERVER_CARD_LINK_SEL, "href") or ""
    except Exception:
        pass

    server_id = _extract_server_id(href)
    if not server_id:
        screenshot(sb, f"server_id_extract_failed_{int(time.time())}.png")
        return None, False

    print(f"🧭 server_id={server_id}，跳转 server 页...")
    try:
        sb.scroll_to(SERVER_CARD_LINK_SEL)
        time.sleep(0.3)
        sb.click(SERVER_CARD_LINK_SEL)
        sb.wait_for_element_visible(NOW_MANAGING_XPATH, timeout=30)
        return server_id, True
    except Exception:
        pass

    # 兜底：直接打开 URL
    try:
        sb.open(SERVER_URL_TPL.format(server_id=server_id))
        sb.wait_for_element_visible(NOW_MANAGING_XPATH, timeout=30)
        return server_id, True
    except Exception:
        screenshot(sb, f"goto_server_failed_{int(time.time())}.png")
        return server_id, False


# ✅ 核心修复：直接访问 /logout，无需查找/点击按钮
def _do_logout(sb) -> bool:
    try:
        print(f"🚪 访问 {LOGOUT_URL}")
        sb.open(LOGOUT_URL)
        sb.wait_for_element_visible("body", timeout=20)
        time.sleep(1.5)
        url_now = (sb.get_current_url() or "").lower()
        if "/login" in url_now or sb.is_element_visible(EMAIL_SEL):
            return True
    except Exception as e:
        print(f"⚠️ 退出失败：{e}")
    screenshot(sb, f"logout_failed_{int(time.time())}.png")
    return False


def _post_login_flow(sb) -> Tuple[Optional[str], bool]:
    server_id, entered_ok = _find_server_id_and_go(sb)
    if not entered_ok:
        return server_id, False

    stay1 = random.randint(4, 6)
    print(f"⏳ server 页停留 {stay1}s")
    time.sleep(stay1)

    print(f"↩️ 返回首页 {HOME_URL}")
    try:
        sb.open(HOME_URL)
        sb.wait_for_element_visible("body", timeout=30)
    except Exception:
        screenshot(sb, f"back_home_failed_{int(time.time())}.png")

    stay2 = random.randint(3, 5)
    print(f"⏳ 首页停留 {stay2}s")
    time.sleep(stay2)

    logout_ok = _do_logout(sb)
    return server_id, logout_ok


def login_then_flow(email: str, password: str) -> Tuple[str, Optional[str], bool, str, Optional[str], bool]:
    with SB(uc=True, locale="en", test=True) as sb:
        print("🚀 浏览器启动")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5.0)
        time.sleep(2)

        try:
            sb.wait_for_element_visible(EMAIL_SEL,  timeout=25)
            sb.wait_for_element_visible(PASS_SEL,   timeout=25)
            sb.wait_for_element_visible(SUBMIT_SEL, timeout=25)
        except Exception:
            return "FAIL", None, _has_cf_clearance(sb), sb.get_current_url() or "", None, False

        sb.clear(EMAIL_SEL);  sb.type(EMAIL_SEL,  email)
        sb.clear(PASS_SEL);   sb.type(PASS_SEL,   password)
        _try_click_captcha(sb, "提交前")
        sb.click(SUBMIT_SEL)
        sb.wait_for_element_visible("body", timeout=30)
        time.sleep(2)
        _try_click_captcha(sb, "提交后")

        has_cf      = _has_cf_clearance(sb)
        current_url = (sb.get_current_url() or "").strip()

        logged_in, welcome_text = False, None
        for _ in range(10):
            logged_in, welcome_text = _is_logged_in(sb)
            if logged_in:
                break
            time.sleep(1)

        if not logged_in:
            return "FAIL", welcome_text, has_cf, current_url, None, False

        server_id, logout_ok = _post_login_flow(sb)

        try:
            current_url = (sb.get_current_url() or "").strip()
        except Exception:
            pass

        return "OK", welcome_text, has_cf, current_url, server_id, logout_ok


def main():
    accounts = build_accounts_from_env()
    display  = setup_xvfb()

    ok = fail = logout_ok_count = 0
    tg_dests: set = set()
    lines:    List[str] = []

    try:
        for i, acc in enumerate(accounts, start=1):
            email    = acc["email"]
            password = acc["password"]
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat  = (acc.get("tg_chat")  or "").strip()
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))

            safe = mask_email_keep_domain(email)
            print("\n" + "=" * 60)
            print(f"👤 [{i}/{len(accounts)}] {safe}")
            print("=" * 60)

            try:
                status, _, has_cf, url_now, server_id, logout_ok = login_then_flow(email, password)
                if status == "OK":
                    ok += 1
                    if logout_ok:
                        logout_ok_count += 1
                    line = (
                        f"✅ {safe} | server:{server_id or '?'} | "
                        f"退出:{'✅' if logout_ok else '❌'} | "
                        f"cf:{'OK' if has_cf else 'NONE'}"
                    )
                else:
                    fail += 1
                    line = f"❌ {safe} | 登录失败 | cf:{'OK' if has_cf else 'NONE'} | 页:{url_now}"
            except Exception as e:
                fail += 1
                line = f"❌ {safe} | 脚本异常：{e}"

            print(line)
            lines.append(line)

            if i < len(accounts):
                time.sleep(10)

        summary = (
            f"📌 Lunes BetaDash 批量保活完成\n"
            f"登录成功 {ok} / 失败 {fail} | 退出成功 {logout_ok_count}/{ok}\n"
            f"{'─' * 30}\n"
            + "\n".join(lines)
        )
        print("\n" + summary)
        for token, chat in sorted(tg_dests):
            tg_send(summary, token, chat)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
