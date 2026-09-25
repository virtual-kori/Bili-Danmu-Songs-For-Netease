"""便携包（自带运行时）打包机制测试。

便携包能让"全新电脑解压即用"，靠的是几处容易在后续改动里被破坏的约定。
这个测试专门守住它们 —— 不需要真的打一次包，几秒就跑完。

用法：
    .venv\\Scripts\\python.exe tests\\portable_bundle_test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import RUNTIME_NODE, RUNTIME_PYTHON, VERSION, bundled_runtime, venv_health  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  —— {detail}" if detail else ""))
    return ok


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 66)
    print("  便携包打包机制测试")
    print("=" * 66)

    # ---------------------------------------------------------- 运行时路径约定
    print("\n-- 运行时路径约定 --")
    check("内置 Python 路径约定正确",
          RUNTIME_PYTHON == ROOT / "runtime" / "python" / "python.exe",
          str(RUNTIME_PYTHON.relative_to(ROOT)))
    check("内置 Node 路径约定正确",
          RUNTIME_NODE == ROOT / "runtime" / "node" / "node.exe",
          str(RUNTIME_NODE.relative_to(ROOT)))

    # 开发机上通常没有 runtime/，所以 bundled_runtime() 应为 False；
    # 关键是不能抛异常、且一旦建了 runtime 目录就能认出来
    try:
        value = bundled_runtime()
        check("bundled_runtime() 可调用且返回布尔", isinstance(value, bool), f"当前 = {value}")
    except Exception as exc:  # noqa: BLE001
        check("bundled_runtime() 可调用且返回布尔", False, f"{type(exc).__name__}: {exc}")

    fake_root = ROOT / "runtime" / "python" / "python.exe"
    if not fake_root.exists():
        try:
            fake_root.parent.mkdir(parents=True, exist_ok=True)
            fake_root.write_bytes(b"")
            check("建成 runtime 后 bundled_runtime() 变为 True", bundled_runtime() is True, "")
            ok, reason = venv_health()
            check("自带运行时时 venv_health() 直接通过", ok, reason)
        finally:
            fake_root.unlink(missing_ok=True)
            # 只清理我们自己造出来的空目录
            for parent in (fake_root.parent, fake_root.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break
    else:
        ok, reason = venv_health()
        check("自带运行时时 venv_health() 直接通过", ok, reason)

    # ---------------------------------------------------------- 启动脚本
    print("\n-- 启动脚本 --")
    required_scripts = [
        "run.cmd", "start_bot.cmd", "start_netease_api.cmd", "login_netease.cmd",
        "open_panel.cmd", "get_mpv.cmd", "check_env.cmd", "_check_env.cmd",
        "make_portable.cmd", "make_portable_bundle.cmd",
    ]
    missing = [name for name in required_scripts if not (ROOT / name).is_file()]
    check("启动脚本齐全", not missing, f"缺少 {missing}" if missing else f"{len(required_scripts)} 个")

    check_env = read("_check_env.cmd")
    check("_check_env.cmd 会选中内置解释器", "runtime\\python\\python.exe" in check_env, "")
    check("_check_env.cmd 保留 .venv 回退", ".venv\\Scripts\\python.exe" in check_env, "")
    check("_check_env.cmd 导出 PYTHON_EXE", 'set "PYTHON_EXE=' in check_env, "")

    # 每个要跑 python 的脚本都必须用 %PYTHON_EXE%，不能写死 .venv
    callers = [
        "run.cmd", "start_bot.cmd", "login_netease.cmd",
        "open_panel.cmd", "get_mpv.cmd", "make_portable.cmd", "make_portable_bundle.cmd",
    ]
    hardcoded = [name for name in callers if '".venv\\Scripts\\python.exe"' in read(name)]
    check("没有脚本把 .venv 解释器写死", not hardcoded,
          f"这些还在写死：{hardcoded}" if hardcoded else f"检查了 {len(callers)} 个")

    uses_exe = [name for name in callers if "%PYTHON_EXE%" in read(name)]
    check("各脚本都改用 %PYTHON_EXE%", len(uses_exe) == len(callers),
          f"{len(uses_exe)}/{len(callers)}")

    api_script = read("start_netease_api.cmd")
    check("API 服务脚本优先用内置 node", "runtime\\node\\node.exe" in api_script, "")
    check("API 服务脚本保留系统 node 回退", 'set "NODE_EXE=node"' in api_script, "")

    # ---------------------------------------------------------- 打包脚本
    print("\n-- 打包脚本 --")
    check("便携包构建脚本存在", (ROOT / "make_portable_bundle.py").is_file(), "")
    try:
        import make_portable_bundle as bundle
        check("构建脚本可导入", True, "")
    except Exception as exc:  # noqa: BLE001
        check("构建脚本可导入", False, f"{type(exc).__name__}: {exc}")
        bundle = None

    if bundle is not None:
        check("构建脚本会下 Python embeddable",
              "python.org" in bundle.PYTHON_EMBED_URL and "embed-amd64" in bundle.PYTHON_EMBED_URL,
              bundle.PYTHON_EMBED_URL)
        check("构建脚本会下 Node 便携版",
              "nodejs.org" in bundle.NODE_URL and "win-x64" in bundle.NODE_URL,
              bundle.NODE_URL)
        check("依赖清单含全部必需包",
              set(bundle.REQUIRED_PACKAGES) >= {"aiohttp", "brotli", "requests", "qrcode", "pillow", "pygame"},
              str(bundle.REQUIRED_PACKAGES))
        check("blivedm 走 --no-deps（绕开写死的 brotli）",
              bundle.NO_DEPS_PACKAGES == ("blivedm",), str(bundle.NO_DEPS_PACKAGES))
        check("自检模块清单覆盖全部依赖",
              set(bundle.SMOKE_MODULES) >= {"blivedm", "aiohttp", "brotli", "requests", "qrcode", "PIL", "pygame"},
              str(bundle.SMOKE_MODULES))
        check("便携包名指向桌面同级目录", True, bundle.DEFAULT_NAME)

        # 这是整个方案的关键：._pth 里的相对路径必须指向包根目录（runtime\python\..\..）。
        # 一旦写错，便携包的解释器就 import 不到项目模块。
        src = (ROOT / "make_portable_bundle.py").read_text(encoding="utf-8")
        check("._pth 写入项目根目录相对路径", '"..\\\\.."' in src or "..\\\\.." in src,
              "需要 '..\\..' 这一行")
        check("._pth 写入 site-packages", "Lib\\\\site-packages" in src, "")

    # ---------------------------------------------------------- 解释器内部的路径引导
    print("\n-- 解释器路径引导 --")
    for name in ("run.py", "bootstrap.py"):
        body = read(name)
        check(f"{name} 自己把项目根目录加进 sys.path",
              "sys.path.insert" in body and "__file__" in body, "")

    common_body = read("common.py")
    check("common.py 暴露 RUNTIME_* 常量",
          all(k in common_body for k in ("RUNTIME_DIR", "RUNTIME_PYTHON", "RUNTIME_SITE_PACKAGES", "RUNTIME_NODE")), "")

    # 用 AST 精确取 venv_health 的函数体（按字符串切片容易被长 docstring 干扰）
    import ast

    tree = ast.parse(common_body)
    venv_fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "venv_health"),
        None,
    )
    if venv_fn is None:
        check("venv_health 优先认内置运行时", False, "没找到 venv_health 函数")
    else:
        body_src = ast.unparse(venv_fn)
        first_guard = body_src.split("\n")[1] if "\n" in body_src else body_src
        check("venv_health 优先认内置运行时",
              "bundled_runtime()" in body_src,
              "函数体里应调用 bundled_runtime()")
        check("bundled_runtime 的判断排在 .venv 检查之前",
              body_src.index("bundled_runtime()") < body_src.index("VENV_DIR"),
              f"第一行判断：{first_guard.strip()[:60]}")

    check_body = read("check_env.py")
    check("check_env.py 也认内置 node", "RUNTIME_NODE" in check_body, "")
    check("check_env.py 文案区分便携包", "便携包" in check_body, "")

    boot_body = read("bootstrap.py")
    check("bootstrap.py 认内置 node", "RUNTIME_NODE" in boot_body, "")
    check("bootstrap.py 文案区分便携包", "便携包" in boot_body, "")

    # ---------------------------------------------------------- 说明文件
    print("\n-- 说明文件 --")
    readme_src = (ROOT / "make_portable_bundle.py").read_text(encoding="utf-8")
    check("便携包内会生成专属说明", "使用说明（便携版）.txt" in readme_src, "")
    check("说明里提到免装 Python/Node", "不需要" in readme_src and "Python" in readme_src, "")
    check("版本号会写进便携包说明", "{version}" in readme_src, f"当前版本 {VERSION}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 66)
    print(f"  结果：{passed}/{total} 通过")
    for label, ok, _ in results:
        if not ok:
            print(f"    未通过：{label}")
    print("=" * 66)
    return 1 if passed != total else 0


if __name__ == "__main__":
    raise SystemExit(main())
