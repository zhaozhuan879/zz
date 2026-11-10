"""macOS launcher and launchd worker for the research workbench companion.

The .app receives researchworkbench:// links. A separate, self-contained
worker binary runs under the logged-in user's launchd session.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import plistlib

import httpx

from local_companion import Companion


LABEL = "com.yunzhengdata.researchcompanion"
SUPPORT = Path.home() / "Library" / "Application Support" / "ResearchWorkbench" / "Companion"
BINDINGS = SUPPORT / "bindings"
WORKER = SUPPORT / "ResearchCompanionWorker"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_FILE = SUPPORT / "companion.log"


def _ensure_private_dirs() -> None:
    BINDINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SUPPORT, 0o700)
    os.chmod(BINDINGS, 0o700)


def _worker_resource() -> Path:
    # PyInstaller's app executable lives in Contents/MacOS; this is copied by
    # build_macos_companion.sh to Contents/Resources.
    return Path(sys.executable).resolve().parent.parent / "Resources" / "ResearchCompanionWorker"


def install_worker() -> None:
    source = _worker_resource()
    if not source.is_file():
        raise RuntimeError("安装包缺少后台执行器，请重新下载安装")
    _ensure_private_dirs()
    def digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    needs_update = not WORKER.is_file() or digest(source) != digest(WORKER)
    if needs_update:
        temporary = SUPPORT / "ResearchCompanionWorker.new"
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o755)
        os.replace(temporary, WORKER)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "Label": LABEL,
        "ProgramArguments": [str(WORKER), "--background"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(SUPPORT / "launchd.out.log"),
        "StandardErrorPath": str(SUPPORT / "launchd.err.log"),
    }
    temporary_plist = LAUNCH_AGENT.with_suffix(".new")
    temporary_plist.write_bytes(plistlib.dumps(job))
    os.replace(temporary_plist, LAUNCH_AGENT)
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not loaded:
        subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT)], check=True)
    elif needs_update:
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)


def pair(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme != "researchworkbench" or parsed.netloc != "pair":
        raise ValueError("无效的本机绑定链接")
    query = parse_qs(parsed.query, strict_parsing=True)
    server = query["server"][0].rstrip("/")
    project_id = query["project_id"][0]
    code = query["code"][0]
    if not re.fullmatch(r"p_[0-9a-f]{12}", project_id) or len(code) < 24:
        raise ValueError("无效的项目绑定凭证")
    if not server.startswith(("https://", "http://10.", "http://127.0.0.1:", "http://localhost:")):
        raise ValueError("平台地址必须是 HTTPS 或受信任的内网地址")
    # Prepare the agent before consuming the single-use pairing code.
    install_worker()
    response = httpx.post(f"{server}/api/companion/{project_id}/redeem", json={"code": code}, timeout=20)
    response.raise_for_status()
    token = response.json()["token"]
    temporary = BINDINGS / f"{project_id}.new"
    temporary.write_text(json.dumps({"server": server, "project_id": project_id, "token": token}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, BINDINGS / f"{project_id}.json")
    return project_id


def _logger() -> logging.Logger:
    _ensure_private_dirs()
    log = logging.getLogger("ResearchWorkbenchCompanion")
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


def background() -> None:
    os.umask(0o077)
    _ensure_private_dirs()
    with (SUPPORT / "agent.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        log = _logger()
        sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        log.info("macOS agent started")
        workers: dict[str, tuple[threading.Thread, threading.Event, str]] = {}

        def run_binding(path: Path, stop: threading.Event) -> None:
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                companion = Companion(config, Path.home() / "ResearchWorkbench" / "projects")
            except Exception:
                log.exception("Invalid binding for %s", path.stem)
                return
            while not stop.is_set():
                try:
                    worked = companion.work_once()
                    if worked:
                        log.info("Task finished for %s", path.stem)
                except Exception:
                    log.exception("Connection/task error for %s", path.stem)
                    worked = False
                if not worked:
                    stop.wait(5)

        while True:
            for path in BINDINGS.glob("p_*.json"):
                try:
                    stamp = str(path.stat().st_mtime_ns)
                except OSError:
                    continue
                existing = workers.get(path.stem)
                if existing and existing[0].is_alive() and existing[2] == stamp:
                    continue
                if existing:
                    existing[1].set()
                    if existing[0].is_alive():
                        continue
                stop = threading.Event()
                thread = threading.Thread(target=run_binding, args=(path, stop), daemon=True)
                workers[path.stem] = (thread, stop, stamp)
                thread.start()
                log.info("Binding active: %s", path.stem)
            time.sleep(3)


def show_message(message: str) -> None:
    # Messages are static except for a validated 14-character project ID.
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display dialog "{safe}" buttons {{"好"}} default button "好"'], check=False)


def main() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("macOS 客户端只能在 Mac 上运行")
    if len(sys.argv) >= 2 and sys.argv[1] == "--background":
        background()
        return
    try:
        if len(sys.argv) >= 2 and sys.argv[1].startswith("researchworkbench://"):
            project_id = pair(sys.argv[1])
            show_message(f"项目 {project_id} 已连接，助手会在后台运行并在登录电脑后自动启动。")
        elif len(sys.argv) >= 3 and sys.argv[1] == "--pair":
            project_id = pair(sys.argv[2])
            show_message(f"项目 {project_id} 已连接，助手会在后台运行并在登录电脑后自动启动。")
        else:
            install_worker()
            show_message("本机助手已安装并在后台启动。现在回到网页，点击“连接本机”。")
    except Exception as exc:
        show_message(f"本机助手操作失败：{exc}")


if __name__ == "__main__":
    main()
