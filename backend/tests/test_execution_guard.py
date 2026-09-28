"""
执行判定与变量校验的回归测试（不需要真实 Stata 也能跑，除最后的路径自检外）。

背景：2026-09 本机 Stata 学校授权到期，Stata 只打印授权横幅就退出。
旧实现靠"日志里没有 r(***) 就算成功"判定，导致系统长期**假装执行成功**。
本测试锁死该场景，防止判定逻辑退回。

运行方式（在 backend 目录下）：
    ../.venv/Scripts/python.exe tests/test_execution_guard.py
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.config import STATA_PATH  # noqa: E402
from core.stata_worker import StataWorker  # noqa: E402

import app as backend_app  # noqa: E402

passed = failed = 0


def check(name, got, expect):
    global passed, failed
    good = got == expect
    passed += good
    failed += (not good)
    print(f"{'PASS' if good else 'FAIL'}  {name}")
    if not good:
        print(f"      实际={got!r}  期望={expect!r}")


def main():
    worker = StataWorker(stata_path=STATA_PATH)
    tmp_log = Path(worker.workspace) / "_guard_case.log"

    print("=" * 78)
    print("一、执行判定：默认判失败，只有正向证据才判成功")
    print("=" * 78)
    cases = [
        ("正常跑完", 0, "sysuse auto\n. sum price\n\nend of do-file\n", True, None),
        ("脚本内有报错 r(111)", 0,
         "use x\nvariable notexistvar not found\nr(111);\n\nend of do-file\nr(111);\n",
         False, "r(111)"),
        ("授权过期（只有横幅，无 end of do-file）", 0,
         "\nStata license: 100-student lab, expiring 11 Jul 2026\nSerial number: 4019\n"
         "  Licensed to: Rachel Wang\n               Shanghai University of Finance and Economics\n",
         False, "stata_license"),
        ("日志为空（Stata 启动即失败）", 0, "   \n\n", False, "log_empty"),
        ("日志缺少结束标记（中途中断）", 0, "sysuse auto, clear\n. sum price\n", False, "not_executed"),
        ("进程退出码非 0", 3, "end of do-file\n", False, "exit_code_3"),
    ]
    for name, rc, log, expect_ok, expect_code in cases:
        tmp_log.write_text(log, encoding="utf-8")
        ok, code, msg, _ = worker._judge(rc, tmp_log, log)
        check(f"{name} → {'成功' if ok else '失败'} / {code}", (ok, code), (expect_ok, expect_code))
    tmp_log.unlink(missing_ok=True)

    # 日志文件根本不存在
    missing = Path(worker.workspace) / "_guard_missing.log"
    missing.unlink(missing_ok=True)
    ok, code, _, _ = worker._judge(0, missing, "")
    check(f"日志文件不存在 → {code}", (ok, code), (False, "log_missing"))

    print()
    print("=" * 78)
    print("二、模板 required_globals 校验")
    print("=" * 78)
    fe_info = backend_app._validate_template("fe")
    desc_info = backend_app._validate_template("descriptive_stats")
    checks = [
        ("fe + 完整映射", fe_info, {"y": "tfp", "x": "focus", "id": "id", "time": "year", "controls": ["lev"]}, []),
        ("fe + 全空映射（真实踩到的坑）", fe_info, {"y": "", "x": "", "id": "", "time": "", "controls": []},
         ["y", "x", "id", "time"]),
        ("fe + 缺 time", fe_info, {"y": "tfp", "x": "focus", "id": "id", "time": "", "controls": []}, ["time"]),
        ("descriptive_stats + 只需 y/x", desc_info, {"y": "tfp", "x": "focus", "controls": []}, []),
        ("descriptive_stats + 缺 x", desc_info, {"y": "tfp", "x": "", "controls": []}, ["x"]),
    ]
    for name, info, mapping, expect in checks:
        check(f"{name} → 缺 {backend_app._missing_required_globals(info, mapping)}",
              backend_app._missing_required_globals(info, mapping), expect)

    print()
    print("=" * 78)
    print("三、Stata 路径自检")
    print("=" * 78)
    env_ok, env_msg = worker.check_environment()
    check(f"{env_msg}", env_ok, True)

    print()
    print(f"=== 结果：{passed} 通过 / {failed} 失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
