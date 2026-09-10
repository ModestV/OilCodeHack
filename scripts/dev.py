"""Run local API and Vite; shut down both children together on Ctrl-C."""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def free(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    api_port = args.api_port
    while not free(api_port):
        api_port += 1
    ui_port = args.port
    while not free(ui_port) or ui_port == api_port:
        ui_port += 1
    env = {**os.environ, "VITE_API_TARGET": f"http://127.0.0.1:{api_port}"}
    children = []
    try:
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=root,
            )
        )
        children.append(
            subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ui_port),
                    "--strictPort",
                ],
                cwd=root / "frontend",
                env=env,
            )
        )
        print(
            f"Нефтекод: http://127.0.0.1:{ui_port} | API: http://127.0.0.1:{api_port}",
            flush=True,
        )
        while all(c.poll() is None for c in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
