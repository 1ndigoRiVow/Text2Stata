# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（onedir 模式）。

为什么不是 onefile：单文件模式每次启动都要把上百 MB 解压到临时目录，
冷启动要等好几秒，而且被杀毒软件误报的概率明显更高。onedir 解压一次就行。

为什么不是 Docker：Windows 上 Docker 跑的是 Linux 容器，
容器里的进程调不到宿主机安装的 StataSE-64.exe —— 这条路直接堵死。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")

# --------------------------------------------------------------------------
# 随包数据
# 目标路径不是随便定的：core/paths.py 用 sys._MEIPASS 拼只读资源位置，
# 而 TemplateManager 按 `__file__` 的兄弟目录找 templates/，
# 所以模板必须落在 core/templates 这一层。
# --------------------------------------------------------------------------
datas = [
    (os.path.join(PROJECT_ROOT, "frontend"), "frontend"),
    (os.path.join(BACKEND_DIR, "core", "templates"), os.path.join("core", "templates")),
]
binaries = []

# --------------------------------------------------------------------------
# 隐式导入
# launcher.py 里 `import app` / `from core import ...` 写在函数体内，
# PyInstaller 静态分析看不到，必须显式列出来。
# pyreadstat 带编译好的 _readstat 扩展，collect_all 才拿得全。
# --------------------------------------------------------------------------
hiddenimports = ["app", "core", "core.config", "core.paths", "core.stata_detect",
                 "core.llm_agent", "core.logic_center", "core.result_auditor",
                 "core.stata_worker", "core.template_manager"]
hiddenimports += collect_submodules("core")

for package in ("pyreadstat", "pandas", "flask", "flask_cors"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:  # 某个包收集失败不该让整次打包挂掉
        pass

a = Analysis(
    [os.path.join(PROJECT_ROOT, "launcher.py")],
    pathex=[BACKEND_DIR, PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 这些都跟本项目无关，排掉能省不少体积
    excludes=[
        "matplotlib", "scipy", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter", "notebook", "pytest", "setuptools", "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Text2Stata",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 保留控制台：窗口里会打印访问地址，并告诉用户"关掉本窗口即退出"。
    # 对一个要发给学生的工具来说，这比静默运行友好得多。
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Text2Stata",
)
