/**
 * 叠加层前端逻辑测试（Node 环境，假 DOM）。
 *
 * 直接跑 lyric_overlay.py 里那份生产 JS，不复制逻辑 —— JS 改了这里就会跟着测到。
 * 用法：python tests/lyric_overlay_js_test.py
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const py = fs.readFileSync(path.join(ROOT, 'lyric_overlay.py'), 'utf8');
const pageMatch = py.match(/PAGE = """([\s\S]*?)"""/);
if (!pageMatch) { console.error('没能从 lyric_overlay.py 里取出 PAGE'); process.exit(1); }
const PAGE = pageMatch[1];

const scriptMatch = PAGE.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) { console.error('页面里没有 <script>'); process.exit(1); }
const productionJs = scriptMatch[1];

// ---------------------------------------------------------------- 假 DOM
function makeClassList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, on) => { if (on) set.add(c); else set.delete(c); },
    contains: (c) => set.has(c),
  };
}

class FakeEl {
  constructor(tag, id) {
    this.tagName = (tag || 'div').toUpperCase();
    this.id = id || '';
    this.children = [];
    this.classList = makeClassList();
    this.dataset = {};
    this.style = { props: {}, setProperty(k, v) { this.props[k] = v; } };
    this._html = '';
    this._text = '';
    this.scrolled = 0;
    this.removed = false;
    // 几何量：centerOn() 靠这些算平移偏移
    this.offsetTop = 0;
    this.offsetHeight = 0;
    this.clientHeight = 0;
    this.scrollHeight = 0;
  }
  set innerHTML(v) { this._html = v; this._rebuild(); }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text; }
  remove() { this.removed = true; }
  scrollIntoView() { this.scrolled++; }
  appendChild(el) { this.children.push(el); }
  _rebuild() {
    // 把 innerHTML 里的 <div class="ln">文字<span class="tr">译文</span></div> 解析成子节点
    this.children = [];
    const re = /<div class="ln">([\s\S]*?)<\/div>/g;
    let hit;
    while ((hit = re.exec(this._html))) {
      const inner = hit[1];
      const el = new FakeEl('div');
      el.classList.add('ln');
      const trMatch = inner.match(/<span class="tr">([\s\S]*?)<\/span>/);
      el._text = (trMatch ? inner.slice(0, inner.indexOf('<span')) : inner).trim();
      if (trMatch) {
        const span = new FakeEl('span');
        span.classList.add('tr');
        span._text = trMatch[1];
        el.children.push(span);
      }
      this.children.push(el);
    }
    // 假的纵向几何：每行 60px，容器高 300px（约 5 行，和 --max-lines 一致）
    this.children.forEach((el, i) => {
      el.offsetTop = i * 60;
      el.offsetHeight = 60;
    });
    this.clientHeight = 300;
    this.offsetHeight = 300;
    this.scrollHeight = Math.max(300, this.children.length * 60);
  }
}

const elements = {};
['lines', 'hint', 'stage'].forEach((id) => { elements[id] = new FakeEl('div', id); });

const headChildren = [];
const body = new FakeEl('body');
const documentStub = {
  documentElement: { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } },
  body,
  head: { appendChild: (el) => { headChildren.push(el); } },
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => new FakeEl(tag),
  title: '',
};

let intervalFn = null;
let payload = null;
let fetchShouldFail = false;
const location = { search: '' };
const fetchStub = async () => {
  if (fetchShouldFail) throw new Error('offline');
  return { json: async () => payload };
};

// ---------------------------------------------------------------- 测试框架
const results = [];
function check(label, ok, detail) {
  results.push([label, ok, detail || '']);
  console.log(`[${ok ? '通过' : '失败'}] ${label}${detail ? '  —— ' + detail : ''}`);
}

// 把生产 JS 跑起来，并在同作用域里导出内部状态供断言
const patched = productionJs.replace(
  /^tick\(\);\s*$/m,
  'globalThis.__api = { tick, styleActive, centerOn, clearHighlight, applyConfig, renderLines,'
  + ' get nodes(){return nodes;},'
  + ' get lines(){return lines;}, get synced(){return synced;} };\n'
);

const runner = new Function(
  'document', 'window', 'location', 'fetch', 'setInterval', 'URLSearchParams', 'globalThis',
  patched
);

function run(search) {
  for (const id of Object.keys(elements)) {
    elements[id].innerHTML = '';
    elements[id]._text = '';
    elements[id].classList = makeClassList();
  }
  body.dataset = {};
  documentStub.documentElement.style.props = {};
  headChildren.length = 0;
  intervalFn = null;
  location.search = search || '';
  runner(documentStub, { addEventListener() {} }, location, fetchStub,
         (fn) => { intervalFn = fn; return 1; }, URLSearchParams, globalThis);
  return globalThis.__api;
}

function lyricsPayload(times, elapsed) {
  return {
    playing: true,
    elapsed,
    now_playing: { name: '歌', artists: '手' },
    lyric: {
      enabled: true, song_id: 7, fetching: false, have: true, error: '',
      synced: true, has_translation: true, count: times.length,
      lines: times.map((t, i) => ({ time: t, text: '第' + (i + 1) + '句', translation: 'L' + (i + 1) })),
    },
    config: { font_size: 44, mode: 'scroll', anchor: 'bottom', color: '#ffffff' },
  };
}

(async () => {
  console.log('='.repeat(66));
  console.log('  叠加层前端逻辑测试');
  console.log('='.repeat(66));

  const times = [0, 5, 10, 15, 20];
  const activeIdx = (api) => api.nodes.findIndex((n) => n.classList.contains('active'));

  // ------------------------------------------------------ 高亮索引
  let api = run('');
  payload = lyricsPayload(times, 12);
  await api.tick();
  check('12s 高亮第 3 句（下标 2）', activeIdx(api) === 2, '实际=' + activeIdx(api));

  payload = lyricsPayload(times, 0.5);
  await api.tick();
  check('0.5s 高亮第 1 句', activeIdx(api) === 0, '实际=' + activeIdx(api));

  payload = lyricsPayload(times, 999);
  await api.tick();
  check('超过最后一句停在最后', activeIdx(api) === 4, '实际=' + activeIdx(api));

  payload = lyricsPayload(times, -5);
  await api.tick();
  check('负进度不高亮任何句', activeIdx(api) === -1, '实际=' + activeIdx(api));

  payload = lyricsPayload(times, 10);
  await api.tick();
  check('正好 10s 高亮第 3 句', activeIdx(api) === 2, '实际=' + activeIdx(api));

  payload = lyricsPayload(times, 17);
  await api.tick();
  check('同时只有一句高亮',
        api.nodes.filter((n) => n.classList.contains('active')).length === 1,
        '实际=' + api.nodes.filter((n) => n.classList.contains('active')).length);

  const nearCount = api.nodes.filter((n) => n.classList.contains('near') || n.classList.contains('active')).length;
  check('邻居范围限制在上下 2 句内', nearCount <= 5, '实际=' + nearCount);

  // ------------------------------------------------------ 渲染内容
  check('渲染出 5 个节点', api.nodes.length === 5, '节点=' + api.nodes.length);
  check('每行都带译文', elements['lines'].innerHTML.includes('class="tr"'), '');

  // ------------------------------------------------------ 平移滚动（transform）
  // 每行 60px、容器 300px：第 3 句（下标 2）应被平移到容器中间
  const tf = () => elements['lines'].style.transform || '';
  api = run('');
  payload = lyricsPayload(times, 12);   // 下标 2
  await api.tick();
  check('当前句被平移（写入 transform）', /^translateY\(-?\d+px\)$/.test(tf()), tf());
  // 自然位置在 120px，容器一半 150px，句高 60 → 需要上移 150-120-30 = 0；但容器只有 300 高、
  // 内容 300 高，min 为 0，所以这里应当是 0
  check('内容不超长时不偏移', tf() === 'translateY(0px)', tf());

  // 内容超长（10 行 = 600px）时，靠后的句子必须被平移到可视区
  const many = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45];
  payload = lyricsPayload(many, 42);    // 下标 8
  payload.lyric.song_id = 77;
  payload.lyric.count = 10;
  await api.tick();
  const n = Number((tf().match(/-?\d+/) || [0])[0]);
  check('超长内容会向上平移露出当前句', n < 0, tf());
  check('平移量不会超过容器下界', n >= 300 - 600, tf());

  payload = lyricsPayload(many, 47);    // 最后一句，下标 9
  payload.lyric.song_id = 77;
  payload.lyric.count = 10;
  await api.tick();
  const n2 = Number((tf().match(/-?\d+/) || [0])[0]);
  check('最后一句被夹在下界', n2 === 300 - 600, tf());

  payload = lyricsPayload(many, 0);     // 第一句
  payload.lyric.song_id = 77;
  payload.lyric.count = 10;
  await api.tick();
  check('第一句回到不偏移', tf() === 'translateY(0px)', tf());

  // ------------------------------------------------------ 换歌重绘
  api = run('');
  payload = lyricsPayload(times, 3);
  await api.tick();
  const beforeHtml = elements['lines'].innerHTML;
  // 换首歌就等于换了 song_id，内容自然重绘
  payload = lyricsPayload([0, 1], 0.5);
  payload.lyric.song_id = 8;
  payload.lyric.count = 2;
  await api.tick();
  check('换歌后歌词重绘', elements['lines'].innerHTML !== beforeHtml && api.nodes.length === 2,
        '节点=' + api.nodes.length);

  // 同一首歌内容不变时不重建 DOM（避免每秒闪烁）
  const htmlBefore = elements['lines'].innerHTML;
  await api.tick();
  check('同一首歌不重复重建 DOM', elements['lines'].innerHTML === htmlBefore, '');

  // ------------------------------------------------------ 自定义 CSS
  api = run('');
  payload = lyricsPayload(times, 3);
  payload.config.css = '.ln.active{color:red}';
  await api.tick();
  check('自定义 CSS 被注入 <head>',
        headChildren.length === 1 && headChildren[0].textContent === '.ln.active{color:red}',
        '注入数=' + headChildren.length);
  check('注入的是 style 节点', headChildren[0] && headChildren[0].tagName === 'STYLE', '');

  payload.config.css = '.ln{color:blue}';
  await api.tick();
  check('改 CSS 后旧样式被移除', headChildren[0].removed === true, '');
  check('新 CSS 已注入',
        headChildren.length === 2 && headChildren[1].textContent === '.ln{color:blue}', '');

  payload.config.css = '';
  await api.tick();
  check('清空 CSS 后无残留样式', headChildren.filter((e) => !e.removed).length === 0, '');

  // ------------------------------------------------------ 配置 → CSS 变量
  api = run('');
  payload = lyricsPayload(times, 3);
  payload.config.font_size = 72;
  payload.config.active_color = '#ff5c8a';
  payload.config.align = 'left';
  await api.tick();
  const props = documentStub.documentElement.style.props;
  check('字号映射到 --font-size', props['--font-size'] === '72px', String(props['--font-size']));
  check('高亮色映射到 --active-color', props['--active-color'] === '#ff5c8a', String(props['--active-color']));
  check('对齐映射到 --align', props['--align'] === 'left', String(props['--align']));
  check('位置映射到 body 属性', body.dataset.anchor === 'bottom', String(body.dataset.anchor));

  // ------------------------------------------------------ 网址参数覆盖
  api = run('?font_size=100&color=%23ffe066&mode=focus');
  payload = lyricsPayload(times, 3);
  payload.config.font_size = 44;
  await api.tick();
  const props2 = documentStub.documentElement.style.props;
  check('网址参数覆盖字号', props2['--font-size'] === '100px', String(props2['--font-size']));
  check('网址参数覆盖颜色', props2['--color'] === '#ffe066', String(props2['--color']));
  check('网址参数覆盖模式', body.dataset.mode === 'focus', String(body.dataset.mode));

  // ------------------------------------------------------ 版本号
  // 叠加层是直播画面，版本号只能进标题，绝不能在画面上显示出来
  api = run('');
  payload = lyricsPayload(times, 3);
  payload.version = 'V.0.1.1';
  await api.tick();
  check('版本号写进页面标题', documentStub.title.includes('V.0.1.1'), documentStub.title);
  payload.version = '';
  await api.tick();
  check('没有版本号时不报错', typeof documentStub.title === 'string', documentStub.title);
  check('画面上不出现版本号',
        !elements['lines'].innerHTML.includes('V.0.1.1') && !elements['hint'].textContent.includes('V.0.1.1'),
        elements['hint'].textContent);

  // ------------------------------------------------------ 空状态
  // 每段都用新实例：换 payload 但不换 song_id 时，页面会（正确地）沿用已渲染的歌词
  api = run('');
  payload = lyricsPayload(times, 3);
  payload.playing = false;
  await api.tick();
  check('没在播放时状态为 idle', body.dataset.status === 'idle', String(body.dataset.status));

  api = run('');
  payload = lyricsPayload(times, 3);
  payload.lyric.fetching = true;
  await api.tick();
  check('歌词加载中状态正确', body.dataset.status === 'loading', String(body.dataset.status));

  api = run('');
  payload = lyricsPayload(times, 3);
  payload.lyric.lines = [];
  payload.lyric.count = 0;
  payload.lyric.have = false;
  await api.tick();
  check('无歌词状态正确', body.dataset.status === 'nolyric', String(body.dataset.status));
  check('无歌词时高亮被清掉', activeIdx(api) === -1, '实际=' + activeIdx(api));

  api = run('');
  payload = lyricsPayload(times, 3);
  payload.lyric.synced = false;
  payload.lyric.lines = times.map((t, i) => ({ time: null, text: '行' + i, translation: '' }));
  await api.tick();
  check('无时间戳时不报错', body.dataset.status === 'playing', String(body.dataset.status));
  check('无时间戳时不乱高亮', activeIdx(api) === -1, '实际=' + activeIdx(api));

  // 歌词重新加载后高亮要能恢复（曾经因为 lastIndex 没复位而失效）
  api = run('');
  payload = lyricsPayload(times, 12);
  await api.tick();
  check('恢复前高亮正常（下标 2）', activeIdx(api) === 2, '实际=' + activeIdx(api));
  payload = lyricsPayload(times, 12);
  payload.lyric.fetching = true;
  await api.tick();
  payload = lyricsPayload(times, 12);
  await api.tick();
  check('加载中转完后高亮能恢复', activeIdx(api) === 2, '实际=' + activeIdx(api));

  api = run('');
  payload = {
    playing: true, elapsed: 3, now_playing: { name: '歌' },
    lyric: { enabled: false, song_id: 1, lines: [], count: 0, fetching: false, synced: false },
    config: {},
  };
  await api.tick();
  check('歌词关闭时状态为 nolyric', body.dataset.status === 'nolyric', String(body.dataset.status));

  fetchShouldFail = true;
  api = run('');
  await api.tick();
  check('连不上机器人时状态为 offline', body.dataset.status === 'offline', String(body.dataset.status));
  check('连不上时给出提示', elements['hint'].textContent.includes('连不上'), elements['hint'].textContent);
  fetchShouldFail = false;

  // ------------------------------------------------------ 轮询
  api = run('');
  check('注册了定时轮询', typeof intervalFn === 'function', typeof intervalFn);

  const passed = results.filter((r) => r[1]).length;
  console.log('\n' + '='.repeat(66));
  console.log(`  结果：${passed}/${results.length} 通过`);
  results.filter((r) => !r[1]).forEach((r) => console.log('    未通过：' + r[0]));
  console.log('='.repeat(66));
  process.exit(passed === results.length ? 0 : 1);
})().catch((err) => { console.error('测试崩了：', err); process.exit(1); });
