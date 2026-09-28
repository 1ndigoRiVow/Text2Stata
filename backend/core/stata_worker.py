import os
import re
import subprocess
import time
import uuid
from pathlib import Path


# Stata 把 do-file 跑到结尾时会打印这个标记；授权失败/未执行时不会出现。
_COMPLETION_MARKER = re.compile(r"end of do-file", re.IGNORECASE)

# Stata 报错的标准格式：r(198); / r(2000);
_ERROR_CODE = re.compile(r"r\(([0-9]+)\);?")

# 授权类问题：这类失败只打印授权横幅，日志里不会出现 r(***)，必须单独识别，
# 否则会被误判成"执行成功"（2026-09 曾真实发生过：学校授权到期后系统一直报成功）。
_LICENSE_HINT = re.compile(
    r"(license\s+(?:has\s+)?expir"
    r"|expir(?:e|ed|es|ing|y)\s+\d"
    r"|STATA\.LIC"
    r"|not\s+licensed"
    r"|license\s+is\s+invalid"
    r"|unable\s+to\s+obtain\s+a\s+license"
    r"|license\s+has\s+expired)",
    re.IGNORECASE,
)


class StataWorker:
    """
    Text2Stata 核心执行引擎
    负责在后台调用 Stata 命令行，执行 .do 脚本，并捕获日志和报错。

    判定原则：**默认判失败，只有拿到正向证据才判成功。**
    正向证据 = 进程退出码为 0 + 日志存在且非空 + 日志中出现 do-file 结束标记 + 无 r(***) 报错。
    """

    # 会被当作"本次任务产物"收集的扩展名
    ARTIFACT_SUFFIXES = (".rtf", ".doc", ".docx", ".csv", ".png", ".gph")

    def __init__(self, stata_path=None):
        # 1. 确定工作目录 (用于存放临时的 do 文件和 log 文件)
        self.workspace = Path(__file__).parent.parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # 2. 配置 Stata 执行文件路径
        # Windows 用户通常填具体路径，如 "C:/Program Files/StataNow19/StataSE-64.exe"
        # Mac/Linux 用户通常是 "stata-mp" 或 "stata"
        self.stata_path = stata_path or self._detect_default_stata()

    def _detect_default_stata(self):
        """简单探测系统默认的 stata 命令 (这是一个 fallback 机制)"""
        if os.name == "nt":  # Windows
            return "StataMP-64.exe"
        return "stata-mp"

    def check_environment(self):
        """
        执行前的环境自检：Stata 可执行文件是否真的存在。
        返回 (ok, message)。不依赖真正跑一次 Stata，避免把授权问题拖到执行阶段才发现。
        """
        path = Path(self.stata_path) if self.stata_path else None
        # 只写文件名（无路径分隔符）时交给系统 PATH 去解析，这里不误报
        if path and (path.parent != Path(".") or os.sep in str(self.stata_path)):
            if not path.exists():
                return False, f"找不到 Stata 可执行文件：{self.stata_path}"
        return True, "Stata 路径检查通过。"

    def execute_script(self, do_file_content, task_id=None):
        """
        核心执行方法
        :param do_file_content: 完整的 Stata 代码字符串
        :param task_id: 任务唯一标识，如果不传则自动生成
        :return: 字典包含执行状态、日志内容、报错信息等
        """
        # 生成唯一任务ID，避免并发时文件覆盖
        if not task_id:
            task_id = f"task_{uuid.uuid4().hex[:8]}"

        do_file_path = self.workspace / f"{task_id}.do"
        log_file_path = self.workspace / f"{task_id}.log"

        # 1. 写 .do 文件
        with open(do_file_path, "w", encoding="utf-8") as f:
            f.write(do_file_content)

        # 2. 清掉同名旧日志，否则可能读到上一次的残留内容（Stata 是追加写日志的）
        if log_file_path.exists():
            try:
                log_file_path.unlink()
            except OSError:
                pass

        # 3. 执行前给工作区拍快照，用于之后做差集，只认本次新产生的产物
        before_snapshot = self._snapshot_artifacts()

        # 4. 构建命令行指令
        # -e: batch mode (静默执行，不弹窗)
        # -q: suppress logo (跳过启动 logo 加快速度)
        # do: 执行 do 文件
        cmd = [self.stata_path, "-e", "-q", "do", str(do_file_path)]

        start_time = time.time()

        try:
            # 5. 阻塞式执行 Stata 进程，设置超时时间防卡死 (比如 5 分钟)
            # 注意：cwd 设置为 workspace，这样 Stata 生成的各种图表/表格默认也会保存在这里
            process = subprocess.run(
                cmd,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return self._error_result(task_id, "timeout", "执行超时（超过 300 秒），任务已终止。",
                                      time_cost=round(time.time() - start_time, 2))
        except FileNotFoundError:
            return self._error_result(
                task_id, "stata_not_found",
                f"找不到 Stata 启动程序，请检查路径：{self.stata_path}",
                time_cost=round(time.time() - start_time, 2),
            )
        except Exception as e:
            return self._error_result(task_id, "unknown", f"系统未知错误：{e}",
                                      time_cost=round(time.time() - start_time, 2))

        execution_time = time.time() - start_time

        # 6. 读日志 + 判定结果（默认失败，需正向证据）
        log_content = self._read_log(log_file_path)
        ok, error_code, message, diagnosis = self._judge(
            returncode=process.returncode,
            log_file_path=log_file_path,
            log_content=log_content,
        )

        # 7. 只收集本次任务新产生 / 被重写的成果文件
        generated_files = self._collect_new_artifacts(before_snapshot)

        result = {
            "status": "success" if ok else "failed",
            "task_id": task_id,
            "message": message,
            "log": log_content,
            "time_cost": round(execution_time, 2),
            "files": generated_files,
            "return_code": process.returncode,
        }
        if not ok:
            result["error_code"] = error_code
            result["diagnosis"] = diagnosis
        return result

    # ------------------------------------------------------------------
    # 判定逻辑
    # ------------------------------------------------------------------
    def _judge(self, returncode, log_file_path, log_content):
        """
        判定执行结果。
        :return: (ok, error_code, message, diagnosis)
        默认判失败，只有全部正向证据都成立才判成功。
        """
        # (1) 进程退出码异常
        if returncode != 0:
            return (
                False,
                f"exit_code_{returncode}",
                f"Stata 进程异常退出（退出码 {returncode}），结果不可信。",
                "Stata 进程未正常结束，可能是授权、安装或运行环境问题。",
            )

        # (2) 日志缺失或为空 —— 没有日志就没有任何"执行过"的证据
        if not log_file_path.exists():
            return (
                False,
                "log_missing",
                "未生成 Stata 日志，无法确认脚本是否执行。",
                "Stata 可能未能启动，或执行目录不可写。",
            )
        if not log_content.strip():
            return (
                False,
                "log_empty",
                "Stata 日志为空，脚本未真正执行。",
                "Stata 启动了但没有产生任何输出，通常是启动阶段就失败了。",
            )

        # (3) 授权类问题：只打印授权横幅，不会出现 r(***)
        if _LICENSE_HINT.search(log_content):
            return (
                False,
                "stata_license",
                "Stata 授权无效或已过期，脚本未被执行。",
                "请检查 Stata 许可证：学校/机构授权到期需要续期或更换序列号。",
            )

        # (4) 正向证据：do-file 必须真的跑到结尾
        if not _COMPLETION_MARKER.search(log_content):
            return (
                False,
                "not_executed",
                "日志中缺少 do-file 结束标记，脚本没有执行完。",
                "脚本中途中断，或 Stata 只打印了启动信息就退出。",
            )

        # (5) 显式报错码
        match = _ERROR_CODE.search(log_content)
        if match:
            return (
                False,
                f"r({match.group(1)})",
                "Stata 执行报错，请检查代码或数据。",
                "脚本已执行但中断在报错处，请查看日志中 r() 附近的上下文。",
            )

        return True, None, "执行成功", None

    def _read_log(self, log_file_path):
        """读取日志文件内容；不存在时返回空串。"""
        if not log_file_path.exists():
            return ""
        with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def _error_result(self, task_id, error_code, message, time_cost=None, diagnosis=None):
        """构造统一的失败返回结构。"""
        result = {
            "status": "error",
            "task_id": task_id,
            "error_code": error_code,
            "message": message,
            "log": "",
            "files": [],
        }
        if time_cost is not None:
            result["time_cost"] = time_cost
        if diagnosis:
            result["diagnosis"] = diagnosis
        return result

    # ------------------------------------------------------------------
    # 产物隔离
    # ------------------------------------------------------------------
    def _snapshot_artifacts(self):
        """记录当前工作区内成果文件的 mtime，用于执行后做差集。"""
        snapshot = {}
        try:
            names = os.listdir(self.workspace)
        except OSError:
            return snapshot
        for name in names:
            if not name.endswith(self.ARTIFACT_SUFFIXES):
                continue
            try:
                snapshot[name] = (self.workspace / name).stat().st_mtime_ns
            except OSError:
                pass
        return snapshot

    def _collect_new_artifacts(self, before_snapshot):
        """
        只统计本次任务新产生或被重写（mtime 变化）的成果文件。
        原实现扫描整个 workspace，会把历史文件和并发任务的产物算进本次结果。
        """
        produced = []
        try:
            names = os.listdir(self.workspace)
        except OSError:
            return produced
        for name in names:
            if not name.endswith(self.ARTIFACT_SUFFIXES):
                continue
            try:
                mtime = (self.workspace / name).stat().st_mtime_ns
            except OSError:
                continue
            if before_snapshot.get(name) != mtime:
                produced.append(name)
        return sorted(produced)


# ==========================================
# 本地测试模块
# ==========================================
if __name__ == "__main__":
    print("=== 开始测试 Stata 执行引擎 ===\n")

    LOCAL_STATA_PATH = r"C:\Program Files\StataNow19\StataSE-64.exe"

    worker = StataWorker(stata_path=LOCAL_STATA_PATH)

    env_ok, env_msg = worker.check_environment()
    print(f"环境自检: {'通过' if env_ok else '失败'} - {env_msg}")
    print(f"当前工作区: {worker.workspace}\n")

    # --- 用例 1：正常脚本，应判成功 ---
    good_code = "sysuse auto, clear\nsum price mpg\nregress price mpg rep78, robust\n"
    print("正在呼叫 Stata 执行回归测试...\n")
    result = worker.execute_script(good_code, task_id="selftest_ok")
    print(f"[用例1 正常脚本] 状态={result['status']} 耗时={result.get('time_cost')}s "
          f"退出码={result.get('return_code')} 产物={result.get('files')}")
    if result["status"] == "success":
        print("  日志最后 300 字符：")
        print("  " + result["log"][-300:].replace("\n", "\n  "))
    else:
        print("  message:", result.get("message"), "| error_code:", result.get("error_code"))

    # --- 用例 2：错误脚本，应判失败并抓到 r() ---
    bad_code = "sysuse auto, clear\nregress price notexistvar\n"
    result2 = worker.execute_script(bad_code, task_id="selftest_bad")
    print(f"\n[用例2 报错脚本] 状态={result2['status']} error_code={result2.get('error_code')} "
          f"diagnosis={result2.get('diagnosis')}")

    # --- 用例 3：伪造"授权过期"日志，验证不再误判成功 ---
    fake_log = Path(worker.workspace) / "selftest_license.log"
    fake_log.write_text(
        "\nStata license: 100-student lab, expiring 11 Jul 2026\n"
        "Serial number: 401909202974\n"
        "  Licensed to: Someone\n               Some University\n",
        encoding="utf-8",
    )
    ok, code, msg, diag = worker._judge(0, fake_log, fake_log.read_text(encoding="utf-8"))
    print(f"\n[用例3 伪造授权过期日志] 判定={'成功(错误!)' if ok else '失败(正确)'} "
          f"error_code={code}")
    print(f"  message: {msg}\n  diagnosis: {diag}")
    fake_log.unlink()
