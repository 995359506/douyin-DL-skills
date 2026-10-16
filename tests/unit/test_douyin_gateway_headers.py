"""抖音网关请求头兼容，不读取本地 Cookie、不联网。"""

import pytest

from skills.douyin_downloader.cookie_manager import build_douyin_gateway_headers
from skills.douyin_downloader.core import DouyinDownloadSkill
from skills.douyin_downloader.downloader import DouyinDownloader


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("", None),
        ("UIFID=current-test; UIFID_TEMP=temp-test", "current-test"),
        ("UIFID_TEMP=temp-test", "temp-test"),
        ("UIFID=; UIFID_TEMP=temp-test", "temp-test"),
        ("UIFID=current=test", "current=test"),
    ],
)
def test_gateway_headers_follow_current_cookie(cookie, expected):
    headers = {"User-Agent": "test-agent"}
    result = build_douyin_gateway_headers(headers, cookie)
    assert result["x-tt-argus"] == "1"
    assert result.get("uifid") == expected
    assert result["User-Agent"] == "test-agent"
    assert headers == {"User-Agent": "test-agent"}
    assert "Cookie" not in result


def test_gateway_headers_preserve_explicit_configuration_case_insensitively():
    configured = {"X-TT-ARGUS": "configured-test", "Uifid": "configured-user"}
    result = build_douyin_gateway_headers(configured, "UIFID=from-cookie-test")
    assert result == configured
    assert result is not configured


@pytest.mark.parametrize("kind", [DouyinDownloadSkill, DouyinDownloader])
def test_both_download_entry_points_include_gateway_headers(kind):
    service = kind.__new__(kind)
    service.douyin_conf = {}
    service._cookie = None
    result = service._build_kwargs({"cookie": "UIFID=current-test"})
    assert result["headers"]["x-tt-argus"] == "1"
    assert result["headers"]["uifid"] == "current-test"
