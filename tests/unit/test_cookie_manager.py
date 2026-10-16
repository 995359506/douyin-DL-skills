"""
cookie_manager 单元测试：Cookie 校验、拼接与登录流程辅助逻辑

覆盖：
- extract_cookie_field / extract_douyin_cookie_from_playwright 纯函数
- is_cookie_logged_in 判定（含真实场景：无 user_id 但有 passport_csrf_token）
- _is_captcha_wall 验证码中间页检测
- _try_accept_login_prompt 保存登录态浮层自动点击
- _is_logged_in_by_storage localStorage 登录信号
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from skills.douyin_downloader import cookie_manager
from skills.douyin_downloader.cookie_manager import (
    extract_cookie_field,
    extract_douyin_cookie_from_playwright,
    is_cookie_logged_in,
    _is_captcha_wall,
    _try_accept_login_prompt,
    _is_logged_in_by_storage,
)


class TestCookieParsing:
    """Cookie 拼接与字段提取"""

    def test_extract_cookie_field(self):
        cookie = "a=1; sessionid=abc123; name=value with=space"
        assert extract_cookie_field(cookie, "sessionid") == "abc123"
        assert extract_cookie_field(cookie, "a") == "1"
        assert extract_cookie_field(cookie, "missing") == ""

    def test_extract_from_playwright(self):
        cookies = [
            {"name": "sessionid", "value": "abc"},
            {"name": "user_id", "value": "123"},
        ]
        assert (
            extract_douyin_cookie_from_playwright(cookies)
            == "sessionid=abc; user_id=123"
        )


class TestIsCookieLoggedIn:
    """is_cookie_logged_in 判定"""

    def test_empty(self):
        assert is_cookie_logged_in("") is False
        assert is_cookie_logged_in(None) is False

    def test_too_short(self):
        assert is_cookie_logged_in("short=1") is False

    def test_no_session(self):
        """长度够、有身份字段，但缺少 sessionid"""
        cookie = (
            "passport_csrf_token=" + "x" * 32
            + "; user_id=" + "y" * 19
            + "; pad=" + "z" * 300
        )
        assert is_cookie_logged_in(cookie) is False

    def test_no_identity(self):
        """有 sessionid，但无 user_id 也无 passport_csrf_token"""
        cookie = "sessionid=" + "s" * 32 + "; pad=" + "z" * 300
        assert is_cookie_logged_in(cookie) is False

    def test_sessionid_too_short(self):
        """sessionid 长度不足 10 视为无效"""
        cookie = (
            "sessionid=abc; user_id=" + "u" * 19 + "; pad=" + "z" * 300
        )
        assert is_cookie_logged_in(cookie) is False

    def test_full_logged_in(self):
        cookie = (
            "sessionid=" + "s" * 32
            + "; user_id=" + "u" * 19
            + "; pad=" + "z" * 300
        )
        assert is_cookie_logged_in(cookie) is True

    def test_session_with_csrf_only(self):
        """真实场景：无 user_id 但有 passport_csrf_token（本机 douyin_cookie.txt 同款）"""
        cookie = (
            "sessionid_ss=" + "s" * 32
            + "; passport_csrf_token=" + "c" * 32
            + "; pad=" + "z" * 300
        )
        assert is_cookie_logged_in(cookie) is True


class TestCaptchaWall:
    """验证码中间页检测"""

    def test_detected(self):
        page = mock.Mock()
        page.title.return_value = "验证码中间页"
        assert _is_captcha_wall(page) is True

    def test_not_detected(self):
        page = mock.Mock()
        page.title.return_value = "抖音 - 记录美好生活"
        assert _is_captcha_wall(page) is False

    def test_title_error_returns_false(self):
        page = mock.Mock()
        page.title.side_effect = Exception("boom")
        assert _is_captcha_wall(page) is False


class TestAcceptLoginPrompt:
    """保存登录态浮层自动点击"""

    @staticmethod
    def _make_page(text_to_count):
        """构造 mock page：get_by_text(text, exact=True) 按文本返回对应 locator"""
        page = mock.Mock()
        loc_map = {}
        for text, count in text_to_count.items():
            loc = mock.Mock()
            loc.count.return_value = count
            loc.first.is_visible.return_value = True  # 默认可见
            loc_map[text] = loc

        def side_effect(text, exact=True):
            return loc_map.get(text)

        page.get_by_text.side_effect = side_effect
        return page, loc_map

    def test_no_prompt_no_click(self):
        page, _ = self._make_page({"确定": 0, "保存": 0})
        assert _try_accept_login_prompt(page) is False

    def test_hidden_element_not_clicked(self):
        """匹配到但不可见的元素不点击（防误点）"""
        page, loc_map = self._make_page({"确定": 1, "保存": 0})
        loc_map["确定"].first.is_visible.return_value = False
        assert _try_accept_login_prompt(page) is False
        loc_map["确定"].first.click.assert_not_called()

    def test_click_first_confirm(self):
        page, loc_map = self._make_page({"确定": 1, "保存": 0})
        assert _try_accept_login_prompt(page) is True
        loc_map["确定"].first.click.assert_called_once()

    def test_fallback_to_second_text(self):
        """'确定'不可点/缺失时尝试'保存'"""
        page, loc_map = self._make_page({"确定": 1, "保存": 1})
        loc_map["确定"].first.click.side_effect = Exception("not visible")
        assert _try_accept_login_prompt(page) is True
        loc_map["保存"].first.click.assert_called_once()

    def test_all_click_fail_returns_false(self):
        page, loc_map = self._make_page({"确定": 1, "保存": 1})
        loc_map["确定"].first.click.side_effect = Exception("fail")
        loc_map["保存"].first.click.side_effect = Exception("fail")
        assert _try_accept_login_prompt(page) is False


class TestStorageSignal:
    """localStorage 登录信号"""

    def test_logged_in(self):
        page = mock.Mock()
        page.evaluate.return_value = True
        assert _is_logged_in_by_storage(page) is True

    def test_not_logged_in(self):
        page = mock.Mock()
        page.evaluate.return_value = False
        assert _is_logged_in_by_storage(page) is False

    def test_error_returns_false(self):
        page = mock.Mock()
        page.evaluate.side_effect = Exception("boom")
        assert _is_logged_in_by_storage(page) is False


class TestBrowserLaunch:
    def test_explicit_browser_path_is_used(self):
        assert cookie_manager._find_browser_executable("/Applications/Chrome") == "/Applications/Chrome"

    def test_finds_installed_google_chrome_on_macos(self, monkeypatch):
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        monkeypatch.setattr(cookie_manager.sys, "platform", "darwin")
        monkeypatch.setattr(cookie_manager.shutil, "which", lambda _command: None)
        monkeypatch.setattr(
            cookie_manager.Path,
            "is_file",
            lambda path: str(path) == chrome_path,
        )
        monkeypatch.setattr(cookie_manager.os, "access", lambda path, _mode: path == chrome_path)

        assert cookie_manager._find_browser_executable() == chrome_path

    def test_failed_launch_stops_playwright(self, monkeypatch):
        playwright = mock.Mock()
        playwright.chromium.launch.side_effect = RuntimeError("launch failed")
        context = mock.Mock()
        context.start.return_value = playwright
        monkeypatch.setattr(cookie_manager, "sync_playwright", mock.Mock(return_value=context))
        monkeypatch.setattr(cookie_manager, "_global_playwright", None)
        monkeypatch.setattr(cookie_manager, "_global_browser", None)
        monkeypatch.setattr(cookie_manager, "_find_browser_executable", lambda _path: "/Applications/Chrome")

        with pytest.raises(RuntimeError, match="launch failed"):
            cookie_manager.init_global_browser(headless=False)

        playwright.stop.assert_called_once()
        assert cookie_manager._global_playwright is None


def test_login_browser_opens_douyin_homepage_instead_of_login_api(monkeypatch):
    page = mock.Mock()
    context = mock.Mock()
    context.new_page.return_value = page
    context.cookies.return_value = [
        {"name": "sessionid", "value": "s" * 32, "domain": ".douyin.com"},
        {"name": "user_id", "value": "u" * 19, "domain": ".douyin.com"},
        {"name": "pad", "value": "p" * 200, "domain": ".douyin.com"},
    ]
    browser = mock.Mock()
    browser.new_context.return_value = context
    monkeypatch.setattr(cookie_manager, "save_cookie_to_local", mock.Mock())

    cookie = cookie_manager.get_cookie_by_playwright(browser)

    page.goto.assert_called_once_with("https://www.douyin.com/")
    context.close.assert_called_once()
    assert cookie_manager.is_cookie_logged_in(cookie)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
