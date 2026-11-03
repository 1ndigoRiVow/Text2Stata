"""云端执行代理管理器：把 agent 并进本地版 GUI，零命令行（ISSUES.md #2）。

目标用户是经管学生和不写代码的老师，他们不该看到"终端""python""init"任何一个词。
这个模块在本地版设置面板和 LocalAgent 之间做桥：

    设置页「开启云端模式」→ manager.start(cloud_url, token)
        ├─ 线程 1：LocalAgent.serve_forever（轮询云端领任务、跑 Stata）
        └─ 线程 2：127.0.0.1:5100 小服务（云端网页直连这里上传 .dta）
    设置页「关闭」→ manager.stop()（等当前 Stata 任务跑完再退，不杀进程）

复用 agent/agent.py 的全部实现，不复制代码；CLI 形态（python -m agent.agent run）
继续可用，两种入口共存。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, paths


def _import_agent():
    """惰性导入 agent 包。backend 的 sys.path 里没有 repo 根，这里补上。

    frozen 模式下 agent 包由 spec 收进 _MEIPASS，paths.RESOURCE_ROOT 正好指向那里。
    """
    root = str(paths.RESOURCE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from agent.agent import (  # noqa: E402
        AgentConfig,
        DatasetRegistry,
        LocalAgent,
        StataWorker,
        create_local_app,
    )
    return AgentConfig, DatasetRegistry, LocalAgent, StataWorker, create_local_app


def _agent_data_dir() -> Path:
    """代理数据目录：跟 CLI 形态（python -m agent.agent）共用同一个。

    开发模式 = repo 根/agent_data（CLI 的默认位置，用户 CLI 注册过的数据集
    GUI 起来直接可用）；打包模式 = %APPDATA%/Text2Stata/agent_data，
    覆盖升级不丢已登记的数据集。两边的 .gitignore 都已覆盖。
    """
    base = paths.DATA_ROOT if paths.is_frozen() else paths.RESOURCE_ROOT
    d = base / "agent_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


class CloudAgentManager:
    """单线程生命周期的代理管理器（app.py 持有一个实例）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._poll_thread: threading.Thread | None = None
        self._http_server = None          # werkzeug 基础服务器，可 shutdown
        self._http_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._agent: "object | None" = None
        # 对外可见的状态快照（status() 返回）
        self.state = {
            "running": False,
            "cloud_url": "",
            "started_at": "",
            "last_error": "",
        }

    # ---------------- 生命周期 ----------------
    def start(self, cloud_url: str, token: str, local_port: int = 0) -> dict:
        """启动代理。cloud_url 形如 http://host:port；token 是云端发的 t2sa_..."""
        cloud_url = (cloud_url or "").strip().rstrip("/")
        token = (token or "").strip()
        if not cloud_url or not token:
            return {"status": "error", "message": "云端地址和代理令牌都要填。"}
        if self.state["running"]:
            if self.state["cloud_url"] == cloud_url:
                return {"status": "success", "message": "代理本来就在跑。"}
            self.stop()  # 换了云端地址，先停旧的

        AgentConfig, DatasetRegistry, LocalAgent, StataWorker, create_local_app = _import_agent()

        data_dir = _agent_data_dir()
        cfg = AgentConfig(data_dir)
        cfg.values["cloud_url"] = cloud_url
        cfg.values["token"] = token
        cfg.values["stata_path"] = config.STATA_PATH or ""
        if local_port:
            cfg.values["local_port"] = int(local_port)
        cfg.save()

        agent = LocalAgent(
            cfg,
            DatasetRegistry(data_dir),
            StataWorker(stata_path=config.STATA_PATH or None,
                        workspace=data_dir / "workspace"),
        )

        self._stop_event = threading.Event()

        # 线程 1：轮询云端（stop_event 可立刻打断等待）
        self._agent = agent
        self._poll_thread = threading.Thread(
            target=self._run_poll, args=(agent,), daemon=True, name="t2s-cloud-agent")
        self._poll_thread.start()

        # 线程 2：本机上传服务。不用 app.run()——它没法从外部停，
        # make_server 拿到的对象可以 shutdown()，"关闭云端模式"才干净。
        port = int(cfg.values.get("local_port") or 5100)
        # Windows 允许两个 socket 绑同一端口（SO_REUSEADDR 的坑），make_server
        # 端口被占也不报错、监听被先绑者接管——必须先探测，否则静默双代理。
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                probe.close()
                self.stop()  # 轮询线程已经先起了，端口起不来就得连它一起收掉
                return {"status": "error",
                        "message": f"本机端口 {port} 已有服务在跑——很可能你之前开过"
                                   f"代理（终端窗口还开着）。先把那个窗口关掉再开启。"}
        finally:
            probe.close()
        try:
            from werkzeug.serving import make_server
            self._http_server = make_server(
                "127.0.0.1", port, create_local_app(agent, cloud_url), threaded=True)
            self._http_thread = threading.Thread(
                target=self._http_server.serve_forever, daemon=True, name="t2s-agent-upload")
            self._http_thread.start()
        except OSError as exc:
            self.stop()
            return {"status": "error",
                    "message": f"本机端口 {port} 起不来（可能被占用）：{exc}"}

        self.state.update(running=True, cloud_url=cloud_url, started_at=_now(),
                          last_error="")
        return {"status": "success",
                "message": f"代理已启动，正在轮询 {cloud_url}，上传端口 {port}。"}

    def stop(self) -> dict:
        """停止代理。正在执行的 Stata 任务会跑完才退（不杀进程，避免半截产物）。"""
        with self._lock:
            self._stop_event.set()
            if self._http_server is not None:
                try:
                    self._http_server.shutdown()      # 停 serve_forever 循环
                    self._http_server.server_close()  # shutdown 不关 socket，端口要靠这句释放
                except Exception:
                    pass
                self._http_server = None
            self._poll_thread = None
            self._http_thread = None
            self.state.update(running=False, cloud_url="", started_at="")
            return {"status": "success", "message": "代理已停止。"}

    def _run_poll(self, agent):
        try:
            agent.serve_forever(stop_event=self._stop_event)
        except Exception as exc:  # 代理线程的异常不能带崩整个本地版
            self.state.update(running=False, last_error=str(exc))

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        snapshot = dict(self.state)
        snapshot["poll_alive"] = bool(
            self._poll_thread and self._poll_thread.is_alive())
        agent = self._agent
        if agent is not None and getattr(agent, "last_task", None):
            snapshot["last_task"] = agent.last_task
        return snapshot

    def autostart(self) -> None:
        """本地版启动时调用：配置里开了云端模式就自动把代理拉起来。"""
        if not config.CLOUD_ENABLED:
            return
        self.start(config.CLOUD_URL, config.CLOUD_TOKEN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
