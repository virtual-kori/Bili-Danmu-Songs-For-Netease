# 更新日志

本项目的所有重要变更都会记在这里。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号形如 `V.0.1.2`，对应的 git tag 是 `v0.1.2`。

---

## [V.0.1.2] - 2026-09-26

**可以打成"自带运行时"的便携包了**：一台没装过 Python 和 Node.js 的干净电脑，
解压后双击 `run.cmd` 就能跑。

### 新增

- **便携包打包**（`make_portable_bundle.cmd` / `make_portable_bundle.py`）
  - 把 **Python 解释器 + Node.js + 全部依赖**一起打包，目标电脑免安装任何东西
  - Python 用官方 **embeddable** 包（10.5MB，只有解释器和标准库，不是完整安装版）；
    Node 只提取 `node.exe` 和 npm 必需文件，丢掉文档和头文件 —— 这是"轻量"能做到的程度
  - 打包结束后会用**包内解释器实跑自检**：导入全部依赖和项目模块，确认真的能跑
  - 支持 `--zip` / `--no-mpv`（省约 120MB）/ `--no-node-modules` / `--out` / `--force`
  - 体积约 320MB，压成 zip 约 135MB
  - 包内会生成 `使用说明（便携版）.txt`
  - 与原有的 `make_portable.py` 并存，两者区别：

    | | `make_portable.py` | `make_portable_bundle.py` |
    |---|---|---|
    | 体积 | 约 70MB | 约 320MB |
    | 自带 Python / Node | ❌ | ✅ |
    | 目标电脑要做什么 | 装 Python+Node，跑 install.cmd | 解压，双击 run.cmd |

- **启动脚本同时兼容便携包和开发环境**
  - `_check_env.cmd` 负责在两套运行时里选一个，并导出 `PYTHON_EXE` 供各脚本使用
  - 7 个启动脚本不再把 `.venv` 路径写死
  - `run.py` / `check_env.py` / `bootstrap.py` / `start_netease_api.cmd` 优先使用内置 node
  - `common.py` 新增 `RUNTIME_PYTHON` / `RUNTIME_NODE` / `bundled_runtime()`；
    `venv_health()` 优先认内置运行时（便携包不需要那套"虚拟环境跨电脑"的检查）
  - `run.py`、`bootstrap.py` 自己把项目根目录加进 `sys.path`
    （embeddable 解释器的 `sys.path` 不含脚本所在目录）

- **测试**：`tests/portable_bundle_test.py`（35 项），守住打包机制的约定。

### 修复

- **5 个 `.cmd` 被写成了 LF 换行**，已按项目约定改为 CRLF ——
  `cmd.exe` 解析标签跳转时 LF 可能出错。

### 说明（实现上的关键点）

- embeddable 解释器一旦存在 `python*._pth` 文件，就进入 **isolated 模式并完全忽略
  `PYTHONPATH`**。所以项目根目录必须在打包时以**相对路径**（`..\..`）写进 `._pth`，
  否则便携包里的解释器 import 不到项目模块。这条约定有测试守着。
- 便携包**不带** `config.json`（里面有直播间号和登录凭据），首次运行自动生成默认配置。

---

## [V.0.1.1] - 2026-09-25

**加入了歌词叠加层**：现在放歌的时候，直播画面上能同步显示歌词了。

### 新增

- **歌词抓取**
  - 网易云接口新增 `lyric_full()`，同时取回原文歌词和**翻译歌词**（`tlyric`）。
    原先的 `lyric()` 只取原文，且项目中从未被调用过。
  - 歌词在**后台线程**抓取，不阻塞起播；抓到之前叠加层显示「歌词加载中…」。
  - 按歌曲 id 缓存（上限 80 首），同一首歌再放到不会重复请求。
  - 切歌时用世代计数丢弃过期结果，避免慢请求把上一首的歌词盖到新歌上。
  - 拿不到歌词（纯音乐、冷门歌、接口异常）只提示「这首歌没有歌词」，**不影响放歌**。

- **OBS 歌词叠加层**（`/overlay`）
  - 独立页面，**背景完全透明**，直接作为 OBS「浏览器源」使用，不需要抠像或滤色。
  - **逐句同步高亮**：按播放进度高亮当前句，容器内平滑滚动跟随。
  - **中英双语**：原文 + 译文双行显示，译文按时间戳容差对齐到原文行。
  - **三种显示方式**：滚动列表 / 只显示当前句和上下句 / 只显示当前句。
  - **字体与样式可自定义**：字体、字号、行高、当前句放大倍数、三种颜色、
    对齐方式、贴顶/居中/贴底，全部通过 CSS 变量驱动。
  - **自定义 CSS**：追加在内置样式之后，优先级最高，可以彻底重写外观。
    可用的钩子有 `#stage`、`#lines`、`.ln`、`.ln.active`、`.ln.near`、`.ln .tr`，
    以及 `body[data-status]`（`loading`/`playing`/`nolyric`/`idle`/`offline`）。
  - **网址参数临时调参**：`?font_size=64&color=%23ffe066&mode=focus`，只影响当前源，
    方便在 OBS 里快速试效果，满意后再存进配置。

- **控制面板**
  - 新增歌词区：显示当前歌词并逐句高亮。
  - 新增「歌词叠加层」栏：一键复制 OBS 地址、预览按钮。
  - 新增「歌词样式设置」：可视化调节上述全部样式，保存后叠加层自动刷新，无需重开 OBS。
  - 标题旁显示当前版本号。

- **配置文件**：`config.json` 新增 `lyric` 段（总开关、显示方式、字体、字号、颜色、CSS 等）。

- **测试**：新增 4 个测试套件，共 154 项，都不需要网易云 API 服务：
  | 套件 | 项数 | 覆盖 |
  |---|---|---|
  | `tests/lyrics_test.py` | 36 | LRC 解析：毫秒位数、`[offset:]`、一行多时间戳、双语对齐、重复行定位、无时间戳歌词、3000 行性能 |
  | `tests/lyric_overlay_test.py` | 52 | 叠加层透明性、接口字段、设置落盘、非法输入拦截、**不劫持页面滚动**、版本号 |
  | `tests/lyric_bot_test.py` | 22 | 缓存命中、换歌丢弃过期结果、失败降级、缓存上限 |
  | `tests/lyric_overlay_js.test.js` | 44 | **直接执行叠加层页面里那份真实 JS**，验证高亮下标、平移夹取、CSS 注入/移除、版本号 |

### 修复

- **面板歌词高亮会劫持整个页面滚动**：原先用 `scrollIntoView()`，它会滚动所有可滚动祖先，
  导致每唱一句页面就被拉回歌词卡片，用户无法查看队列和日志。改为只调整歌词框自身的
  `scrollTop`，并已加入测试守卫防止改回去。
- **重复歌词行定位串位**：`index_at()` 原先用 `list.index()` 查找，副歌这类重复句会
  永远命中第一处。改为维护「带时间戳行 → 原行下标」的映射。
- **叠加层滚动不生效**：`#lines` 使用 `overflow:hidden` 会让 `scrollIntoView()` 完全失效
  （歌词不动），改用 `overflow:auto` 又会因 flex 居中对齐导致横向滚动。
  最终改为 `transform: translateY()` 平移方案，并夹取偏移范围。
- **歌词重新加载后高亮失效**：`lastIndex` 未复位，导致 `setActive(相同下标)` 被提前返回。
- **`[offset:+500]` 等元数据标签被当成歌词显示**：现已过滤 `offset`/`ti`/`ar`/`al`/`by` 等标签。
- **换歌后歌词框停留在上个位置**：现在会滚回顶部。

### 说明

- 这里的「桌面歌词」指的是 **OBS 浏览器源叠加层**，不是独立的置顶桌面窗口。
  浏览器页面无法置顶或实现桌面级拖拽，如果需要那种形态，需要单独用原生窗口实现。

---

## [V.0.1.0] - 2026-09-25

基础功能（本版本之前仓库没有版本号，这里补记为 0.1.0，未单独打 tag）：

- 观众在直播间发 `点歌 歌名`，机器人自动搜索、排队、用本机播放器播放
- 网易云扫码登录，VIP 账号可播放会员歌曲
- 本地网页面板：状态、队列、弹幕流水、日志、切歌/暂停/音量等控制，可在面板上直接点歌
- 空闲随机播放：队列为空时自动补歌，来源支持私人 FM / 每日推荐 / 关键词
- 播放后端自动探测：mpv / ffplay / pygame / WMP / null，优先 mpv
- 预下载下一首，减少切歌等待
- 可选回发弹幕（需配置 `sessdata` + `bili_jct`）
- 一键脚本：`install.cmd`（装依赖）、`run.cmd`（运行）、`make_portable.cmd`（打包迁移）
- 仓库整理：`.gitignore` 排除 `node_modules` 与 `tools/`（均由脚本自动重建），
  清除历史上误提交的 121 MB 二进制，仓库从 168.7 MB 降至 162 KB

[V.0.1.2]: https://github.com/virtual-kori/Bili-Danmu-Songs-For-Netease/releases/tag/v0.1.2
[V.0.1.1]: https://github.com/virtual-kori/Bili-Danmu-Songs-For-Netease/releases/tag/v0.1.1
[V.0.1.0]: https://github.com/virtual-kori/Bili-Danmu-Songs-For-Netease/releases/tag/v0.1.0
