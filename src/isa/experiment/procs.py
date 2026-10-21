"""Child processes for experiment runs, and port checks for preconditions and orphans."""

import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is listening. Used both before a run (is the port
    free?) and after it (did a replica escape cleanup?)."""
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


@dataclass
class Child:
    name: str
    proc: subprocess.Popen
    log_path: Path


def start_child(name: str, module: str, args: list[str], log_path: Path) -> Child:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ISA_LOG_FORMAT": "json"}  # logs are data for the analysis
    with log_path.open("ab") as f:
        proc = subprocess.Popen(
            [sys.executable, "-m", module, *args],
            stdout=f,
            stderr=subprocess.STDOUT,
            env=env,
            # Own process group: a Ctrl-C to the runner must not hit the children
            # directly. The runner stops them itself, in order, in its finally block.
            start_new_session=True,
        )
    return Child(name, proc, log_path)


def stop_child(child: Child, timeout_s: float = 30.0) -> int:
    """SIGINT first: uvicorn treats it as a graceful shutdown and runs the app's
    lifespan teardown, which for the controller terminates its replicas."""
    if child.proc.poll() is None:
        child.proc.send_signal(signal.SIGINT)
        try:
            child.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            child.proc.kill()
            child.proc.wait()
    return child.proc.returncode