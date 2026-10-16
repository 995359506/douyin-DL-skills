"""油猴当前视频识别与本地提交回归，不发下载或模型请求。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_userscript_selects_current_video_and_requests_auto_summary():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要前端构建使用的 Node.js")
    script = Path(__file__).resolve().parents[2] / "frontend/public/userscripts/douyin-download-summary.user.js"
    result = subprocess.run(
        [node, "-e", r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = { module: { exports: {} }, URL };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const { idFromUrl, currentVideoId, requestDownload } = context.module.exports;
const first = '7640783480796999754';
const second = '7691681728880477119';
assert.equal(idFromUrl(`https://www.douyin.com/video/${first}`), first);
assert.equal(idFromUrl(`https://www.douyin.com/?vid=${first}`), first);
assert.equal(idFromUrl(`https://www.douyin.com.evil.example/video/${first}`), '');
assert.equal(idFromUrl(`https://www.douyin.com/user/${first}`), '');
const viewport = {width: 1200, height: 800};
const element = (id, top = 0) => ({
  getAttribute: () => id,
  getBoundingClientRect: () => ({left: 100, right: 900, top, bottom: top+700}),
});
let items = [element(second), element(first, 900)];
const doc = {querySelectorAll: () => items};
assert.equal(currentVideoId(doc, `https://www.douyin.com/video/${first}`, viewport), second);
assert.equal(currentVideoId(doc, `https://www.douyin.com/?modal_id=${first}`, viewport), first);
items = [element(first), element(second)];
assert.equal(currentVideoId(doc, 'https://www.douyin.com/?recommend=1', viewport), '');
items = [];
assert.equal(currentVideoId(doc, `https://www.douyin.com/video/${first}`, viewport), first);
assert.equal(currentVideoId(doc, 'https://www.douyin.com/user/abc', viewport), '');

async function checkSubmit() {
  let calls = 0;
  const result = await requestDownload(second, options => {
    calls++;
    assert.equal(options.method, 'POST');
    assert.equal(options.url, 'http://127.0.0.1:8080/api/download');
    assert.equal(options.anonymous, true);
    assert.deepEqual(JSON.parse(options.data), {url:`https://www.douyin.com/video/${second}`,auto_summary:true});
    assert.equal(Object.keys(options.headers).join(','), 'Content-Type');
    options.onload({status:200,responseText:JSON.stringify({success:true,data:{task_id:'test-task'}})});
  });
  assert.equal(calls, 1);
  assert.equal(result.data.task_id, 'test-task');
  await assert.rejects(requestDownload(first, o => o.onload({status:401,responseText:'{"detail":{"message":"登录已过期","action":"请刷新登录"}}'})), /登录已过期.*请刷新登录/);
  await assert.rejects(requestDownload(first, o => o.onerror()), /本地工作台/);
  await assert.rejects(requestDownload(first, o => o.ontimeout()), /任务可能已创建/);
}
checkSubmit().catch(error => { console.error(error); process.exitCode = 1; });
""", str(script)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
