// ==UserScript==
// @name         抖音 · 一键下载并总结到本地工作台
// @namespace    douyin-local-workbench
// @version      1.2.3
// @description  单视频下载并总结；主页视频一次交给本机后台，按看板显示的间隔逐条下载（默认 30 秒）。
// @match        https://www.douyin.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// @noframes
// ==/UserScript==

(() => {
  'use strict';

  const BASE = 'http://127.0.0.1:8080';
  const PANEL_ID = 'douyin-local-download-summary';
  const STORAGE_KEY = `${PANEL_ID}:batch`;
  const validId = value => typeof value === 'string' && /^\d{5,32}$/.test(value) ? value : '';

  function idFromUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || url.hostname !== 'www.douyin.com') return '';
      return validId(url.searchParams.get('modal_id'))
        || validId(url.pathname.match(/^\/video\/(\d+)\/?$/)?.[1])
        || validId(url.searchParams.get('vid'));
    } catch {
      return '';
    }
  }

  function visibleArea(element, viewport) {
    const rect = element.getBoundingClientRect();
    const width = Math.max(0, Math.min(rect.right, viewport.width) - Math.max(rect.left, 0));
    const height = Math.max(0, Math.min(rect.bottom, viewport.height) - Math.max(rect.top, 0));
    return width * height;
  }

  function currentVideoId(doc, href, viewport) {
    // 搜索/主页的视频弹窗优先，不能误取弹窗下面仍在播放的推荐流。
    const url = new URL(href);
    const modal = validId(url.searchParams.get('modal_id'));
    if (modal) return modal;

    // 推荐流不一定更新地址栏；读取当前活动项，不扫描旁边的推荐链接。
    const active = [...doc.querySelectorAll('[data-e2e="feed-active-video"][data-e2e-vid]')]
      .map(element => ({ id: validId(element.getAttribute('data-e2e-vid')), area: visibleArea(element, viewport) }))
      .filter(item => item.id && item.area > 0)
      .sort((a, b) => b.area - a.area);
    if (active.length && (active.length === 1 || active[0].area > active[1].area * 1.5)) return active[0].id;
    if (active.length) return ''; // 切换动画中无法确定时，等待下一次识别。
    return idFromUrl(href);
  }

  function profileIdFromUrl(href) {
    try {
      const url = new URL(href);
      if (url.origin !== 'https://www.douyin.com' || url.searchParams.has('modal_id')) return '';
      return url.pathname.match(/^\/user\/([\w-]+)\/?$/)?.[1] || '';
    } catch { return ''; }
  }

  function readProfilePosts(doc, href) {
    const profileId = profileIdFromUrl(href);
    if (!profileId) throw new Error('请先打开博主主页，并关闭视频弹窗');
    const visible = el => el.getClientRects().length > 0 && !el.closest('[aria-hidden="true"]')
      && doc.defaultView.getComputedStyle(el).visibility !== 'hidden';
    const details = [...doc.querySelectorAll('[data-e2e="user-detail"]')].filter(visible);
    if (details.length !== 1) throw new Error('主页尚未加载稳定，请稍后重新收集');
    const detail = details[0];
    const tabs = [...detail.querySelectorAll('[role="tab"][aria-selected="true"]')].filter(visible);
    if (!tabs.length || tabs.some(tab => !/^作品/.test(tab.textContent.trim()))) {
      throw new Error('请切换到主页的“作品”标签，不能从喜欢、推荐或合集中收集');
    }
    if (detail.querySelector('input[placeholder="搜索 Ta 的作品"]')?.value.trim()) {
      throw new Error('请先清空作品搜索条件，再收集全部视频');
    }
    const lists = [...detail.querySelectorAll('[data-e2e="user-post-list"]')].filter(visible);
    if (lists.length !== 1) throw new Error('未找到唯一的主页作品列表，请等页面加载完成后再试');
    const list = lists[0];
    // 列表状态只读作品网格之外的提示，避免把视频标题当成“没有更多”。
    const text = [...list.children]
      .filter(el => !el.matches('[data-e2e="scroll-list"], ul') && !el.querySelector('a[href]'))
      .map(el => el.innerText || el.textContent || '').join('\n');
    if (/服务异常|加载失败|网络异常|请登录|登录后查看|访问频繁|操作频繁|安全验证|验证码/.test(text)) {
      throw new Error('主页作品未能加载，请先在抖音完成登录或验证，再重新收集');
    }
    const ids = new Set();
    const notes = new Set();
    for (const anchor of list.querySelectorAll('a[href]')) {
      try {
        const url = new URL(anchor.getAttribute('href'), href);
        if (url.origin !== 'https://www.douyin.com') continue;
        const match = url.pathname.match(/^\/(video|note)\/(\d+)\/?$/);
        if (match && validId(match[2])) (match[1] === 'video' ? ids : notes).add(match[2]);
      } catch { /* 忽略无效链接，不读取页面里的账号状态或 Cookie。 */ }
    }
    const countText = detail.querySelector('[data-e2e="user-tab-count"]')?.textContent.trim() || '';
    return {
      profileId, list, ids: [...ids], notes: [...notes],
      author: detail.querySelector('[data-e2e="user-info"] h1')?.textContent.trim().slice(0, 80) || '当前博主',
      expected: /^\d+$/.test(countText) ? Number(countText) : null,
      ended: /没有更多|暂无更多|已经到底|已展示全部|暂时没有作品|暂无作品/.test(text),
    };
  }

  function batchSubmission(ids, meta, requestId) {
    if (!Array.isArray(ids) || !ids.length || ids.some(id => !validId(id))) throw new Error('视频编号无效，请重新收集');
    if (!/^[\w-]+$/.test(meta.profileId || '')) throw new Error('主页编号无效，请重新收集');
    if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(requestId || '')) throw new Error('批次请求编号无效，请重新操作');
    return { video_ids: [...new Set(ids)], author: String(meta.author || '当前博主').slice(0, 80),
      profile_id: meta.profileId, client_request_id: requestId, auto_summary: true };
  }

  function requestLocal(method, path, data, transport) {
    return new Promise((resolve, reject) => {
      transport({
        method,
        url: `${BASE}${path}`,
        headers: { 'Content-Type': 'application/json' },
        ...(data === undefined ? {} : { data: JSON.stringify(data) }),
        anonymous: true,
        timeout: 60000,
        onload(response) {
          let result;
          try { result = JSON.parse(response.responseText); }
          catch { reject(new Error('本地接口返回异常，请打开工作台检查')); return; }
          if (response.status < 200 || response.status >= 300 || result?.success !== true) {
            const detail = result?.detail;
            const reason = typeof detail === 'string' ? detail : detail?.message || detail?.reason || result?.message || `HTTP ${response.status}`;
            const action = detail?.action ? `；${detail.action}` : '';
            reject(new Error(`${reason}${action}`));
            return;
          }
          resolve(result);
        },
        onerror() { reject(new Error('连接不到本地工作台，请确认 127.0.0.1:8080 已启动，并允许油猴访问本机')); },
        ontimeout() { reject(new Error('提交超时，任务可能已创建，请先到工作台查看')); },
      });
    });
  }

  async function requestDownload(id, transport) {
    const result = await requestLocal('POST', '/api/download', { url: `https://www.douyin.com/video/${id}`, auto_summary: true }, transport);
    if (!result.data?.task_id) throw new Error('接口未返回任务编号，请到工作台确认');
    return result;
  }

  function validateBatch(result) {
    const batch = result.data;
    if (!batch || !/^[\w-]{1,100}$/.test(batch.batch_id || '') || typeof batch.status !== 'string'
      || !['total', 'completed', 'failed', 'pending', 'cancelled'].every(key => Number.isInteger(batch[key]) && batch[key] >= 0)) {
      throw new Error('接口未返回有效批次信息，请到工作台确认；不要重新创建批次');
    }
    return result;
  }

  async function requestBatch(submission, transport) {
    const payload = batchSubmission(submission.video_ids,
      { author: submission.author, profileId: submission.profile_id }, submission.client_request_id);
    return validateBatch(await requestLocal('POST', '/api/download-batches', payload, transport));
  }

  async function requestBatchState(batchId, transport, action) {
    if (!/^[\w-]{1,100}$/.test(batchId || '') || (action && !['pause', 'resume', 'stop'].includes(action))) {
      throw new Error('批次操作无效，请到工作台确认');
    }
    const result = validateBatch(await requestLocal(action ? 'POST' : 'GET',
      `/api/download-batches/${batchId}${action ? `/${action}` : ''}`, action ? {} : undefined, transport));
    if (result.data.batch_id !== batchId) throw new Error('批次编号不一致，请到工作台确认');
    return result;
  }

  // 供本地无网络回归测试复用同一份识别与提交逻辑。
  if (typeof module === 'object' && module.exports) {
    module.exports = { idFromUrl, currentVideoId, requestDownload, profileIdFromUrl, readProfilePosts,
      batchSubmission, requestBatch, requestBatchState };
    return;
  }

  if (document.getElementById(PANEL_ID)) return;
  const host = document.createElement('div');
  host.id = PANEL_ID;
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; position: fixed; right: 20px; bottom: 50px; z-index: 2147483647; color-scheme: dark; }
      * { box-sizing: border-box; }
      section { width: min(278px, calc(100vw - 40px)); padding: 15px; max-height: calc(100vh - 74px); max-height: calc(100dvh - 74px); overflow-y: auto; border: 1px solid #ffffff30; border-radius: 12px; background: #14151bf5; box-shadow: 0 12px 40px #0006; color: #fff; font: 13px/1.55 system-ui, sans-serif; }
      header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 9px; }
      .header-actions { display: flex; align-items: center; gap: 8px; }
      strong { font-size: 12px; letter-spacing: .06em; color: #d1d2d8; }
      a { color: #c2c5cf; font-size: 11px; text-decoration: none; }
      a:hover { color: #fff; text-decoration: underline; }
      button { display: block; width: 100%; min-height: 40px; border: 0; border-radius: 7px; color: white; background: #fe2c55; font: 600 14px system-ui, sans-serif; cursor: pointer; }
      button:disabled { opacity: .55; cursor: default; }
      button:focus-visible, a:focus-visible { outline: 2px solid #fff; outline-offset: 3px; }
      #launcher { display: grid; place-items: center; width: 48px; height: 48px; min-height: 48px; padding: 0; border: 1px solid #ffffff30; border-radius: 50%; background: #191a20ed; color: #ff6685; box-shadow: 0 4px 16px #0004; }
      #launcher:hover { background: #292b34; color: #fff; }
      #launcher svg { width: 22px; height: 22px; }
      #collapse { width: 28px; height: 28px; min-height: 28px; padding: 0; background: transparent; color: #b3b8c8; font-size: 20px; line-height: 1; }
      #collapse:hover { background: #ffffff12; color: #fff; }
      #video { margin: 0 0 10px; color: #b3b8c8; font: 11px/1.5 ui-monospace, monospace; overflow-wrap: anywhere; }
      #status { margin: 9px 0 0; color: #c1c5d0; font-size: 12px; overflow-wrap: anywhere; }
      #status.error { color: #ffb2bd; }
      [hidden] { display: none !important; }
      #batch { border-top: 1px solid #ffffff26; margin-top: 13px; padding-top: 12px; }
      #batch p { margin: 8px 0; overflow-wrap: anywhere; }
      #batch-status { font-size: 12px; color: #c1c5d0; }
      .secondary { background: #343742; font-size: 12px; min-height: 34px; }
      .controls { display: flex; gap: 7px; margin-top: 7px; }
      .hint { color: #969eaf; font-size: 11px; }
    </style>
    <button type="button" id="launcher" aria-label="打开下载与总结" title="打开下载与总结" aria-controls="panel" aria-expanded="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M5 16v4h14v-4"/></svg>
    </button>
    <section id="panel" aria-label="抖音本地下载与总结" hidden>
      <header><strong>本地内容工作台</strong><div class="header-actions"><a href="${BASE}/app/#task-desk" target="_blank" rel="noopener noreferrer">工作台 ↗</a><button type="button" id="collapse" aria-label="收起面板" title="收起面板（Esc）">×</button></div></header>
      <p id="video"></p>
      <button type="button" id="submit">下载并总结</button>
      <p id="status" role="status" aria-live="polite">点击后下载，并调用已配置的总结模型。</p>
      <div id="batch" hidden>
        <strong>主页批量 · 本地后台排队</strong>
        <p id="batch-status" role="status" aria-live="polite">仅收集当前博主“作品”中的视频，图文会跳过。</p>
        <button type="button" id="collect" class="secondary">收集主页全部视频</button>
        <div class="controls">
          <button type="button" id="batch-start" hidden>开始批量下载并总结</button>
          <button type="button" id="batch-pause" class="secondary" hidden>暂停</button>
          <button type="button" id="batch-stop" class="secondary" hidden>停止</button>
        </div>
        <p class="hint">全部链接一次提交；按看板显示的间隔逐条下载（默认 30 秒），本地已有视频会跳过，总结独立运行。提交成功后可关闭网页，本地服务和电脑需保持运行。</p>
      </div>
    </section>`;
  document.body.append(host);
  const launcher = shadow.getElementById('launcher');
  const panel = shadow.getElementById('panel');
  const collapse = shadow.getElementById('collapse');
  function setExpanded(expanded) {
    panel.hidden = !expanded;
    launcher.hidden = expanded;
    launcher.setAttribute('aria-expanded', String(expanded));
    (expanded ? collapse : launcher).focus();
  }
  launcher.addEventListener('click', () => setExpanded(true));
  collapse.addEventListener('click', () => setExpanded(false));
  addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) {
      event.preventDefault();
      event.stopPropagation();
      setExpanded(false);
    }
  }, true);
  const button = shadow.getElementById('submit');
  const videoLabel = shadow.getElementById('video');
  const status = shadow.getElementById('status');
  const submitted = new Map();
  let displayedId = '';
  let busy = false;
  let collecting = false;
  let collection = null;
  let scanToken = 0;
  let scanMessage = '';
  const batchPanel = shadow.getElementById('batch');
  const batchStatus = shadow.getElementById('batch-status');
  const collectButton = shadow.getElementById('collect');
  const startButton = shadow.getElementById('batch-start');
  const pauseButton = shadow.getElementById('batch-pause');
  const stopButton = shadow.getElementById('batch-stop');
  let batch = null;
  let pendingSubmission = null;
  let batchBusy = false;
  let polling = false;
  let batchMessage = '';
  let lastPoll = 0;
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
    if (saved?.version === 2 && /^[\w-]{1,100}$/.test(saved.batch_id || '')) {
      batch = { batch_id: saved.batch_id, author: saved.author || '当前博主', status: 'restoring' };
    } else if (saved?.version === 2 && saved.request) {
      pendingSubmission = batchSubmission(saved.request.video_ids,
        { author: saved.request.author, profileId: saved.request.profile_id }, saved.request.client_request_id);
      batchMessage = '上次提交结果尚未确认；点击“确认上次提交”会使用同一请求编号，不会重复创建批次。';
    } else if (saved?.ids?.length) {
      scanMessage = '旧版待提交队列未自动迁移。请先在看板核对已提交任务，再重新收集；不会自动补发。';
    }
  } catch { batchMessage = '无法恢复批次记录，请先到工作台确认已有任务。'; }

  function saveBatch() {
    try {
      const saved = batch ? { version: 2, batch_id: batch.batch_id, author: batch.author }
        : pendingSubmission ? { version: 2, request: pendingSubmission } : null;
      if (saved) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch { /* 页面存储失败不影响已保存到本地后台的批次。 */ }
  }

  function hasBatch() { return !!batch && !['completed', 'cancelled', 'stopped'].includes(batch.status); }

  async function refreshBatch(action) {
    if (!batch || polling || batchBusy) return;
    polling = true;
    batchBusy = !!action;
    lastPoll = Date.now();
    renderBatch();
    try {
      const result = await requestBatchState(batch.batch_id, GM_xmlhttpRequest, action);
      batch = result.data;
      batchMessage = '';
      saveBatch();
    } catch (error) {
      batchMessage = `${error.message}；仅状态同步失败，不代表后台任务已停止`;
    } finally {
      polling = false;
      batchBusy = false;
      render(); renderBatch();
    }
  }

  function renderBatch() {
    const onProfile = !!profileIdFromUrl(location.href);
    batchPanel.hidden = !onProfile && !collecting && !batch && !pendingSubmission;
    collectButton.disabled = collecting || hasBatch() || !!pendingSubmission || batchBusy || busy || !onProfile;
    startButton.hidden = collecting || hasBatch() || (!pendingSubmission && !collection?.ids.size);
    startButton.disabled = batchBusy;
    startButton.textContent = batchBusy ? '正在一次提交全部链接…' : pendingSubmission ? '确认上次提交' : `下载并总结这 ${collection?.ids.size || 0} 条`;
    pauseButton.hidden = !hasBatch() || batch.status === 'restoring';
    pauseButton.disabled = batchBusy || polling;
    pauseButton.textContent = batch?.status === 'paused' ? '继续' : '暂停';
    stopButton.hidden = !collecting && !hasBatch();
    stopButton.disabled = batchBusy || polling;
    if (batch) {
      const wait = Math.max(0, Math.ceil(Number(batch.next_allowed_at || 0) - Date.now() / 1000));
      const labels = { queued: '等待后台下载', running: batch.current_aweme_id ? '正在下载一条' : '后台排队中',
        paused: '后台已暂停，继续会重试失败项', completed: '本批视频下载已完成，总结独立处理',
        cancelled: '已停止后续下载，当前在途任务继续', stopped: '已停止后续下载', restoring: '正在恢复后台状态…' };
      const next = hasBatch() && batch.status !== 'paused' && wait > 0
        ? `；下一条至少 ${Math.floor(wait / 60)}:${String(wait % 60).padStart(2, '0')} 后开始` : '';
      const counts = Number.isInteger(batch.total)
        ? ` · 完成 ${batch.completed}/${batch.total}，失败 ${batch.failed}，待处理 ${batch.pending}，已停止 ${batch.cancelled}` : '';
      const detail = typeof batch.error === 'string' ? batch.error
        : [batch.error?.reason || batch.error?.message, batch.error?.action].filter(Boolean).join('；');
      batchStatus.textContent = `${batch.author || '当前博主'}${counts}；${labels[batch.status] || batch.status}${next}${detail ? `。${detail}` : ''}${batchMessage ? `。${batchMessage}` : ''}`;
    } else batchStatus.textContent = batchMessage || scanMessage || '仅收集当前博主“作品”中的视频，图文会跳过。';
  }

  function scrollContainer(list) {
    for (let el = list.parentElement; el; el = el.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight) return el;
    }
    return document.scrollingElement;
  }

  collectButton.addEventListener('click', async () => {
    if (collecting || hasBatch() || pendingSubmission || batchBusy || busy) return;
    const token = ++scanToken;
    collecting = true;
    collection = null;
    batch = null;
    batchMessage = '';
    saveBatch();
    scanMessage = '正在检查主页作品列表…';
    render(); renderBatch();
    try {
      const first = readProfilePosts(document, location.href);
      collection = { profileId: first.profileId, author: first.author, ids: new Set(), notes: new Set(), complete: false };
      const scroller = scrollContainer(first.list);
      scroller.scrollTop = 0;
      await new Promise(resolve => setTimeout(resolve, 3000)); // 等待虚拟列表切回顶部，不能读取旧页尾。
      let idle = 0;
      let previousSize = -1;
      const deadline = Date.now() + 30 * 60 * 1000;
      // ponytail: 只读取页面实际加载的作品；遇到登录/列表改版就停止，不另造抓取接口。
      while (token === scanToken && collecting) {
        const page = readProfilePosts(document, location.href);
        if (page.profileId !== collection.profileId) throw new Error('已离开原博主主页，收集已停止');
        page.ids.forEach(id => collection.ids.add(id));
        page.notes.forEach(id => collection.notes.add(id));
        const size = collection.ids.size + collection.notes.size;
        if (!scroller.isConnected) throw new Error('页面作品列表已变化，请重新收集');
        const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4;
        collection.complete = (atBottom && page.ended) || (page.expected > 0 && size >= page.expected);
        scanMessage = `${collection.author} · 已收集 ${collection.ids.size} 条视频，跳过 ${collection.notes.size} 条图文；正在低速滚动…`;
        renderBatch();
        if (collection.complete) break;
        idle = atBottom && size === previousSize ? idle + 1 : 0;
        if (idle >= 8 || Date.now() >= deadline) break; // 停滞 24 秒或超过 30 分钟均按部分收集处理。
        previousSize = size;
        scroller.scrollBy({ top: Math.max(300, scroller.clientHeight * .8), behavior: 'instant' });
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
      if (token !== scanToken) return;
      scanMessage = `${collection.author} · ${collection.complete ? '收集完成' : '仅收集到当前可见范围，未确认全部'}：${collection.ids.size} 条视频，跳过 ${collection.notes.size} 条图文。请核对主页数量；搜索/日期筛选会影响范围。`;
    } catch (error) {
      if (token === scanToken) {
        scanMessage = `${error.message}${collection?.ids.size ? `；已保留 ${collection.ids.size} 条，未确认全部` : ''}`;
      }
    } finally {
      if (token === scanToken) { collecting = false; render(); renderBatch(); }
    }
  });

  startButton.addEventListener('click', async () => {
    if (collecting || hasBatch() || batchBusy || busy) return;
    if (!pendingSubmission) {
      if (!collection?.ids.size) return;
      if (profileIdFromUrl(location.href) !== collection.profileId) {
        scanMessage = '主页已变化，请重新收集当前博主的作品'; renderBatch(); return;
      }
      const ids = [...collection.ids].filter(id => !submitted.has(id));
      if (!ids.length) { scanMessage = '这些视频已在当前页提交，无需重复提交'; renderBatch(); return; }
      const scope = collection.complete ? '已收集' : '尚未确认收集全部，本次仅提交已收集的';
      if (!confirm(`${collection.author}：${scope} ${ids.length} 条视频。\n全部链接一次交给本地后台，按看板显示的间隔逐条下载（默认 30 秒）；本地已有视频会自动跳过。\n每条下载后单独总结，会调用已配置模型。提交成功后可关闭网页，本地服务和电脑需保持运行。\n遇到下载错误后台会暂停，下载间隔不能保证避免风控。\n确认开始？`)) return;
      try { pendingSubmission = batchSubmission(ids, collection, crypto.randomUUID()); }
      catch (error) { batchMessage = error.message; renderBatch(); return; }
    }
    // 先保存请求编号；超时或刷新后只能确认同一批次，不能悄悄换编号重建。
    saveBatch();
    batchBusy = true;
    batchMessage = '正在一次提交全部视频链接…';
    render(); renderBatch();
    try {
      const result = await requestBatch(pendingSubmission, GM_xmlhttpRequest);
      batch = result.data;
      pendingSubmission.video_ids.forEach(id => submitted.set(id, '已交给本地后台批次，下载完成后自动总结。'));
      pendingSubmission = null;
      collection = null;
      batchMessage = '全部链接已保存到本地后台，可关闭此网页；下载和逐条总结会继续。';
      saveBatch();
    } catch (error) {
      batchMessage = `${error.message}；请点击“确认上次提交”，会沿用同一请求编号，不会重复创建批次`;
    } finally { batchBusy = false; render(); renderBatch(); }
  });
  pauseButton.addEventListener('click', () => {
    refreshBatch(batch?.status === 'paused' ? 'resume' : 'pause');
  });
  stopButton.addEventListener('click', () => {
    if (collecting) {
      scanToken += 1; collecting = false;
      scanMessage = `已停止收集，保留 ${collection?.ids.size || 0} 条；未确认全部`;
    } else if (batch && confirm('停止本批后续下载？正在下载的当前视频和已创建的总结仍会继续。')) refreshBatch('stop');
    render(); renderBatch();
  });
  addEventListener('beforeunload', event => {
    if (collecting || batchBusy || busy) { event.preventDefault(); event.returnValue = ''; }
  });

  function readCurrent() {
    return currentVideoId(document, location.href, { width: innerWidth, height: innerHeight });
  }

  function render(message, error = false) {
    const id = readCurrent();
    const switched = id !== displayedId;
    displayedId = id;
    videoLabel.textContent = id ? `当前视频：${id}` : '请先打开或播放一条抖音视频';
    button.disabled = busy || collecting || batchBusy || !!pendingSubmission || hasBatch() || !id || submitted.has(id);
    button.textContent = busy ? '正在提交…' : submitted.has(id) ? '已提交到工作台' : '下载并总结';
    if (message !== undefined || switched) {
      status.textContent = message ?? (submitted.get(id) || '点击后下载，并调用已配置的总结模型。');
      status.className = error ? 'error' : '';
    }
  }

  button.addEventListener('click', async () => {
    const id = readCurrent(); // 点击瞬间重新识别，避免滚动后提交上一条视频。
    if (!id || busy || collecting || batchBusy || pendingSubmission || hasBatch() || submitted.has(id)) return;
    busy = true;
    render('正在连接本地工作台…');
    try {
      const result = await requestDownload(id, GM_xmlhttpRequest);
      const message = result.data.status === 'completed'
        ? '视频已在本机，已请求复用或生成总结。'
        : '已加入后台队列，下载完成后自动总结；可继续刷视频或关闭网页。';
      submitted.set(id, message);
      busy = false;
      render(readCurrent() === id ? message : undefined);
    } catch (error) {
      busy = false;
      render(readCurrent() === id ? error.message : undefined, readCurrent() === id);
    }
  });

  render();
  renderBatch();
  if (batch) refreshBatch(); // 只恢复 GET 状态查询，刷新页面绝不自动重发 POST。
  setInterval(() => {
    render(); renderBatch();
    if (hasBatch() && !document.hidden && Date.now() - lastPoll >= 10000) refreshBatch();
  }, 700);
})();
