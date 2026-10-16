# cookie_manager.py
"""
功能：自动化浏览器获取 www.douyin.com 的登录 Cookie。
从原 dy_scripts/get_cookie_by_browser.py 迁移而来
"""

import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 默认 cookie 文件路径（项目根目录）
COOKIE_FILE = "douyin_cookie.txt"
FORCE_LOGIN = False  # 如需强制重新登录覆盖cookie，设为True

INVALID_LOGIN_HINT = "登录后即可观看喜欢、收藏的视频"

# 登录完成后的"保存登录状态"提示浮层上的确认按钮文本（DOM 浮层，非原生 dialog）
_LOGIN_PROMPT_ACCEPT_TEXTS = ("确定", "保存")
# 抖音风控"验证码中间页"的页面标题特征
_CAPTCHA_WALL_TITLE = "验证码中间页"
DOUYIN_LOGIN_URL = "https://www.douyin.com/"
# 抖音域名后缀（cookie domain 过滤用，兼容 www.douyin.com / .douyin.com 等）
_DY_DOMAIN_SUFFIX = "douyin.com"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("douyin_cookie")

# 全局变量
_global_playwright = None
_global_browser = None


def _find_browser_executable(browser_path: Optional[str] = None) -> Optional[str]:
    if browser_path:
        return browser_path

    candidates = []
    if sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
        )
    candidates.extend(
        path
        for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
        if (path := shutil.which(command))
    )

    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def init_global_browser(headless=True, browser_path=None):
    global _global_playwright, _global_browser
    if _global_playwright is None:
        _global_playwright = sync_playwright().start()
    if _global_browser is None:
        browser_args = {}
        executable_path = _find_browser_executable(browser_path)
        if executable_path:
            browser_args["executable_path"] = executable_path
        try:
            _global_browser = _global_playwright.chromium.launch(
                headless=headless, **browser_args
            )
        except Exception:
            try:
                _global_playwright.stop()
            finally:
                _global_playwright = None
            raise
    return _global_browser


def close_global_browser():
    global _global_playwright, _global_browser
    if _global_browser:
        _global_browser.close()
        _global_browser = None
    if _global_playwright:
        _global_playwright.stop()
        _global_playwright = None


def extract_douyin_cookie_from_playwright(cookies: List[Dict]) -> str:
    """
    从 Playwright cookies 列表拼接成标准 Cookie 字符串。
    """
    return "; ".join([f"{c['name']}={c['value']}" for c in cookies])


def load_local_cookie(cookie_file: str = None) -> str:
    """从本地文件加载cookie字符串。"""
    if cookie_file is None:
        cookie_file = COOKIE_FILE

    logger.info(f"[DY-COOKIE] 尝试加载本地cookie文件: {cookie_file}")
    if os.path.exists(cookie_file):
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
                if cookie:
                    logger.info(
                        f"[DY-COOKIE] 已从本地加载抖音cookie，长度: {len(cookie)}"
                    )
                    # 记录关键字段
                    session = extract_cookie_field(
                        cookie, "sessionid_ss"
                    ) or extract_cookie_field(cookie, "sessionid")
                    user_id = extract_cookie_field(cookie, "user_id")
                    logger.info(
                        f"[DY-COOKIE] Cookie信息 - sessionid: {session[:20] if session else 'None'}..., user_id: {user_id}"
                    )
                    return cookie
                else:
                    logger.warning("[DY-COOKIE] Cookie文件为空")
        except Exception as e:
            logger.error(f"[DY-COOKIE] 读取本地cookie文件失败: {e}")
    else:
        logger.info(f"[DY-COOKIE] Cookie文件不存在: {cookie_file}")
    return ""


def save_cookie_to_local(cookie: str, cookie_file: str = None):
    """保存cookie到本地文件。"""
    if cookie_file is None:
        cookie_file = COOKIE_FILE

    try:
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie)
        logger.info("[DY-COOKIE] 已保存cookie到本地文件: {cookie_file}")
    except Exception as e:
        logger.error("[DY-COOKIE] 保存cookie到本地失败: {e}")


def extract_cookie_field(cookie_str, field):
    for item in cookie_str.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            if k.strip() == field:
                return v.strip()
    return ""


def is_cookie_logged_in(cookie_str):
    """
    检查Cookie是否已登录

    要求：
    1. sessionid_ss 或 sessionid 必须存在且有效（长度>10）
    2. Cookie总长度应该大于200（未登录时通常很短）

    这样可以避免误判未登录状态的Cookie为已登录
    """
    if not cookie_str:
        logger.debug("[DY-COOKIE] 验证失败: cookie为空")
        return False

    cookie = str(cookie_str)

    # Cookie长度检查：登录后的Cookie通常较长（>200字符）
    if len(cookie) < 200:
        logger.debug(f"[DY-COOKIE] 验证失败: Cookie长度{len(cookie)} < 200")
        return False

    # 检查核心登录字段
    session_keys = ["sessionid_ss", "sessionid"]
    has_valid_session = False
    session_value = None
    for key in session_keys:
        value = extract_cookie_field(cookie, key)
        if value and len(value) > 10:
            has_valid_session = True
            session_value = value[:20]
            break

    if not has_valid_session:
        logger.debug("[DY-COOKIE] 验证失败: 未找到有效的sessionid")
        return False

    # 检查是否包含用户ID相关的字段（登录后才有）
    user_id = extract_cookie_field(cookie, "user_id")
    csrf = extract_cookie_field(cookie, "passport_csrf_token")

    if not user_id and not csrf:
        logger.debug("[DY-COOKIE] 验证失败: 未找到user_id或passport_csrf_token")
        return False

    logger.info(
        f"[DY-COOKIE] Cookie验证通过 - 长度:{len(cookie)}, session:{session_value}..., user_id:{user_id is not None}"
    )
    return True


def build_douyin_gateway_headers(headers: Optional[Dict], cookie: str) -> Dict:
    """补齐抖音网关请求头，保留用户配置且不修改原字典。

    兼容 F2 官方修复 9b759fc：旧发行版缺少这两个请求头会返回 403。
    https://github.com/Johnserf-Seed/f2/commit/9b759fc178237b35cd531c635fd1176e4a9873e7
    """
    merged = dict(headers or {})
    present = {str(name).lower() for name in merged}
    if "x-tt-argus" not in present:
        merged["x-tt-argus"] = "1"
    if "uifid" not in present:
        cookie = cookie if isinstance(cookie, str) else ""
        uifid = extract_cookie_field(cookie, "UIFID") or extract_cookie_field(cookie, "UIFID_TEMP")
        if uifid:
            merged["uifid"] = uifid
    return merged


def _is_captcha_wall(page) -> bool:
    """
    检测页面是否落在抖音风控的"验证码中间页"（滑块/行为验证）。

    Args:
        page: Playwright 页面对象

    Returns:
        是否命中验证码中间页
    """
    try:
        return _CAPTCHA_WALL_TITLE in page.title()
    except Exception:
        return False


def _try_accept_login_prompt(page) -> bool:
    """
    自动点击登录完成后的"保存登录状态"提示浮层上的确认按钮。

    该提示在抖音 web 当前版本是页面内 DOM 浮层（非原生 dialog，
    原生 dialog 由 page.on("dialog") 的 handler 自动 accept 兜底）。
    只点击"确定/保存"这类确认语义的按钮：
    - 未登录时的扫码弹层上不存在"确定"按钮，误点风险低；
    - 不自动点击"取消"，因为扫码弹层（关闭登录框）与登录完成提示
      中都可能出现"取消"，自动点击可能误关扫码窗口。

    Args:
        page: Playwright 页面对象

    Returns:
        是否执行了一次点击
    """
    for text in _LOGIN_PROMPT_ACCEPT_TEXTS:
        try:
            locator = page.get_by_text(text, exact=True)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=2000)
                logger.info(f"[DY-COOKIE] 已自动点击登录完成提示按钮: {text}")
                return True
        except Exception:
            continue
    return False


def _is_logged_in_by_storage(page) -> bool:
    """
    读取页面 localStorage 的登录信号（与 MediaCrawler 同款信号）。

    仅作为 cookie 校验之外的辅助诊断信号：当 localStorage 已标记登录
    但 cookie 校验未通过时，说明登录态可能尚未完整写入，继续等待。

    Args:
        page: Playwright 页面对象

    Returns:
        localStorage["HasUserLogin"] 是否为 "1"
    """
    try:
        return page.evaluate(
            "() => window.localStorage.getItem('HasUserLogin') === '1'"
        )
    except Exception:
        return False


def get_cookie_by_playwright(browser) -> str:
    logger.info("[DY-COOKIE] 正在打开抖音扫码登录页...")
    context = browser.new_context()
    page = context.new_page()

    # 自动处理原生 JS 弹窗（alert/confirm/prompt）
    def handle_dialog(dialog):
        logger.info(f"[DY-COOKIE] 检测到弹窗: {dialog.message}")
        try:
            dialog.accept()
            logger.info("[DY-COOKIE] 已自动接受弹窗。")
        except Exception as e:
            logger.warning(f"[DY-COOKIE] 自动接受弹窗失败: {e}")

    page.on("dialog", handle_dialog)
    try:
        page.goto(DOUYIN_LOGIN_URL)
    except Exception as e:
        logger.error("[DY-COOKIE] 打开抖音登录页失败: %s", type(e).__name__)
        context.close()
        raise RuntimeError("无法打开抖音登录页，请检查网络后重试") from e
    logger.info(
        "[DY-COOKIE] 请在弹出的浏览器窗口中完成抖音登录，系统将自动检测登录状态..."
    )
    logger.info(
        "[DY-COOKIE] 提示: 登录成功后如弹出'保存登录状态'提示，请点击确认；系统也会尝试自动点击"
    )
    max_wait = 300
    poll_interval = 3
    start_time = time.time()
    last_cookie_str = ""
    captcha_wall_seen = False
    prompt_clicked = False
    storage_logged_in = False
    while time.time() - start_time < max_wait:
        try:
            # 多信号读取：取全部 cookie 并按 douyin.com 域过滤，
            # 避免只按 https://www.douyin.com/ 过滤遗漏子域 cookie
            all_cookies = context.cookies()
            dy_cookies = [
                c
                for c in all_cookies
                if c.get("domain", "").rstrip(".").endswith(_DY_DOMAIN_SUFFIX)
            ]
            cookie_str = extract_douyin_cookie_from_playwright(dy_cookies)
            # 只在cookie非空且变化且长度大于50时检测
            if cookie_str and cookie_str != last_cookie_str and len(cookie_str) > 50:
                last_cookie_str = cookie_str
                logger.debug(
                    f"[DY-COOKIE] 当前cookie({len(dy_cookies)}个): "
                    + (", ".join(c["name"] for c in dy_cookies) or "(空)")
                )
                if is_cookie_logged_in(cookie_str):
                    logger.info(
                        "[DY-COOKIE] 检测到cookie关键字段，自动获取到抖音登录Cookie。"
                    )
                    save_cookie_to_local(cookie_str)
                    context.close()
                    return cookie_str
            # 检测风控"验证码中间页"
            if _is_captcha_wall(page):
                if not captcha_wall_seen:
                    captcha_wall_seen = True
                    logger.warning(
                        "[DY-COOKIE] 检测到抖音风控'验证码中间页'，可能需要完成滑块验证；"
                        "如持续无法登录，请更换网络环境后重试"
                    )
            # 自动点击登录完成后的"保存登录状态"浮层（DOM 浮层）
            if not prompt_clicked and _try_accept_login_prompt(page):
                prompt_clicked = True
            # 辅助诊断：localStorage 登录信号
            if not storage_logged_in and _is_logged_in_by_storage(page):
                storage_logged_in = True
                logger.info(
                    "[DY-COOKIE] 检测到页面localStorage登录信号，但cookie校验未通过，"
                    "可能登录态尚未完整写入，继续等待..."
                )
        except Exception as e:
            logger.error(f"[DY-COOKIE] 获取cookie失败: {e}")
        time.sleep(poll_interval)

    # 超时：输出诊断信息，便于定位原因
    logger.warning("[DY-COOKIE] 超时未检测到登录Cookie。")
    try:
        logger.warning(f"[DY-COOKIE] 诊断 - 页面标题: {page.title()}")
    except Exception:
        pass
    try:
        all_cookies = context.cookies()
        dy_cookies = [
            c
            for c in all_cookies
            if c.get("domain", "").rstrip(".").endswith(_DY_DOMAIN_SUFFIX)
        ]
        logger.warning(
            f"[DY-COOKIE] 诊断 - 当前cookie({len(dy_cookies)}个): "
            + (", ".join(c["name"] for c in dy_cookies) or "(空)")
        )
    except Exception as e:
        logger.warning(f"[DY-COOKIE] 诊断 - 读取cookie失败: {e}")
    logger.warning(f"[DY-COOKIE] 诊断 - localStorage登录信号: {storage_logged_in}")
    if captcha_wall_seen:
        logger.warning(
            "[DY-COOKIE] 诊断 - 曾检测到风控'验证码中间页'，建议更换网络环境后重试"
        )
    if prompt_clicked:
        logger.warning(
            "[DY-COOKIE] 诊断 - 曾自动点击登录完成提示按钮，但登录态仍未就绪"
        )
    context.close()
    return ""


def get_douyin_cookie_auto(force_refresh=False, headless=True, browser_path=None):
    """
    自动获取抖音Cookie（推荐接口）

    Args:
        force_refresh: 是否强制重新获取
        headless: 是否使用无头模式
        browser_path: 浏览器路径（可选）

    Returns:
        Cookie字符串
    """
    # 如果不是强制刷新，先尝试加载本地cookie
    if not force_refresh:
        local_cookie = load_local_cookie()
        if local_cookie and is_cookie_logged_in(local_cookie):
            logger.info("[DY-COOKIE] 使用本地缓存的Cookie")
            return local_cookie

    # 通过浏览器获取
    logger.info("[DY-COOKIE] 启动扫码登录浏览器...")
    browser = init_global_browser(headless=headless, browser_path=browser_path)
    try:
        cookie = get_cookie_by_playwright(browser)
        if cookie:
            return cookie
        else:
            raise RuntimeError("未能获取到有效的Cookie")
    finally:
        close_global_browser()


# 保持向后兼容的别名
get_douyin_cookie = get_douyin_cookie_auto
