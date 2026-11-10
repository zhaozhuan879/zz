"""Run one user's Web Agent tasks in a persistent folder on their own computer.

The packaged Windows companion includes Python, httpx and the Codex executable.
The pairing credential is project scoped and must stay on the user's computer.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import threading
import time
import zipfile

import httpx


EDITABLE = {".py", ".js", ".ts", ".tsx", ".html", ".css", ".txt", ".md",
            ".csv", ".json", ".yaml", ".yml", ".toml", ".ipynb"}
PRESERVE = {"node_modules", ".venv", ".venv-linux", ".cache", ".local-recovery"}
SKIP = PRESERVE | {"__pycache__", ".git", "dist", "build"}


def refresh_source(workspace: Path, archive_bytes: bytes) -> None:
    """Refresh platform source without deleting the user's installed packages."""
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / ".platform-sync.json"
    current = source_hashes(workspace)
    try:
        synced = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    except (OSError, json.JSONDecodeError):
        synced = {}
    if current and current != synced:
        recovery = workspace / ".local-recovery" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        for name in current:
            target = recovery / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workspace / name, target)
        print(f"Saved unsynced local files to {recovery}", flush=True)
    for base, dirs, files in os.walk(workspace, topdown=False, followlinks=False):
        folder = Path(base)
        if any(part in PRESERVE for part in folder.relative_to(workspace).parts):
            continue
        for name in files:
            (folder / name).unlink()
        for name in dirs:
            entry = folder / name
            if name in PRESERVE and entry.is_dir() and not entry.is_symlink():
                continue
            if entry.is_symlink():
                entry.unlink()
            else:
                try:
                    entry.rmdir()
                except OSError:
                    pass
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > 150 * 1024 * 1024:
            raise ValueError("project snapshot is too large")
        for item in entries:
            relative = PurePosixPath(item.filename)
            if (not relative.parts or relative.is_absolute() or item.is_dir() or
                    "\\" in item.filename or
                    any(part in {".", ".."} or part.startswith(".") or
                        re.search(r'[<>:"|?*\x00-\x1f]', part) for part in relative.parts) or
                    (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("unsafe project archive member")
            target = workspace.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    marker.write_text(json.dumps(source_hashes(workspace), ensure_ascii=False), encoding="utf-8")


def source_hashes(workspace: Path) -> dict[str, str]:
    hashes = {}
    for base, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in SKIP and not name.startswith(".")]
        for name in files:
            path = Path(base) / name
            if name.startswith(".") or path.is_symlink():
                continue
            hashes[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def changed_text(workspace: Path, before: dict[str, str]) -> dict[str, str | None]:
    after = source_hashes(workspace)
    result = {}
    total = 0
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        path = workspace / name
        if Path(name).suffix.lower() not in EDITABLE or name == "README.md":
            continue  # Binary outputs remain on the user's computer.
        if name not in after:
            result[name] = None
            continue
        if path.stat().st_size > 256 * 1024:
            raise ValueError(f"changed source file too large: {name}")
        content = path.read_text(encoding="utf-8")
        total += len(content.encode("utf-8"))
        if total > 2 * 1024 * 1024 or len(result) >= 100:
            raise ValueError("too many changed source bytes or files")
        result[name] = content
    return result


def codex_command(workspace: Path, task: dict, container: dict | None = None) -> list[str]:
    if container and task["sandbox"] != "workspace-write":
        raise RuntimeError("The selected container only supports project-write tasks")
    configured = os.getenv("RESEARCH_LOCAL_CODEX_BIN", "").strip()
    bundled_name = "codex.exe" if os.name == "nt" else "codex"
    bundle_root = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    bundled = Path(bundle_root) / "codex-bin" / bundled_name if bundle_root else None
    binary = (container["binary"] if container else
              configured or (str(bundled) if bundled and bundled.is_file() else "") or shutil.which("codex") or shutil.which("codex.cmd"))
    if not binary:
        raise RuntimeError("Codex CLI is not installed or is not on PATH")
    if os.name == "nt" and binary.lower().endswith(".cmd"):
        node = shutil.which("node")
        script = Path(binary).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if not node or not script.is_file():
            raise RuntimeError("Node.js or the Codex CLI JavaScript package is missing")
        command = [node, str(script)]
    else:
        command = [binary]
    gateway = task.get("model_gateway")
    if not isinstance(gateway, dict) or not gateway.get("base_url") or not gateway.get("token"):
        raise RuntimeError("平台没有提供模型网关；请更新本机客户端后重试")
    codex_workspace = container["workspace"] if container else str(workspace)
    sandbox = "danger-full-access" if container else task["sandbox"]
    command += ["exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox,
                "--cd", codex_workspace, "-c", f'model_reasoning_effort="{task["effort"]}"']
    if os.name == "nt":
        command += ["-c", "features.prefer_mxc=true"]
    provider = {"model_provider": "research", "model_providers.research.name": "Research platform Qwen",
                "model_providers.research.base_url": gateway["base_url"],
                "model_providers.research.env_key": "RESEARCH_AGENT_MODEL_TOKEN",
                "model_providers.research.wire_api": "responses",
                "model_providers.research.requires_openai_auth": False,
                "model_providers.research.supports_websockets": False}
    for key, value in provider.items():
        command += ["-c", f"{key}={json.dumps(value)}"]
    if task.get("model"):
        command += ["--model", task["model"]]
    command += ["-"]
    if container:
        return ["docker", "exec", "-i", "-u", f"{os.getuid()}:{os.getgid()}", "-w", codex_workspace,
                "-e", "RESEARCH_AGENT_MODEL_TOKEN", "-e", f"CODEX_HOME={container['codex_home']}",
                "-e", f"HOME={container['codex_home']}",
                container["name"], *command]
    return command


class Companion:
    def __init__(self, config: dict, root: Path):
        self.project_id = config["project_id"]
        if not re.fullmatch(r"p_[0-9a-f]{12}", self.project_id):
            raise ValueError("invalid project id")
        server = config["server"].rstrip("/")
        if not server.startswith(("https://", "http://127.0.0.1:", "http://localhost:", "http://10.")):
            raise ValueError("server must use HTTPS or an explicitly private address")
        self.client = httpx.Client(base_url=server,
            headers={"Authorization": f"Bearer {config['token']}"}, timeout=30)
        self.prefix = f"/api/companion/{self.project_id}"
        self.workspace = root.expanduser().resolve() / self.project_id
        self.container = config.get("container")

    def request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, self.prefix + path, **kwargs)
        response.raise_for_status()
        return response

    def work_once(self) -> bool:
        self.request("POST", "/heartbeat", json={"protocol_version": 2})
        claim = self.request("POST", "/claim").json()
        task_id = claim.get("task_id")
        if not task_id:
            return False
        claim_id = claim["claim_id"]
        process = None
        stop = threading.Event()

        def keep_alive():
            while not stop.wait(5):
                try:
                    self.request("POST", "/heartbeat", json={"task_id": task_id, "protocol_version": 2})
                    state = self.request("GET", f"/status/{task_id}").json()["status"]
                    if state != "running" and process and process.poll() is None:
                        process.terminate()
                        return
                except Exception as exc:
                    print(f"Heartbeat error: {exc}", file=sys.stderr)

        heartbeat = threading.Thread(target=keep_alive, daemon=True)
        heartbeat.start()
        exit_code, stderr, changes, before = 1, "", {}, None
        try:
            archive = self.request("GET", f"/archive/{task_id}", params={"claim_id": claim_id}).content
            refresh_source(self.workspace, archive)
            before = source_hashes(self.workspace)
            command = codex_command(self.workspace, claim, self.container)
            environment = os.environ.copy()
            environment["RESEARCH_AGENT_MODEL_TOKEN"] = claim["model_gateway"]["token"]
            print(f"Running {task_id} in {self.workspace}", flush=True)
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_log:
                # The companion runs in the background; keep the Codex console hidden on Windows.
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                with subprocess.Popen(command, cwd=self.workspace, env=environment, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=error_log,
                                      text=True, encoding="utf-8", errors="replace", bufsize=1,
                                      creationflags=creationflags) as process:
                    process.stdin.write(claim["prompt"])
                    process.stdin.close()
                    turn_completed = False
                    for line in process.stdout:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if len(line.encode("utf-8")) > 60_000:
                            event = {"type": "local.output.truncated", "bytes": len(line.encode("utf-8"))}
                        self.request("POST", f"/events/{task_id}", json={"claim_id": claim_id, "event": event})
                        print(event.get("type", "event"), flush=True)
                        if event.get("type") == "turn.completed":
                            turn_completed = True
                            break
                    if turn_completed:
                        # Codex has finished the answer. Its CLI process can keep stdout
                        # open for minutes while shutting down, so bound that cleanup.
                        try:
                            exit_code = process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            if os.name == "nt":
                                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                               capture_output=True, timeout=8)
                            else:
                                process.terminate()
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=3)
                            exit_code = 0
                    else:
                        exit_code = process.wait()
                error_log.seek(0)
                stderr = error_log.read()[-4000:]
            changes = changed_text(self.workspace, before)
        except Exception as exc:
            stderr = f"{type(exc).__name__}: {exc}"
            print(stderr, file=sys.stderr)
            if before is not None:
                try:
                    changes = changed_text(self.workspace, before)
                except Exception as change_error:
                    stderr += f"; source sync failed: {change_error}"
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        try:
            result = self.request("POST", f"/result/{task_id}", json={
                "claim_id": claim_id, "exit_code": exit_code,
                "stderr": stderr, "changes": changes}).json()
            print(f"{task_id}: {result['status']}", flush=True)
            after = source_hashes(self.workspace)
            unsynced = before is not None and any(
                before.get(name) != after.get(name) and name not in changes
                for name in before.keys() | after.keys()
            )
            if unsynced:
                print("Some local files were not synced; they will be backed up before the next task.", flush=True)
            if result.get("synced") and not unsynced:
                (self.workspace / ".platform-sync.json").write_text(
                    json.dumps(source_hashes(self.workspace), ensure_ascii=False), encoding="utf-8")
        except httpx.HTTPError as exc:
            print(f"Could not report {task_id}: {exc}", file=sys.stderr)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Codex execution companion")
    parser.add_argument("--config", type=Path, required=True, help="pairing JSON downloaded from the Web platform")
    parser.add_argument("--root", type=Path, default=Path.home() / "ResearchWorkbench" / "projects")
    parser.add_argument("--once", action="store_true", help="claim at most one task, then exit")
    options = parser.parse_args()
    config = json.loads(options.config.read_text(encoding="utf-8"))
    companion = Companion(config, options.root)
    print(f"Connected project {companion.project_id}; files: {companion.workspace}", flush=True)
    while True:
        try:
            worked = companion.work_once()
        except (httpx.HTTPError, OSError) as exc:
            print(f"Connection error: {exc}", file=sys.stderr)
            worked = False
        if options.once:
            break
        if not worked:
            time.sleep(5)


if __name__ == "__main__":
    main()
