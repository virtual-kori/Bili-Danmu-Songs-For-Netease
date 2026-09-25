"""OBS 歌词叠加层页面。

这个页面专门给 OBS 的「浏览器源」用：
    http://127.0.0.1:8765/overlay

设计要点：
    * 背景完全透明 —— html/body 都是 transparent，OBS 里勾选「透明背景」即可
    * 逐句高亮 + 平滑滚动，卡拉OK 那种感觉
    * 支持原文 + 译文双行
    * 字体、字号、颜色、阴影、位置都能改：面板里改（写进 config.json），
      或者临时用网址参数改（OBS 地址栏加 ?font_size=64&color=%23ffe066）
    * 用户自定义 CSS 追加在内置样式之后，可以彻底重写外观

页面只依赖 /api/overlay 一个接口，不用外部 CDN。
"""

from __future__ import annotations

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>歌词叠加层</title>
<style>
  :root{
    --font-family: "Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
    --font-size: 44px;
    --line-height: 1.35;
    --color: #ffffff;
    --active-color: #7cc4ff;
    --translation-color: #c9d4e6;
    --translation-size: 0.62em;
    --active-scale: 1.06;
    --align: center;
    --anchor: bottom;
    --pad: 4vh;
    --gap: 0.34em;
    /* 滚动列表最多同时显示几行（超出的靠平移滚出去） */
    --max-lines: 5;
    /* 文字描边/阴影：直播画面上保证可读性，可在自定义 CSS 里改掉 */
    --shadow: 0 2px 10px rgba(0,0,0,.92), 0 0 3px rgba(0,0,0,.95);
    --idle-opacity: .34;
  }
  html,body{background:transparent !important;margin:0;padding:0;overflow:hidden}
  body{font-family:var(--font-family);color:var(--color)}

  #stage{
    position:fixed;left:0;right:0;top:0;bottom:0;
    display:flex;padding:var(--pad) 3vw;
    align-items:flex-end;justify-content:center;
    overflow:hidden;pointer-events:none;
  }
  body[data-anchor="top"] #stage{align-items:flex-start}
  body[data-anchor="center"] #stage{align-items:center}

  /* 歌词用 transform 平移来滚动。
     注意这里不能用 overflow:auto + scrollIntoView —— .ln 是 flex 居中对齐的，
     会发生横向滚动；overflow:hidden 又会把滚动整个禁掉，歌词就再也不动了。 */
  #lines{
    width:100%;max-height:100%;
    max-height:calc(var(--max-lines) * var(--line-height) * var(--font-size) + var(--pad));
    overflow:hidden;
    text-align:var(--align);
    display:flex;flex-direction:column;gap:var(--gap);
    will-change:transform;
    transition:transform .38s cubic-bezier(.22,.61,.36,1);
  }

  .ln{
    font-size:var(--font-size);line-height:var(--line-height);
    color:var(--color);opacity:var(--idle-opacity);
    text-shadow:var(--shadow);
    /* 只过渡颜色和透明度：transform 一旦参与过渡，居中偏移的计算会被过渡值污染 */
    transition:opacity .28s ease,color .28s ease;
  }
  .ln .tr{
    display:block;font-size:var(--translation-size);
    color:var(--translation-color);opacity:.9;margin-top:.12em;
  }
  .ln.active{
    color:var(--active-color);opacity:1;font-weight:700;
  }
  .ln.active .tr{color:var(--active-color);opacity:1}
  .ln:empty{min-height:.9em}

  /* 专注模式：只显示当前句和它的邻居 */
  body[data-mode="focus"] .ln{display:none}
  body[data-mode="focus"] .ln.near{display:block}
  body[data-mode="focus"] .ln:not(.active):not(.near){opacity:0}

  /* 单行模式：只有当前句 */
  body[data-mode="single"] .ln{display:none}
  body[data-mode="single"] .ln.active{display:block}

  #hint{
    position:fixed;left:50%;transform:translateX(-50%);bottom:8px;
    font-size:14px;color:#9aa4b8;text-shadow:var(--shadow);
    font-family:system-ui,sans-serif;
  }
  body[data-status="playing"] #hint{display:none}
</style>
</head>
<body data-mode="scroll" data-anchor="bottom" data-status="loading">
  <div id="stage"><div id="lines"></div></div>
  <div id="hint">连接中…</div>

<script>
const qs = new URLSearchParams(location.search);
const OVERRIDES = {};   // 网址参数，优先级最高
for (const [k, v] of qs.entries()) OVERRIDES[k] = v;

const CSS_VARS = {
  font_family: '--font-family', font_size: '--font-size', line_height: '--line-height',
  color: '--color', active_color: '--active-color', translation_color: '--translation-color',
  active_scale: '--active-scale', align: '--align', anchor: '--anchor',
};
const PX_VARS = {font_size: 'px'};
const DATA_ATTRS = {mode: 'mode', anchor: 'anchor'};

let lastSongId = null;
let lines = [];
let synced = false;
let userCssEl = null;

function applyConfig(cfg){
  const root = document.documentElement;
  for (const [key, varName] of Object.entries(CSS_VARS)){
    let value = OVERRIDES[key] !== undefined ? OVERRIDES[key] : cfg[key];
    if (value === undefined || value === null || value === '') continue;
    if (PX_VARS[key] && !isNaN(Number(value))) value = Number(value) + PX_VARS[key];
    root.style.setProperty(varName, String(value));
  }
  for (const [key, attr] of Object.entries(DATA_ATTRS)){
    const value = OVERRIDES[key] !== undefined ? OVERRIDES[key] : cfg[key];
    if (value) document.body.dataset[attr] = String(value);
  }
  // 自定义 CSS：整段替换，方便实时改
  const css = OVERRIDES.css !== undefined ? OVERRIDES.css : (cfg.css || '');
  if (userCssEl) userCssEl.remove();
  userCssEl = null;
  if (css){
    userCssEl = document.createElement('style');
    userCssEl.textContent = css;
    document.head.appendChild(userCssEl);
  }
}

/** 版本号只写进标题：叠加层是直播画面，上面不能多出任何文字。 */
function applyVersion(version){
  if (!version) return;
  document.title = '歌词叠加层 ' + version;
}

function esc(t){ const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

function renderLines(list){
  const box = document.getElementById('lines');
  box.innerHTML = list.map(ln =>
    '<div class="ln">' + esc(ln.text) +
    (ln.translation ? '<span class="tr">' + esc(ln.translation) + '</span>' : '') +
    '</div>').join('');
  return Array.from(box.children);
}

function setHint(text){ document.getElementById('hint').textContent = text || ''; }

let nodes = [];
let shownIndex = -1;   // 样式是按哪一句算的
let scrolledIndex = -1; // 平移是按哪一句算的，两者分开才不会互相吃掉更新

/** 只负责更新高亮样式。 */
function styleActive(index){
  if (index === shownIndex) return;
  shownIndex = index;
  for (let i = 0; i < nodes.length; i++){
    const el = nodes[i];
    el.classList.toggle('active', i === index);
    // 邻居：专注模式要显示上下各两句
    el.classList.toggle('near', index >= 0 && Math.abs(i - index) <= 2);
  }
}

/** 用 transform 把当前句平移到容器中间。
 *
 * 不用 scrollIntoView：.ln 是 flex 居中对齐的，横向也会跟着滚，画面会歪。
 */
function centerOn(index){
  if (index === scrolledIndex) return;
  const box = document.getElementById('lines');
  const el = nodes[index];
  // 隐藏的行（专注模式下的非邻居）量为 0，跳过，免得算出错误的偏移
  if (!box || !el || !el.offsetHeight){ scrolledIndex = index; return; }
  scrolledIndex = index;

  const height = box.clientHeight || box.offsetHeight || 0;
  const want = height / 2 - (el.offsetTop + el.offsetHeight / 2);
  const total = box.scrollHeight || 0;
  const min = Math.min(0, height - total);
  const offset = Math.max(min, Math.min(0, want));
  box.style.transform = 'translateY(' + Math.round(offset) + 'px)';
}

/** 回到顶部并清掉高亮，用于换歌、歌词消失、无时间戳这些情况。 */
function clearHighlight(){
  for (const el of nodes){
    el.classList.remove('active');
  }
  shownIndex = -1;
  scrolledIndex = -1;
  const box = document.getElementById('lines');
  if (box) box.style.transform = 'translateY(0px)';
}

function applyStatus(status){
  document.body.dataset.status = status;
  if (status === 'playing') setHint('');
}

async function tick(){
  let data;
  try {
    data = await (await fetch('/api/overlay', {cache: 'no-store'})).json();
  } catch (e){
    applyStatus('offline');
    setHint('连不上机器人（它可能没在运行）');
    return;
  }

  if (data.error){ applyStatus('error'); setHint(data.error); return; }
  applyVersion(data.version);
  applyConfig(data.config || {});

  if (!data.playing){
    applyStatus('idle');
    setHint('等待播放…');
    if (lastSongId !== null){
      lastSongId = null;
      nodes = [];
      lines = [];
      document.getElementById('lines').innerHTML = '';
      clearHighlight();
    }
    return;
  }

  const lyric = data.lyric || {};
  if (lyric.song_id !== lastSongId){
    lastSongId = lyric.song_id;
    lines = lyric.lines || [];
    synced = !!lyric.synced;
    nodes = renderLines(lines);
    clearHighlight();
  }

  if (lyric.fetching){
    applyStatus('loading'); setHint('歌词加载中…');
    // 换歌后要重新算高亮，这里必须把状态清掉，否则新歌第一句可能不亮
    clearHighlight();
    return;
  }
  if (!lines.length){
    applyStatus('nolyric');
    setHint(lyric.error ? ('取歌词失败：' + lyric.error) : '这首歌没有歌词');
    clearHighlight();
    return;
  }
  if (!synced){
    // 没有时间戳：整段静态显示，回到顶部
    applyStatus('playing');
    for (const el of nodes) el.classList.add('near');
    clearHighlight();
    return;
  }

  applyStatus('playing');
  const elapsed = data.elapsed || 0;
  let index = -1;
  for (let i = 0; i < lines.length; i++){
    if (lines[i].time !== null && lines[i].time <= elapsed) index = i; else break;
  }
  styleActive(index);
  centerOn(index);
}

tick();
setInterval(tick, 250);
</script>
</body>
</html>
"""
