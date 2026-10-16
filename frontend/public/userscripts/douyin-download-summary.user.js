// ==UserScript==
// @name         抖音 · 一键下载并总结到本地工作台
// @namespace    douyin-local-workbench
// @version      1.0.0
// @description  点击按钮，将正在观看的视频交给本机 8080 工作台下载并自动生成总结。
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
  const validId = value => /^\d{5,32}$/.test(value || '') ? value : '';

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

  function requestDownload(id, transport) {
    return new Promise((resolve, reject) => {
      transport({
        method: 'POST',
        url: `${BASE}/api/download`,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify({ url: `https://www.douyin.com/video/${id}`, auto_summary: true }),
        anonymous: true,
        timeout: 60000,
        onload(response) {
          let result;
          try { result = JSON.parse(response.responseText); }
          catch { reject(new Error('本地接口返回异常，请打开工作台检查')); return; }
          if (response.status < 200 || response.status >= 300 || !result.success) {
            const detail = result.detail;
            const reason = typeof detail === 'string' ? detail : detail?.message || result.message || `HTTP ${response.status}`;
            const action = detail?.action ? `；${detail.action}` : '';
            reject(new Error(`${reason}${action}`));
            return;
          }
          if (!result.data?.task_id) { reject(new Error('接口未返回任务编号，请到工作台确认')); return; }
          resolve(result);
        },
        onerror() { reject(new Error('连接不到本地工作台，请确认 127.0.0.1:8080 已启动，并允许油猴访问本机')); },
        ontimeout() { reject(new Error('提交超时，任务可能已创建，请先到工作台查看')); },
      });
    });
  }

  // 供本地无网络回归测试复用同一份识别与提交逻辑。
  if (typeof module === 'object' && module.exports) {
    module.exports = { idFromUrl, currentVideoId, requestDownload };
    return;
  }

  if (document.getElementById(PANEL_ID)) return;
  const host = document.createElement('div');
  host.id = PANEL_ID;
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host { all: initial; position: fixed; right: 20px; bottom: 86px; z-index: 2147483647; width: 238px; color-scheme: dark; }
      * { box-sizing: border-box; }
      section { padding: 15px; border: 1px solid #ffffff30; border-radius: 12px; background: #14151bf5; box-shadow: 0 12px 40px #0006; color: #fff; font: 13px/1.55 system-ui, sans-serif; }
      header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 9px; }
      strong { font-size: 12px; letter-spacing: .06em; color: #d1d2d8; }
      a { color: #c2c5cf; font-size: 11px; text-decoration: none; }
      a:hover { color: #fff; text-decoration: underline; }
      button { display: block; width: 100%; min-height: 40px; border: 0; border-radius: 7px; color: white; background: #fe2c55; font: 600 14px system-ui, sans-serif; cursor: pointer; }
      button:disabled { opacity: .55; cursor: default; }
      button:focus-visible, a:focus-visible { outline: 2px solid #fff; outline-offset: 3px; }
      #video { margin: 0 0 10px; color: #b3b8c8; font: 11px/1.5 ui-monospace, monospace; overflow-wrap: anywhere; }
      #status { margin: 9px 0 0; color: #c1c5d0; font-size: 12px; overflow-wrap: anywhere; }
      #status.error { color: #ffb2bd; }
    </style>
    <section aria-label="抖音本地下载与总结">
      <header><strong>本地内容工作台</strong><a href="${BASE}/app/#task-desk" target="_blank" rel="noopener noreferrer">打开工作台 ↗</a></header>
      <p id="video"></p>
      <button type="button" id="submit">下载并总结</button>
      <p id="status" role="status" aria-live="polite">点击后下载，并调用已配置的总结模型。</p>
    </section>`;
  document.body.append(host);
  const button = shadow.getElementById('submit');
  const videoLabel = shadow.getElementById('video');
  const status = shadow.getElementById('status');
  const submitted = new Map();
  let displayedId = '';
  let busy = false;

  function readCurrent() {
    return currentVideoId(document, location.href, { width: innerWidth, height: innerHeight });
  }

  function render(message, error = false) {
    const id = readCurrent();
    const switched = id !== displayedId;
    displayedId = id;
    videoLabel.textContent = id ? `当前视频：${id}` : '请先打开或播放一条抖音视频';
    button.disabled = busy || !id || submitted.has(id);
    button.textContent = busy ? '正在提交…' : submitted.has(id) ? '已提交到工作台' : '下载并总结';
    if (message !== undefined || switched) {
      status.textContent = message ?? (submitted.get(id) || '点击后下载，并调用已配置的总结模型。');
      status.className = error ? 'error' : '';
    }
  }

  button.addEventListener('click', async () => {
    const id = readCurrent(); // 点击瞬间重新识别，避免滚动后提交上一条视频。
    if (!id || busy || submitted.has(id)) return;
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
  setInterval(() => render(), 700);
})();
