"""本地网页面板：在浏览器里看状态、控制播放。

控制台窗口容易被挡住或者干脆没打开（尤其后台运行时），所以这里起一个只监听
127.0.0.1 的小 HTTP 服务，页面自动刷新显示当前播放、队列、连接状态和日志，
也能直接点按钮切歌 / 暂停 / 调音量。

只用 Python 标准库，不引入任何新依赖。
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from common import DEFAULT_CONFIG, VERSION, load_config, recent_logs, save_config
from lyric_overlay import PAGE as OVERLAY_PAGE
from netease_api import Song

LogFunc = Callable[[str], None]

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B站弹幕点歌 · 控制台</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --fg:#e6e9ef; --dim:#8b93a7; --accent:#4c8dff; --ok:#3ecf8e; --warn:#f5a623; --err:#ff5c5c;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:18px}
  header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px}
  h1{font-size:17px;margin:0;font-weight:600}
  /* 版本号：不能直接写进 PAGE，否则每次改版本都得动这一大段 HTML */
  .ver{color:var(--dim);font-size:11px;font-weight:400;margin-left:6px;
       border:1px solid var(--line);border-radius:999px;padding:1px 7px;vertical-align:middle}
  .pills{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}
  .pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
        padding:3px 11px;font-size:12px;color:var(--dim)}
  .pill b{color:var(--fg);font-weight:600}
  .pill.on b{color:var(--ok)} .pill.off b{color:var(--err)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:16px;margin-bottom:14px}
  .now .title{font-size:19px;font-weight:600;margin-bottom:2px}
  .now .sub{color:var(--dim);font-size:13px}
  .bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin:12px 0 6px}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,#4c8dff,#7aa9ff);width:0}
  .times{display:flex;justify-content:space-between;color:var(--dim);font-size:12px}
  .empty{color:var(--dim);padding:8px 0}
  .ctrls{display:flex;flex-wrap:wrap;gap:9px;align-items:center}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:8px;padding:8px 15px;font-size:13px;cursor:pointer;font-family:inherit}
  button:hover{background:#262b36;border-color:#3a4150}
  button:active{transform:translateY(1px)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.stop{background:#3a2020;border-color:#5c2e2e;color:#ff9a9a}
  button.stop:hover{background:#4a2626;border-color:var(--err);color:#fff}
  button.danger:hover{border-color:var(--err);color:var(--err)}
  .vol{display:flex;align-items:center;gap:9px;margin-left:auto}
  input[type=range]{width:130px;accent-color:var(--accent)}
  .toggle{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px;
          cursor:pointer;user-select:none;white-space:nowrap}
  .toggle input{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
  .toggle:hover{color:var(--fg)}
  .cols{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  @media(max-width:1080px){.cols{grid-template-columns:1fr 1fr}}
  @media(max-width:820px){.cols{grid-template-columns:1fr}}
  h2{font-size:13px;color:var(--dim);font-weight:600;margin:0 0 10px;
     text-transform:uppercase;letter-spacing:.06em}
  ol.q{list-style:none;margin:0;padding:0;max-height:320px;overflow:auto}
  ol.q li{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}
  ol.q li:last-child{border-bottom:none}
  ol.q .idx{color:var(--dim);min-width:18px}
  ol.q .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  ol.q .by{color:var(--dim);font-size:12px;white-space:nowrap}
  #danmaku{max-height:320px;overflow:auto;font-size:13px}
  #danmaku div{padding:4px 0;border-bottom:1px solid #21252e;word-break:break-all}
  #danmaku .t{color:#5d6478;font-size:11px;margin-right:6px}
  #danmaku .u{color:var(--accent)}
  #danmaku div.hit{color:var(--ok)}
  #danmaku div.hit .u{color:var(--ok)}
  #danmaku div.hit::before{content:"★ "}
  .req{display:flex;gap:9px;align-items:center}
  .req input{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);
             border-radius:8px;padding:9px 12px;color:var(--fg);font-size:14px;font-family:inherit}
  .req input:focus{outline:none;border-color:var(--accent)}
  #results{margin-top:10px}
  #results .empty{padding:4px 0}
  .r{display:flex;gap:10px;align-items:center;padding:7px 0;
     border-bottom:1px solid var(--line);font-size:13px}
  .r:last-child{border-bottom:none}
  .r .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .r .meta{color:var(--dim);font-size:12px;white-space:nowrap}
  .r button{padding:5px 12px;font-size:12px;flex:none}
  .vip{color:var(--warn);font-size:11px;border:1px solid var(--warn);
       border-radius:4px;padding:0 4px;margin-left:6px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:var(--ok);margin-right:5px;animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
  #logs{max-height:320px;overflow:auto;font:12px/1.65 ui-monospace,Consolas,monospace}
  #logs div{padding:2px 0;border-bottom:1px solid #21252e;word-break:break-all}
  #logs .t{color:#5d6478;margin-right:7px}
  #logs .WARNING{color:var(--warn)} #logs .ERROR{color:var(--err)}
  .hint{color:var(--dim);font-size:12px;margin-top:10px}
  .hint code{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
             padding:1px 5px;font-size:11px;color:#a9b6cc}
  /* 歌词区 */
  .nowlyric{max-height:340px;overflow:auto;text-align:center;padding:6px 0}
  .nowlyric .sl{font-size:15px;line-height:1.9;color:var(--dim);padding:1px 0;
                transition:color .25s,font-size .25s}
  .nowlyric .sl.on{color:var(--accent);font-size:17px;font-weight:600}
  .nowlyric .sl .tr{display:block;font-size:12px;color:#7b8496}
  .nowlyric .sl.on .tr{color:#9fc0ff}
  /* 样式设置区 */
  details.sty{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
  details.sty>summary{cursor:pointer;color:var(--dim);font-size:13px;user-select:none}
  details.sty>summary:hover{color:var(--fg)}
  .grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:12px}
  @media(max-width:820px){.grid{grid-template-columns:1fr}}
  .f{display:flex;align-items:center;gap:9px;font-size:13px}
  .f>span{color:var(--dim);min-width:96px;font-size:12px}
  .f input[type=text],.f select{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);
    border-radius:7px;padding:6px 9px;color:var(--fg);font-size:13px;font-family:inherit}
  .f input[type=text]:focus,.f select:focus{outline:none;border-color:var(--accent)}
  .f input[type=color]{width:44px;height:28px;padding:0;border:1px solid var(--line);
    border-radius:6px;background:var(--panel2);cursor:pointer}
  .f input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);cursor:pointer}
  .f input[type=range]{flex:1;min-width:0;width:auto}
  #lyCss{width:100%;height:130px;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:10px;color:var(--fg);font:12px/1.6 ui-monospace,Consolas,monospace;
    resize:vertical}
  #lyCss:focus{outline:none;border-color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>B站弹幕点歌 · 控制台 <span class="ver">{{VERSION}}</span></h1>
    <div class="pills" id="pills"></div>
  </header>

  <div class="card now">
    <div id="nowTitle" class="title">等待点歌…</div>
    <div id="nowSub" class="sub">在直播间发「点歌 歌名」试试</div>
    <div class="bar"><i id="bar"></i></div>
    <div class="times"><span id="tCur">0:00</span><span id="tTotal">0:00</span></div>
  </div>

  <div class="card">
    <div class="ctrls">
      <button class="primary" onclick="cmd('skip')">切歌</button>
      <button id="btnPause" onclick="cmd('pause')">暂停</button>
      <button onclick="cmd('resume')">继续</button>
      <button class="stop" onclick="cmd('stop')">■ 停止播放</button>
      <button class="danger" onclick="cmd('clear')">清空队列</button>
      <label class="toggle" title="没人点歌时自动放随机歌；点「停止播放」会一并关掉">
        <input type="checkbox" id="autoPlay" onchange="cmd('autoplay', this.checked)"> 空闲随机播放
      </label>
      <div class="vol">
        <span style="color:var(--dim)">音量</span>
        <input type="range" id="vol" min="0" max="100" oninput="onVol(this.value)">
        <span id="volTxt" style="min-width:30px">-</span>
      </div>
    </div>
    <div class="hint" id="hint"></div>
  </div>

  <div class="card">
    <h2>点歌（不用发弹幕）</h2>
    <div class="req">
      <input id="q" type="text" placeholder="输入歌名，回车搜索，例如：晴天"
             onkeydown="if(event.key==='Enter'){doSearch()}">
      <button class="primary" onclick="doSearch()">搜索</button>
      <button onclick="quickAdd()">直接点第一首</button>
    </div>
    <div id="results"><div class="empty">搜到结果后，点「点这首」加入队列</div></div>
    <div class="hint" id="reqHint"></div>
  </div>

  <div class="card">
    <h2>歌词</h2>
    <div class="nowlyric" id="lyricBox"><div class="empty">还没有在放歌</div></div>
    <div class="hint" id="lyricHint"></div>
  </div>

  <div class="card">
    <h2>歌词叠加层（OBS 浏览器源）</h2>
    <div class="req">
      <input id="ovUrl" type="text" readonly onclick="this.select()">
      <button class="primary" onclick="copyOverlay()">复制地址</button>
      <button onclick="window.open(document.getElementById('ovUrl').value,'_blank')">预览</button>
    </div>
    <div class="hint">
      在 OBS 里添加「浏览器源」，把上面的地址粘进去，宽高设成画布大小，勾选「透明背景」，
      自定义 CSS 填 <code>body{background:transparent}</code>。首次加载后建议勾掉「关闭源时关闭浏览器」。
    </div>
    <details class="sty">
      <summary>歌词样式设置（改完点保存，叠加层会自动刷新）</summary>
      <div class="grid">
        <label class="f"><span>启用歌词</span><input type="checkbox" id="lyEnabled"></label>
        <label class="f"><span>显示方式</span>
          <select id="lyMode">
            <option value="scroll">滚动列表</option>
            <option value="focus">只显示当前句和上下句</option>
            <option value="single">只显示当前句</option>
          </select></label>
        <label class="f"><span>字体</span>
          <input type="text" id="lyFont" placeholder="留空=系统默认，例如 思源黑体, Microsoft YaHei"></label>
        <label class="f"><span>字号 <b id="lySizeTxt">44</b>px</span>
          <input type="range" id="lySize" min="12" max="160" step="1"></label>
        <label class="f"><span>行高 <b id="lyLhTxt">1.35</b></span>
          <input type="range" id="lyLh" min="0.9" max="3" step="0.05"></label>
        <label class="f"><span>当前句放大 <b id="lyScaleTxt">1.06</b></span>
          <input type="range" id="lyScale" min="1" max="2" step="0.01"></label>
        <label class="f"><span>普通文字颜色</span><input type="color" id="lyColor"></label>
        <label class="f"><span>当前句颜色</span><input type="color" id="lyActive"></label>
        <label class="f"><span>译文颜色</span><input type="color" id="lyTr"></label>
        <label class="f"><span>对齐</span>
          <select id="lyAlign">
            <option value="left">左对齐</option>
            <option value="center">居中</option>
            <option value="right">右对齐</option>
          </select></label>
        <label class="f"><span>位置</span>
          <select id="lyAnchor">
            <option value="bottom">靠下</option>
            <option value="center">居中</option>
            <option value="top">靠上</option>
          </select></label>
      </div>
      <div class="hint" style="margin:10px 0 6px">
        自定义 CSS（追加在内置样式之后，优先级最高。可写 <code>.ln.active{}</code>、<code>#lines{}</code> 等）
      </div>
      <textarea id="lyCss" spellcheck="false"
        placeholder="/* 例如&#10;.ln.active{ text-shadow:0 0 18px #7cc4ff, 0 2px 10px #000; }&#10;#stage{ padding-bottom:12vh; } */"></textarea>
      <div class="ctrls" style="margin-top:10px">
        <button class="primary" onclick="saveLyric()">保存歌词样式</button>
        <button onclick="resetLyric()">恢复默认</button>
      </div>
      <div class="hint" id="lySaveHint"></div>
    </details>
  </div>

  <div class="cols">
    <div class="card">
      <h2><span id="dmDot"></span>收到的弹幕 <span id="dmCount"></span></h2>
      <div id="danmaku"><div class="empty">还没收到弹幕</div></div>
    </div>
    <div class="card">
      <h2>队列 <span id="qCount"></span></h2>
      <ol class="q" id="queue"><li class="empty">队列是空的</li></ol>
    </div>
    <div class="card">
      <h2>最近日志</h2>
      <div id="logs"></div>
    </div>
  </div>
</div>

<script>
const fmt = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); };
let volTimer = null, dragging = false;

async function post(body){
  const resp = await fetch('/api/command', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  return await resp.json();
}

function setHint(t){ document.getElementById('hint').textContent = t || ''; }
function setReqHint(t){ document.getElementById('reqHint').textContent = t || ''; }

async function cmd(action, value){
  try {
    const r = await post({action, value});
    if (r && r.message) setHint(r.message);
    refresh();
  } catch(e){ setHint('操作失败：' + e); }
}

// ---------------------------------------------------------------- 点歌

let lastResults = [];

async function doSearch(){
  const q = document.getElementById('q').value.trim();
  const box = document.getElementById('results');
  if (!q){ setReqHint('先输入歌名'); return; }
  setReqHint('搜索中…');
  try {
    const r = await post({action:'search', value:q});
    if (!r.ok){ setReqHint(r.message); box.innerHTML = ''; lastResults = []; return; }
    lastResults = r.songs || [];
    setReqHint(r.message);
    box.innerHTML = lastResults.map((s,i) =>
      `<div class="r"><span class="nm" title="${esc(s.name)} - ${esc(s.artists)}">${esc(s.name)}` +
      `<span class="meta"> - ${esc(s.artists)}</span>${s.vip_only ? '<span class="vip">VIP</span>' : ''}</span>` +
      `<span class="meta">${esc(s.duration)}</span>` +
      `<button class="primary" onclick="addResult(${i})">点这首</button></div>`
    ).join('');
  } catch(e){ setReqHint('搜索失败：' + e); }
}

async function addResult(i){
  const song = lastResults[i];
  if (!song) return;
  setReqHint('正在加入队列…');
  try {
    const r = await post({action:'request', value:song});
    setReqHint(r.message);
    refresh();
  } catch(e){ setReqHint('点歌失败：' + e); }
}

async function quickAdd(){
  const q = document.getElementById('q').value.trim();
  if (!q){ setReqHint('先输入歌名'); return; }
  setReqHint('正在点歌…');
  try {
    const r = await post({action:'request', value:{keyword:q}});
    setReqHint(r.message);
    refresh();
  } catch(e){ setHint('点歌失败：' + e); }
}

function onVol(v){
  document.getElementById('volTxt').textContent = v;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => cmd('volume', Number(v)), 220);
}

function esc(t){ const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

async function refresh(){
  let s;
  try { s = await (await fetch('/api/status')).json(); }
  catch(e){
    document.getElementById('hint').textContent = '与机器人失去连接，它可能已经退出了。';
    return;
  }

  // 顶部状态
  const st = s.stats || {};
  const ago = st.seconds_since_danmaku;
  const dmText = st.danmaku_received
    ? (st.danmaku_received + ' 条' + (ago != null && ago < 120 ? '（' + Math.round(ago) + '秒前）' : ''))
    : (s.connected ? '还没收到' : '未连接');
  const pills = [
    ['直播间', s.room_id ? ('#' + s.room_id) : '未配置', s.connected],
    ['弹幕', dmText, st.danmaku_received ? true : (s.connected ? false : null)],
    ['网易云', s.netease_account || (s.netease_ok ? '已连接' : '未连接'), s.netease_ok],
    ['播放器', s.backend + (s.paused ? '（已暂停）' : ''), s.backend !== 'null'],
    ['已播放', (st.played || 0) + ' 首', null],
  ];
  const ap = s.autoplay || {};
  if (ap.enabled) pills.push(['随机播放', '开（' + (ap.source || '') + '）', true]);
  document.getElementById('pills').innerHTML = pills.map(([k,v,ok]) =>
    `<span class="pill ${ok===null?'':(ok?'on':'off')}">${k} <b>${esc(v)}</b></span>`).join('');

  // 随机播放开关
  const apBox = document.getElementById('autoPlay');
  if (apBox && document.activeElement !== apBox) apBox.checked = !!ap.enabled;

  // 当前播放
  const np = s.now_playing;
  const title = document.getElementById('nowTitle');
  const sub = document.getElementById('nowSub');
  const bar = document.getElementById('bar');
  if (np){
    title.textContent = np.name;
    const by = np.requester === '自动播放' ? '空闲随机播放' : ('由 ' + np.requester + ' 点播');
    sub.textContent = np.artists + (np.quality ? ' · ' + np.quality : '') + ' · ' + by;
    const el = s.now_elapsed || 0, total = np.duration_sec || 0;
    document.getElementById('tCur').textContent = fmt(el);
    document.getElementById('tTotal').textContent = fmt(total);
    bar.style.width = total > 0 ? Math.min(100, el/total*100) + '%' : '0';
  } else {
    title.textContent = '等待点歌…';
    sub.textContent = s.connected ? '在直播间发「点歌 歌名」试试' : '还没连上直播间';
    document.getElementById('tCur').textContent = '0:00';
    document.getElementById('tTotal').textContent = '0:00';
    bar.style.width = '0';
  }

  // 音量（正在拖动时不要覆盖用户操作）
  const vol = document.getElementById('vol');
  if (!dragging && document.activeElement !== vol){
    vol.value = s.volume;
    document.getElementById('volTxt').textContent = s.volume;
  }
  document.getElementById('btnPause').textContent = s.paused ? '暂停中' : '暂停';

  // 弹幕
  const dm = document.getElementById('danmaku');
  const list = s.danmaku || [];
  document.getElementById('dmCount').textContent =
    st.danmaku_received ? '(' + st.danmaku_received + ' 条，点歌 ' + (st.danmaku_matched || 0) + ' 条)' : '';
  document.getElementById('dmDot').innerHTML = s.connected ? '<span class="dot"></span>' : '';
  const dmAtBottom = dm.scrollTop + dm.clientHeight >= dm.scrollHeight - 20;
  if (!list.length){
    dm.innerHTML = s.connected
      ? '<div class="empty">已连上直播间，还没收到弹幕。<br>房间没开播时是没有弹幕的。</div>'
      : '<div class="empty">还没连上直播间</div>';
  } else {
    dm.innerHTML = list.map(it =>
      `<div class="${it.matched ? 'hit' : ''}"><span class="t">${esc(it.time)}</span>` +
      `<span class="u">${esc(it.uname)}</span>：${esc(it.text)}</div>`).join('');
    if (dmAtBottom) dm.scrollTop = dm.scrollHeight;
  }

  // 队列
  const q = document.getElementById('queue');
  document.getElementById('qCount').textContent = s.queue.length ? '(' + s.queue.length + ')' : '';
  q.innerHTML = s.queue.length
    ? s.queue.map((it,i) => `<li><span class="idx">${i+1}</span>
        <span class="nm" title="${esc(it.name)} - ${esc(it.artists)}">${esc(it.name)} - ${esc(it.artists)}</span>
        <span class="by">${esc(it.duration)} · ${esc(it.requester)}</span></li>`).join('')
    : '<li class="empty">队列是空的</li>';

  // 日志
  const lg = document.getElementById('logs');
  const atBottom = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 20;
  lg.innerHTML = s.logs.map(l =>
    `<div><span class="t">${esc(l.time)}</span><span class="${esc(l.level)}">${esc(l.msg)}</span></div>`).join('');
  if (atBottom) lg.scrollTop = lg.scrollHeight;

  renderLyric(s);
}

// ---------------------------------------------------------------- 歌词

let lyricSig = '';          // 歌词内容指纹，变了才重绘
let lyricActive = -1;
let lyricCfgLoaded = false;

function renderLyric(s){
  const box = document.getElementById('lyricBox');
  const hint = document.getElementById('lyricHint');
  const ly = s.lyric || {};

  if (!ly.enabled){
    box.innerHTML = '<div class="empty">歌词功能已关闭</div>';
    hint.textContent = '在下面的「歌词样式设置」里可以重新打开';
    return;
  }
  if (!s.now_playing){
    box.innerHTML = '<div class="empty">还没有在放歌</div>';
    hint.textContent = '';
    lyricSig = ''; lyricActive = -1;
    return;
  }
  if (ly.fetching){
    box.innerHTML = '<div class="empty">歌词加载中…</div>';
    hint.textContent = '';
    return;
  }
  const lines = ly.lines || [];
  if (!lines.length){
    box.innerHTML = '<div class="empty">这首歌没有歌词</div>';
    hint.textContent = ly.error ? ('取歌词失败：' + ly.error) : '';
    lyricSig = ''; lyricActive = -1;
    return;
  }

  hint.textContent = `${ly.count} 行` + (ly.has_translation ? ' · 双语' : '')
    + (ly.synced ? ' · 已同步' : ' · 无时间戳');

  // 歌词内容没变就不重建 DOM，免得每秒钟闪一下
  const sig = ly.song_id + ':' + ly.count;
  if (sig !== lyricSig){
    lyricSig = sig;
    lyricActive = -1;
    box.innerHTML = lines.map(ln =>
      `<div class="sl">${esc(ln.text)}` +
      (ln.translation ? `<span class="tr">${esc(ln.translation)}</span>` : '') + '</div>').join('');
    box.scrollTop = 0;
  }

  // 逐句高亮
  const nodes = box.children;
  let idx = -1;
  if (ly.synced){
    const el = s.now_elapsed || 0;
    for (let i = 0; i < lines.length; i++){
      if (lines[i].time !== null && lines[i].time <= el) idx = i; else break;
    }
  }
  if (idx !== lyricActive){
    for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle('on', i === idx);
    lyricActive = idx;
    scrollLyricBox(box, nodes[idx]);
  }
}

/** 把当前句滚到歌词框中间。
 *
 * 这里只能动歌词框自己的 scrollTop，绝不能用 scrollIntoView ——
 * 它会连整个页面一起滚，于是每唱一句页面就被拉回歌词卡片，
 * 用户想看队列、日志、或者改样式都会被一直弹走。
 */
function scrollLyricBox(box, cur){
  if (!box || !cur) return;
  const target = cur.offsetTop - (box.clientHeight - cur.offsetHeight) / 2;
  const max = Math.max(0, box.scrollHeight - box.clientHeight);
  const top = Math.max(0, Math.min(max, target));
  if (box.scrollTo) box.scrollTo({top, behavior:'smooth'});
  else box.scrollTop = top;
}

function setLySaveHint(t){ document.getElementById('lySaveHint').textContent = t || ''; }

function overlayUrl(){
  return location.origin + '/overlay';
}

function copyOverlay(){
  const input = document.getElementById('ovUrl');
  input.select();
  const done = () => setLySaveHint('地址已复制，去 OBS 添加浏览器源吧');
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(input.value).then(done, () => { document.execCommand('copy'); done(); });
  } else {
    document.execCommand('copy'); done();
  }
}

function fillLyricForm(cfg){
  lyCfg = cfg || {};
  document.getElementById('lyEnabled').checked = cfg.enabled !== false;
  document.getElementById('lyMode').value = cfg.mode || 'scroll';
  document.getElementById('lyFont').value = cfg.font_family || '';
  const size = Number(cfg.font_size || 44);
  document.getElementById('lySize').value = size;
  document.getElementById('lySizeTxt').textContent = size;
  const lh = Number(cfg.line_height || 1.35);
  document.getElementById('lyLh').value = lh;
  document.getElementById('lyLhTxt').textContent = lh;
  const sc = Number(cfg.active_scale || 1.06);
  document.getElementById('lyScale').value = sc;
  document.getElementById('lyScaleTxt').textContent = sc;
  document.getElementById('lyColor').value = cfg.color || '#ffffff';
  document.getElementById('lyActive').value = cfg.active_color || '#7cc4ff';
  document.getElementById('lyTr').value = cfg.translation_color || '#c9d4e6';
  document.getElementById('lyAlign').value = cfg.align || 'center';
  document.getElementById('lyAnchor').value = cfg.anchor || 'bottom';
  document.getElementById('lyCss').value = cfg.css || '';
}

let lyCfg = {};

function collectLyricForm(){
  return {
    enabled: document.getElementById('lyEnabled').checked,
    mode: document.getElementById('lyMode').value,
    font_family: document.getElementById('lyFont').value.trim(),
    font_size: Number(document.getElementById('lySize').value),
    line_height: Number(document.getElementById('lyLh').value),
    active_scale: Number(document.getElementById('lyScale').value),
    color: document.getElementById('lyColor').value,
    active_color: document.getElementById('lyActive').value,
    translation_color: document.getElementById('lyTr').value,
    align: document.getElementById('lyAlign').value,
    anchor: document.getElementById('lyAnchor').value,
    css: document.getElementById('lyCss').value,
  };
}

async function saveLyric(){
  setLySaveHint('保存中…');
  try {
    const r = await (await fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(collectLyricForm())
    })).json();
    setLySaveHint(r.message || (r.ok ? '已保存' : '保存失败'));
    if (r.setting) fillLyricForm(r.setting);
  } catch(e){ setLySaveHint('保存失败：' + e); }
}

async function resetLyric(){
  if (!confirm('恢复成默认歌词样式？')) return;
  setLySaveHint('正在恢复…');
  try {
    const r = await (await fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enabled:true, mode:'scroll', font_family:'', font_size:44,
        line_height:1.35, active_scale:1.06, color:'#ffffff', active_color:'#7cc4ff',
        translation_color:'#c9d4e6', align:'center', anchor:'bottom', css:''})
    })).json();
    setLySaveHint(r.message || '已恢复默认');
    if (r.setting) fillLyricForm(r.setting);
  } catch(e){ setLySaveHint('恢复失败：' + e); }
}

// 滑块旁边的实时数字
[['lySize','lySizeTxt'],['lyLh','lyLhTxt'],['lyScale','lyScaleTxt']].forEach(([id, out]) => {
  const el = document.getElementById(id);
  el.addEventListener('input', () => {
    document.getElementById(out).textContent = el.value;
  });
});

document.getElementById('ovUrl').value = overlayUrl();

const volEl = document.getElementById('vol');
volEl.addEventListener('mousedown', () => dragging = true);
volEl.addEventListener('touchstart', () => dragging = true);
window.addEventListener('mouseup', () => dragging = false);
window.addEventListener('touchend', () => dragging = false);

// 首次拿一次歌词设置填进表单；之后不停覆盖会打断正在编辑的内容
fetch('/api/status').then(r => r.json()).then(s => {
  if (!lyricCfgLoaded){
    lyricCfgLoaded = true;
    fillLyricForm((s && s.lyric_settings) || {});
  }
}).catch(() => {});

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


def _port_usable(host: str, port: int) -> bool:
    """端口能不能绑。

    注意这里【不能】设 SO_REUSEADDR：Windows 上它允许绑定到已经被占用的端口，
    于是"探测"永远成功，冲突检测就失效了（Unix 上行为相反）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def _song_from_payload(payload: dict[str, Any]) -> Song | None:
    """把前端回传的歌曲信息还原成 Song。信息不全就返回 None。"""
    try:
        song_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        return None
    name = str(payload.get("name") or "").strip()
    if song_id <= 0 or not name:
        return None

    def _int(key: str) -> int:
        try:
            return int(payload.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return Song(
        id=song_id,
        name=name,
        artists=str(payload.get("artists") or "未知歌手"),
        duration_ms=_int("duration_ms"),
        fee=_int("fee"),
    )


def _song_to_json(song: Song) -> dict[str, Any]:
    return {
        "id": song.id,
        "name": song.name,
        "artists": song.artists,
        "duration": song.duration_text,
        "duration_ms": song.duration_ms,
        "fee": song.fee,
        "vip_only": song.is_vip_only,
    }


# 颜色只允许十六进制或 rgb/hsl 函数写法，避免有人往 config.json 里塞奇怪的东西
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^(?:rgb|rgba|hsl|hsla)\([0-9.,%\s/]+\)$|^[a-zA-Z]{3,20}$")
_FONT_RE = re.compile(r"^[\w\s,\-'\"\u4e00-\u9fff]+$")
_ALIGN_VALUES = {"left", "center", "right"}
_ANCHOR_VALUES = {"top", "center", "bottom"}
_MODE_VALUES = {"scroll", "focus", "single"}
# 自定义 CSS 的长度上限，防止一不小心贴进来几兆
_CSS_MAX = 20000


def _opt_color(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text and _COLOR_RE.match(text) else fallback


def _opt_font(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if len(text) <= 200 and _FONT_RE.match(text) else fallback


def _opt_number(value: Any, fallback: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return max(low, min(high, number))


def _opt_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def sanitize_lyric_settings(payload: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把面板/接口传来的歌词设置洗一遍，返回可以安全写进 config.json 的值。

    只认白名单字段；越界或格式不对的值一律退回默认，绝不原样写盘。
    """
    defaults = DEFAULT_CONFIG["lyric"]
    merged: dict[str, Any] = dict(defaults)
    if base:
        merged.update(base)

    if "enabled" in payload:
        merged["enabled"] = bool(payload.get("enabled"))
    if "font_family" in payload:
        merged["font_family"] = _opt_font(payload.get("font_family"), str(defaults["font_family"]))
    if "font_size" in payload:
        merged["font_size"] = round(_opt_number(payload.get("font_size"), defaults["font_size"], 12, 300))
    if "line_height" in payload:
        merged["line_height"] = round(_opt_number(payload.get("line_height"), defaults["line_height"], 0.8, 4), 2)
    if "color" in payload:
        merged["color"] = _opt_color(payload.get("color"), str(defaults["color"]))
    if "active_color" in payload:
        merged["active_color"] = _opt_color(payload.get("active_color"), str(defaults["active_color"]))
    if "translation_color" in payload:
        merged["translation_color"] = _opt_color(
            payload.get("translation_color"), str(defaults["translation_color"])
        )
    if "active_scale" in payload:
        merged["active_scale"] = round(_opt_number(payload.get("active_scale"), defaults["active_scale"], 1.0, 2.5), 2)
    if "align" in payload:
        merged["align"] = _opt_choice(payload.get("align"), _ALIGN_VALUES, str(defaults["align"]))
    if "anchor" in payload:
        merged["anchor"] = _opt_choice(payload.get("anchor"), _ANCHOR_VALUES, str(defaults["anchor"]))
    if "mode" in payload:
        merged["mode"] = _opt_choice(payload.get("mode"), _MODE_VALUES, "scroll")
    if "css" in payload:
        css = str(payload.get("css") or "")
        merged["css"] = css[:_CSS_MAX]
    # 只保留默认配置里定义过的键，别的一律丢掉
    return {key: merged[key] for key in defaults if key in merged}


class _PanelServer(ThreadingHTTPServer):
    """本地面板用的 HTTP 服务。

    http.server.HTTPServer 默认 allow_reuse_address = 1，在 Windows 上这会让
    "端口已被占用"也绑定成功，结果两个机器人实例抢同一个端口、请求乱窜。
    关掉它，让冲突老实报错，然后在 start() 里自动换下一个端口。
    """

    allow_reuse_address = False


class ControlPanel:
    """给 SongBot 用的本地网页面板。"""

    def __init__(self, bot: Any, host: str = "127.0.0.1", port: int = 8765, log: LogFunc = print) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.log = log
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""
        # 改 config.json 时串行化，避免两个请求同时写坏文件
        self._config_lock = threading.RLock()

    # ------------------------------------------------------------------ 启动

    def start(self, open_browser: bool = True) -> str:
        """启动面板。端口被占用会自动往后找，全占满则返回空串。"""
        requested = self.port
        last_error = ""
        for offset in range(20):
            port = requested + offset
            if not _port_usable(self.host, port):
                continue
            try:
                self.httpd = _PanelServer((self.host, port), _make_handler(self))
            except OSError as exc:
                last_error = str(exc)
                continue
            self.port = port
            break
        else:
            self.log(f"网页面板启动失败（{requested} 起 20 个端口都不可用）：{last_error}")
            return ""

        self.url = f"http://{self.host}:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web-panel", daemon=True)
        self.thread.start()
        if self.port != requested:
            self.log(f"提示：默认端口 {requested} 被占用，改用 {self.port}")
        self.log(f"网页面板已启动：{self.url}")

        if open_browser:
            # 稍微等一下，确保服务真的在监听了再打开
            threading.Timer(0.8, self._open_browser).start()
        return self.url

    def _open_browser(self) -> None:
        try:
            webbrowser.open(self.url)
            self.log("已尝试打开浏览器；没弹出来的话手动访问上面的地址即可")
        except Exception as exc:  # noqa: BLE001 - 打不开浏览器不算错误
            self.log(f"自动打开浏览器失败（{exc}），手动访问 {self.url} 即可")

    def stop(self) -> None:
        if self.httpd is not None:
            with contextlib.suppress(Exception):
                self.httpd.shutdown()
            with contextlib.suppress(Exception):
                self.httpd.server_close()
            self.httpd = None

    # ------------------------------------------------------------ 数据与操作

    def status(self) -> dict[str, Any]:
        snapshot = self.bot.status_snapshot()
        snapshot["logs"] = recent_logs(120)
        # 面板上的歌词样式表单要用它初始化
        snapshot["lyric_settings"] = self.lyric_settings()
        snapshot["version"] = VERSION
        return snapshot

    # ------------------------------------------------------------ 歌词与叠加层

    def lyric_settings(self) -> dict[str, Any]:
        """当前生效的歌词设置（洗过的）。"""
        raw = (getattr(self.bot, "config", None) or {}).get("lyric") or {}
        return sanitize_lyric_settings({}, raw)

    def overlay_payload(self) -> dict[str, Any]:
        """叠加层要的全部数据，一次请求给全，避免每秒打多个接口。"""
        snapshot = self.bot.status_snapshot()
        return {
            "playing": snapshot.get("now_playing") is not None,
            "elapsed": snapshot.get("now_elapsed") or 0.0,
            "now_playing": snapshot.get("now_playing"),
            "lyric": snapshot.get("lyric") or {},
            "config": self.lyric_settings(),
            "version": VERSION,
        }

    def save_lyric_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把歌词设置写回 config.json，并让机器人立刻生效（不用重启）。"""
        with self._config_lock:
            current = load_config()
            merged = sanitize_lyric_settings(payload, current.get("lyric"))
            current["lyric"] = merged
            try:
                save_config(current)
            except OSError as exc:
                return {"ok": False, "message": f"写入 config.json 失败：{exc}"}

        bot = self.bot
        # 让运行中的机器人用上新设置
        if hasattr(bot, "config"):
            with contextlib.suppress(Exception):
                bot.config["lyric"] = merged
        if hasattr(bot, "lyric_enabled"):
            with contextlib.suppress(Exception):
                bot.lyric_enabled = bool(merged.get("enabled", True))
        return {"ok": True, "message": "歌词样式已保存并生效", "setting": merged}

    def do_action(self, action: str, value: Any) -> dict[str, Any]:
        """执行面板上的一次操作，返回给前端的结果。"""
        bot = self.bot
        if action == "skip":
            bot.player.skip()
            return {"ok": True, "message": "已切歌"}
        if action == "stop":
            cleared = bot.stop_playback()
            return {"ok": True, "message": f"已停止播放（顺带清掉 {cleared} 首待播）"}
        if action == "pause":
            ok = bot.player.pause()
            if ok:
                bot.paused = True
            return {"ok": ok, "message": "已暂停" if ok else "当前播放器不支持暂停"}
        if action == "resume":
            ok = bot.player.resume()
            if ok:
                bot.paused = False
            return {"ok": ok, "message": "已继续" if ok else "当前播放器不支持继续"}
        if action == "volume":
            try:
                bot.player.set_volume(int(value))
            except (TypeError, ValueError):
                return {"ok": False, "message": "音量值不合法"}
            return {"ok": True, "message": f"音量 {bot.player.volume}"}
        if action == "clear":
            count = bot.queue.clear()
            return {"ok": True, "message": f"已清空 {count} 首"}

        if action == "autoplay":
            bot.set_autoplay(bool(value))
            if bot.autoplay_enabled:
                return {"ok": True, "message": f"空闲随机播放已开启（来源：{bot.autoplay_source}）"}
            return {"ok": True, "message": "空闲随机播放已关闭"}

        # ---- 点歌：先搜索，拿到候选再让用户挑；也可以直接点第一首
        if action == "search":
            keyword = str(value or "").strip()
            if not keyword:
                return {"ok": False, "message": "先输入歌名"}
            songs, error = bot.search_songs(keyword, limit=8)
            if error:
                return {"ok": False, "message": f"搜索失败：{error}"}
            if not songs:
                return {"ok": False, "message": f"没找到《{keyword}》，换个关键词试试"}
            return {
                "ok": True,
                "message": f"找到 {len(songs)} 首，点右侧按钮加入队列",
                "songs": [_song_to_json(s) for s in songs],
            }

        if action == "request":
            payload = value if isinstance(value, dict) else {}
            song = _song_from_payload(payload)

            if song is None:
                # 没给完整歌曲信息，就用关键词去搜，按点歌指令同样的规则挑一首
                keyword = str(payload.get("keyword") or "").strip()
                if not keyword:
                    return {"ok": False, "message": "没给歌名"}
                songs, error = bot.search_songs(keyword, limit=5)
                if error:
                    return {"ok": False, "message": f"搜索失败：{error}"}
                if not songs:
                    return {"ok": False, "message": f"没找到《{keyword}》"}
                song = bot.pick_song(keyword, songs)

            ok, message = bot.request_song(song, requester="网页面板")
            if ok:
                message = f"已点《{song.name}》"
            return {"ok": ok, "message": message, "song": _song_to_json(song)}

        return {"ok": False, "message": f"未知操作：{action}"}


def _make_handler(panel: ControlPanel):
    """构造绑定了 panel 的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "qdgj-panel"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - 标准库要求的名字
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                # 版本号在这里替换：PAGE 里 CSS/JS 花括号太多，不能用 f-string
                self._send(200, PAGE.replace("{{VERSION}}", VERSION).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/overlay":
                # OBS 浏览器源用的透明歌词页
                self._send(200, OVERLAY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                try:
                    self._json(panel.status())
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, 500)
            elif path == "/api/overlay":
                # 叠加层轮询这个接口，出错也只回一个 error 字段，让页面自己显示
                try:
                    self._json(panel.overlay_payload())
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"状态读取失败：{exc}"}, 500)
            elif path == "/api/logs":
                self._json(recent_logs(200))
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/api/command", "/api/settings"):
                self._json({"error": "not found"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._json({"ok": False, "message": "请求体不是合法 JSON"}, 400)
                return

            if path == "/api/settings":
                try:
                    result = panel.save_lyric_settings(payload if isinstance(payload, dict) else {})
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "message": f"保存失败：{exc}"}, 500)
                    return
                self._json(result)
                panel.log(f"歌词样式已更新（{'成功' if result.get('ok') else '失败'}）")
                return

            action = str(payload.get("action", ""))
            try:
                self._json(panel.do_action(action, payload.get("value")))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "message": f"操作失败：{exc}"}, 500)

            panel.log(f"网页面板操作：{action}")

        def log_message(self, fmt: str, *args: Any) -> None:
            # 默认实现会把每个请求都打到 stderr，这里静音
            return

    return Handler


if __name__ == "__main__":
    # 单独跑一下看看页面长什么样（用一个假的 bot）
    class _Demo:
        paused = False

        class _P:
            volume = 70
            backend_name = "mpv"

            def skip(self): pass
            def pause(self): return True
            def resume(self): return True
            def set_volume(self, v): self.volume = v

        class _Q:
            def clear(self):
                return 0

        player = _P()
        queue = _Q()

        def stop_playback(self):
            return 0

        def status_snapshot(self):
            return {
                "connected": True, "room_id": 1934302095, "netease_ok": True,
                "netease_account": "演示账号", "backend": "mpv", "paused": False,
                "volume": 70, "now_playing": None, "now_elapsed": 0, "queue": [],
                "stats": {"played": 0}, "logs": [],
            }

    panel = ControlPanel(_Demo(), port=8765, log=print)
    url = panel.start(open_browser=False)
    print(f"演示面板：{url}   按 Ctrl+C 退出")
    with contextlib.suppress(KeyboardInterrupt):
        while True:
            time.sleep(1)
