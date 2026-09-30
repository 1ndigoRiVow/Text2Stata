"""
Stata 可执行文件自动探测。

要用户手动填 `C:\\Program Files\\StataNow19\\StataSE-64.exe` 这种路径，
"傻瓜版"就无从谈起。这里把能找到的 Stata 都翻出来，按可信度排序。

三个来源，按可靠程度依次尝试：

  1. 文件关联 —— 注册表里 `.do` 指向的 progid 的打开命令。
     这是最准的，因为它就是双击 .do 文件时系统实际会启动的那个程序。
  2. 常见安装目录 —— Program Files / Program Files (x86) / 用户目录下的 Stata*。
  3. PATH —— `where stata`。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

# Windows 上三个可执行文件后缀；MP 能力最强，同版本优先
_EDITION_RANK = {"mp": 0, "se": 1, "be": 2}

_EXE_NAME = re.compile(r"^Stata(?P<edition>MP|SE|BE)(?:-64)?\.exe$", re.IGNORECASE)
_QUOTED = re.compile(r'"([^"]+)"')


def _rank(path: Path) -> tuple:
    """排序键：版本号越大越靠前，同版本 MP > SE > BE，64 位优先。"""
    text = str(path)
    version = 0
    for token in re.findall(r"(\d{2})", text):
        version = max(version, int(token))

    match = _EXE_NAME.match(path.name)
    edition = _EDITION_RANK.get(match.group("edition").lower(), 9) if match else 9
    is_64 = 0 if "-64" in path.name else 1
    return (-version, edition, is_64)


# --------------------------------------------------------------------------
# 来源 1：注册表文件关联
# --------------------------------------------------------------------------
def _from_registry() -> list[tuple[Path, str]]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    found: list[tuple[Path, str]] = []
    for extension in (".do", ".dta"):
        progid = None
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Classes\{extension}") as key:
                    progid = winreg.QueryValueEx(key, "")[0]
                    break
            except OSError:
                continue
        if not progid:
            continue

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive, rf"SOFTWARE\Classes\{progid}\shell\open\command"
                ) as key:
                    command = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            # 形如 "C:\Program Files\StataNow19\StataSE-64.exe" "%1"
            quoted = _QUOTED.search(command)
            candidate = quoted.group(1) if quoted else command.split()[0]
            path = Path(candidate)
            if path.name.lower().startswith("stata") and path.exists():
                found.append((path, f"文件关联（{extension}）"))
            break
    return found


# --------------------------------------------------------------------------
# 来源 2：常见安装目录
# --------------------------------------------------------------------------
def _search_roots() -> list[Path]:
    roots = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.getenv(env_name)
        if base:
            roots.append(Path(base))

    roots.extend(
        [
            Path("C:/Program Files"),
            Path("C:/Program Files (x86)"),
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs",
            Path("/usr/local/stata"),
            Path("/Applications"),
        ]
    )
    return [r for r in roots if r and r.exists()]


def _from_common_dirs() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for root in _search_roots():
        try:
            entries = list(root.glob("Stata*"))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.lower().startswith("stata"):
                found.append((entry, "安装目录"))
                continue
            if not entry.is_dir():
                continue
            try:
                for exe in entry.glob("Stata*.exe"):
                    found.append((exe, f"安装目录（{entry.name}）"))
            except OSError:
                continue
    return found


# --------------------------------------------------------------------------
# 来源 3：PATH
# --------------------------------------------------------------------------
def _from_path() -> list[tuple[Path, str]]:
    found = []
    for name in ("stata-mp", "stata-se", "stata", "StataMP-64.exe", "StataSE-64.exe"):
        resolved = shutil.which(name)
        if resolved:
            found.append((Path(resolved), "PATH"))
    return found


def detect() -> list[dict]:
    """
    返回全部候选，按可信度与版本排序。

    每项：{"path": str, "source": str, "has_license": bool}
    """
    collected: list[tuple[Path, str]] = []
    for source in (_from_registry, _from_common_dirs, _from_path):
        try:
            collected.extend(source())
        except Exception:
            # 探测失败不该让程序起不来 —— 用户可以手动填路径
            continue

    unique: dict[str, dict] = {}
    for path, source in collected:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        key = str(resolved).lower()
        # 同一路径被多个来源发现时，保留先出现的（来源顺序即可信度顺序）
        if key not in unique:
            unique[key] = {
                "path": str(resolved),
                "source": source,
                "has_license": (resolved.parent / "STATA.LIC").exists()
                or (resolved.parent / "stata.lic").exists(),
                "_rank": _rank(resolved),
            }

    results = sorted(unique.values(), key=lambda item: item["_rank"])
    for item in results:
        item.pop("_rank", None)
    return results


def best_guess() -> str:
    """没配 stata_path 时用它兜底；探不到就返回空串。"""
    candidates = detect()
    return candidates[0]["path"] if candidates else ""


if __name__ == "__main__":
    for index, item in enumerate(detect(), 1):
        license_note = "已授权" if item["has_license"] else "未找到 STATA.LIC"
        print(f"{index}. {item['path']}")
        print(f"   来源：{item['source']}｜{license_note}")
    print("\n最佳猜测：", best_guess() or "（未探到，需要手动指定）")
