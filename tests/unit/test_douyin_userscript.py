"""油猴视频识别、主页收集及后台批次回归，不发下载或模型请求。"""

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
  await assert.rejects(requestDownload(first, o => o.onload({status:200,responseText:'null'})), /HTTP 200/);
  await assert.rejects(requestDownload(first, o => o.onerror()), /本地工作台/);
  await assert.rejects(requestDownload(first, o => o.ontimeout()), /任务可能已创建/);
}
checkSubmit().catch(error => { console.error(error); process.exitCode = 1; });
""", str(script)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_userscript_refresh_only_monitors_and_retries_the_same_batch():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要前端构建使用的 Node.js")
    script = Path(__file__).resolve().parents[2] / "frontend/public/userscripts/douyin-download-summary.user.js"
    result = subprocess.run(
        [node, "-e", r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
assert.match(source, /^\/\/ @version\s+1\.2\.3$/m);
assert.match(source, /默认 30 秒/);
const first = '7640783480796999754';
const second = '7691681728880477119';
const request = {video_ids:[first,second],author:'测试博主',profile_id:'MS4w-test',
  client_request_id:'fd180491-d94b-4cda-bf88-a3d25bcc38f8',auto_summary:true};
const batch = {batch_id:'test-batch',author:'测试博主',profile_id:'MS4w-test',status:'running',
  total:2,completed:0,failed:0,pending:2,cancelled:0,next_allowed_at:0,interval_seconds:180};
function mount(saved) {
  const nodes = new Map();
  let activeElement = null;
  const makeNode = () => ({hidden:false,attributes:{},listeners:{},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    focus() { activeElement = this; },
  });
  const shadow = {
    set innerHTML(value) {
      // 从真实模板读取初始属性，避免伪 DOM 替脚本补上默认收起状态。
      for (const [tag, id] of value.matchAll(/<[a-z][^>]*\bid="([^"]+)"[^>]*>/g)) {
        const node = makeNode();
        node.hidden = /\shidden(?:\s|>)/.test(tag);
        for (const [, name, attr] of tag.matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(name, attr);
        nodes.set(id, node);
      }
    },
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, makeNode());
      return nodes.get(id);
    },
  };
  const calls = [];
  const events = {};
  const capture = {};
  let storage = JSON.stringify(saved);
  const context = {
    URL, innerWidth:1200, innerHeight:800,
    location:{href:'https://www.douyin.com/user/MS4w-test'},
    document:{getElementById:()=>null, createElement:()=>({attachShadow:()=>shadow}),
      body:{append() {}}, querySelectorAll:()=>[], hidden:false},
    sessionStorage:{getItem:()=>storage,setItem:(_,value)=>{storage=value;},removeItem:()=>{storage=null;}},
    crypto:{randomUUID:()=>{throw new Error('恢复已有请求不应生成新编号');}},
    setInterval() {}, addEventListener:(name,listener,useCapture)=>{events[name]=listener;capture[name]=useCapture;},
    confirm:()=>true,
    GM_xmlhttpRequest(options) {
      calls.push(options);
      options.onload({status:200,responseText:JSON.stringify({success:true,data:batch})});
    },
  };
  vm.runInNewContext(source, context);
  return {nodes,calls,events,capture,readSaved:()=>JSON.parse(storage),activeElement:()=>activeElement};
}
function checkCompactPanel(mounted) {
  const {nodes,calls,events,capture} = mounted;
  const launcher = nodes.get('launcher');
  const panel = nodes.get('panel');
  const collapse = nodes.get('collapse');
  assert.ok(launcher && panel && collapse);
  assert.equal(launcher.hidden, false);
  assert.equal(panel.hidden, true);
  assert.equal(launcher.getAttribute('aria-controls'), 'panel');
  assert.equal(launcher.getAttribute('aria-expanded'), 'false');
  assert.equal(capture.keydown, true); // 视频页的键盘冒泡拦截不应挡住 Escape。
  const callsBefore = calls.length;
  const savedBefore = JSON.stringify(mounted.readSaved());
  const statusBefore = nodes.get('batch-status').textContent;
  launcher.listeners.click();
  assert.equal(panel.hidden, false);
  assert.equal(launcher.hidden, true);
  assert.equal(launcher.getAttribute('aria-expanded'), 'true');
  assert.equal(mounted.activeElement(), collapse);
  events.keydown({key:'Enter',preventDefault() { throw new Error('非 Escape 不应被拦截'); }});
  assert.equal(panel.hidden, false);
  collapse.listeners.click();
  assert.equal(panel.hidden, true);
  assert.equal(launcher.hidden, false);
  assert.equal(launcher.getAttribute('aria-expanded'), 'false');
  assert.equal(mounted.activeElement(), launcher);
  launcher.listeners.click();
  events.keydown({key:'Escape',preventDefault() {},stopPropagation() {}});
  assert.equal(panel.hidden, true);
  assert.equal(launcher.hidden, false);
  assert.equal(launcher.getAttribute('aria-expanded'), 'false');
  assert.equal(mounted.activeElement(), launcher);
  assert.equal(calls.length, callsBefore); // 展开与收起不能提交下载或控制批次。
  assert.equal(JSON.stringify(mounted.readSaved()), savedBefore);
  assert.equal(nodes.get('batch-status').textContent, statusBefore);
}
async function checkRestore() {
  const restored = mount({version:2,batch_id:'test-batch',author:'测试博主'});
  await new Promise(setImmediate);
  assert.equal(restored.calls.length, 1);
  assert.equal(restored.calls[0].method, 'GET'); // 页面刷新只读后台状态。
  checkCompactPanel(restored);
  assert.match(restored.nodes.get('batch-status').textContent, /完成 0\/2/);
  assert.equal(restored.nodes.get('batch-start').hidden, true);
  let prevented = false;
  restored.events.beforeunload({preventDefault:()=>{prevented=true;}});
  assert.equal(prevented, false); // 收到批次编号后关闭网页不再拦截，也不发送取消。
  assert.equal(restored.calls.length, 1);

  const unknown = mount({version:2,request});
  await new Promise(setImmediate);
  assert.equal(unknown.calls.length, 0); // 超时刷新不自动重发 POST。
  assert.equal(unknown.nodes.get('batch-start').textContent, '确认上次提交');
  checkCompactPanel(unknown);
  assert.deepEqual(JSON.parse(JSON.stringify(unknown.readSaved().request)), request);
  await unknown.nodes.get('batch-start').listeners.click();
  assert.equal(unknown.calls.length, 1);
  assert.equal(unknown.calls[0].method, 'POST');
  assert.deepEqual(JSON.parse(unknown.calls[0].data), request);
  assert.equal(unknown.readSaved().batch_id, 'test-batch');
  assert.equal('request' in unknown.readSaved(), false);
  assert.match(unknown.nodes.get('batch-status').textContent, /可关闭此网页/);

  const legacy = mount({ids:[first,second],done:1,status:'paused',notBefore:180000});
  await new Promise(setImmediate);
  assert.equal(legacy.calls.length, 0);
  assert.match(legacy.nodes.get('batch-status').textContent, /旧版.*未自动迁移.*重新收集/);
  assert.equal(legacy.nodes.get('batch-start').hidden, true);
}
checkRestore().catch(error => { console.error(error); process.exitCode = 1; });
""", str(script)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_userscript_profile_scope_and_background_batch_submission():
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
const { profileIdFromUrl, readProfilePosts, batchSubmission, requestBatch, requestBatchState } = context.module.exports;
const href = 'https://www.douyin.com/user/MS4w-test';
const first = '7640783480796999754';
const second = '7691681728880477119';
const third = '7691681728880477120';
assert.equal(profileIdFromUrl(href), 'MS4w-test');
assert.equal(profileIdFromUrl(`${href}?modal_id=${first}`), '');
assert.equal(profileIdFromUrl('https://www.douyin.com.evil.example/user/abc'), '');
assert.equal(profileIdFromUrl(`https://www.douyin.com/video/${first}`), '');
let tab = '作品22';
let search = '';
let footer = '';
const visible = {getClientRects: () => [{}], closest: () => null};
const links = [`/video/${first}`, `/video/${first}?from=profile`, `/video/${second}`,
  `/note/${third}`, `https://www.douyin.com.evil.example/video/${third}`, '/video/not-an-id', '/mix/12345'];
const list = {
  ...visible,
  // 作品标题即使含“没有更多”或“服务异常”也不能当作页尾或加载错误。
  children: [
    {matches: () => true, textContent: '没有更多 服务异常'},
    {matches: () => false, querySelector: () => null, get textContent() { return footer; }},
  ],
  querySelectorAll: selector => {
    assert.equal(selector, 'a[href]');
    return links.map(href => ({getAttribute: () => href}));
  },
};
const detail = {
  ...visible,
  querySelectorAll: selector => selector === '[data-e2e="user-post-list"]' ? [list]
    : [{...visible, textContent: tab}, {...visible, textContent: '作品'}],
  querySelector: selector => {
    if (selector === '[data-e2e="user-post-list"]') return list;
    if (selector === '[data-e2e="user-tab-count"]') return {textContent: '22'};
    if (selector === '[data-e2e="user-info"] h1') return {textContent: ' 测试博主 '};
    if (selector === 'input[placeholder="搜索 Ta 的作品"]') return {value: search};
    throw new Error(`意外选择器 ${selector}`);
  },
};
const doc = {defaultView: {getComputedStyle: () => ({visibility: 'visible'})}, querySelectorAll: selector => {
  assert.equal(selector, '[data-e2e="user-detail"]');
  return [detail, {getClientRects: () => []}]; // SPA 隐藏旧主页不能参与采集。
}};
let profile = readProfilePosts(doc, href);
assert.equal(JSON.stringify(profile.ids), JSON.stringify([first, second]));
assert.equal(JSON.stringify(profile.notes), JSON.stringify([third]));
assert.equal(profile.author, '测试博主');
assert.equal(profile.expected, 22);
assert.equal(profile.ended, false);
const originalRects = list.getClientRects;
list.getClientRects = () => [];
assert.throws(() => readProfilePosts(doc, href), /唯一.*作品列表/);
list.getClientRects = originalRects;
const originalDetails = doc.querySelectorAll;
doc.querySelectorAll = () => [detail, detail];
assert.throws(() => readProfilePosts(doc, href), /加载稳定/);
doc.querySelectorAll = originalDetails;
footer = '没有更多了';
assert.equal(readProfilePosts(doc, href).ended, true);
footer = '服务异常，重新刷新拉取数据';
assert.throws(() => readProfilePosts(doc, href), /登录或验证/);
footer = '';
tab = '喜欢';
assert.throws(() => readProfilePosts(doc, href), /作品.*标签/);
tab = '作品'; search = '筛选';
assert.throws(() => readProfilePosts(doc, href), /清空作品搜索/);
assert.throws(() => readProfilePosts(doc, `https://www.douyin.com/video/${first}`), /博主主页/);

async function checkBatch() {
  const requestId = 'fd180491-d94b-4cda-bf88-a3d25bcc38f8';
  const submission = batchSubmission([first, first, second, third], {author: '测试博主', profileId: 'MS4w-test'}, requestId);
  const expected = {video_ids:[first, second, third],author:'测试博主',profile_id:'MS4w-test',client_request_id:requestId,auto_summary:true};
  assert.deepEqual(JSON.parse(JSON.stringify(submission)), expected);
  assert.throws(() => batchSubmission([first, 7640783480796999754], {profileId:'abc'}, requestId), /视频编号/);
  assert.throws(() => batchSubmission([first], {profileId:'../evil'}, requestId), /主页编号/);
  assert.throws(() => batchSubmission([first], {profileId:'abc'}, 'not-uuid'), /请求编号/);
  assert.throws(() => batchSubmission([], {profileId:'abc'}, requestId), /视频编号/);
  const data = {batch_id:'test-batch',author:'测试博主',profile_id:'MS4w-test',status:'queued',
    total:3,completed:0,failed:0,pending:3,cancelled:0,interval_seconds:180};
  const response = {status:200,responseText:JSON.stringify({success:true,data})};
  const calls = [];
  const transport = options => {
    calls.push(options);
    assert.equal(options.anonymous, true);
    assert.equal(Object.keys(options.headers).join(','), 'Content-Type');
    options.onload(response);
  };
  const result = await requestBatch(submission, transport);
  assert.equal(calls.length, 1); // 全量视频只发一次 POST，不在浏览器逐条排队。
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].url, 'http://127.0.0.1:8080/api/download-batches');
  assert.deepEqual(JSON.parse(calls[0].data), expected);
  assert.equal('interval_seconds' in JSON.parse(calls[0].data), false); // 下载间隔仅由后台控制。
  assert.equal(result.data.batch_id, 'test-batch');
  const retryBodies = [];
  await assert.rejects(requestBatch(submission, o => { retryBodies.push(o.data); o.ontimeout(); }), /任务可能已创建/);
  await requestBatch(JSON.parse(JSON.stringify(submission)), o => { retryBodies.push(o.data); o.onload(response); });
  assert.equal(retryBodies[0], retryBodies[1]); // 超时重试沿用同一请求 ID 和全量内容。
  await requestBatchState('test-batch', transport);
  assert.equal(calls.at(-1).method, 'GET');
  assert.equal(calls.at(-1).url, 'http://127.0.0.1:8080/api/download-batches/test-batch');
  assert.equal('data' in calls.at(-1), false);
  for (const action of ['pause', 'resume', 'stop']) {
    await requestBatchState('test-batch', transport, action);
    assert.equal(calls.at(-1).url, `http://127.0.0.1:8080/api/download-batches/test-batch/${action}`);
    assert.equal(calls.at(-1).method, 'POST');
    assert.equal(calls.at(-1).data, '{}');
  }
  await assert.rejects(requestBatch(submission, o => o.onload({status:200,responseText:'{"success":true,"data":{}}'})), /有效批次/);
  await assert.rejects(requestBatch(submission, o => o.onload({status:200,responseText:'{"success":true,"data":{"batch_id":"../evil"}}'})), /有效批次/);
  await assert.rejects(requestBatch(submission, o => o.onload({status:200,responseText:JSON.stringify({success:true,data:{...data,pending:'3'}})})), /有效批次/);
  await assert.rejects(requestBatchState('test-batch', o => o.onload({status:200,responseText:JSON.stringify({success:true,data:{...data,batch_id:'other'}})})), /编号不一致/);
  await assert.rejects(requestBatchState('../evil', transport), /操作无效/);
  await assert.rejects(requestBatchState('test-batch', transport, 'delete'), /操作无效/);
  await assert.rejects(requestBatch(submission, o => o.onload({status:503,responseText:'{"detail":{"message":"后台暂不可用","action":"检查本地服务"}}'})), /后台暂不可用.*检查本地服务/);
}
checkBatch().catch(error => { console.error(error); process.exitCode = 1; });
""", str(script)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
