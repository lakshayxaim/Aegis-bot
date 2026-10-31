"""Start Aegis: one process holding the model client, the tools, the API and the window."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time

import uvicorn

from aegis import config


def _setup_logging() -> None:
    config.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOG_DIR / "aegis.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _free_port(preferred: int) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free local port for Aegis.")


def _start_server(runtime, port: int) -> uvicorn.Server:
    from aegis.server import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(runtime), host="127.0.0.1", port=port, log_level="warning", access_log=False, log_config=None, lifespan="on", timeout_graceful_shutdown=3))
    thread = threading.Thread(target=server.run, daemon=True, name="aegis-server")
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("The Aegis server did not start. See the log file.")
    config.RUNTIME_PATH.write_text(json.dumps({"port": port, "token": config.API_TOKEN, "pid": os.getpid()}), encoding="utf-8")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="aegis", description="Aegis, a private desktop assistant.")
    parser.add_argument("--serve", action="store_true", help="run only the API and UI server, without the native window")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    args = parser.parse_args()
    _setup_logging()
    logger = logging.getLogger("aegis")
    sys.excepthook = lambda kind, value, trace: logger.critical("uncaught exception", exc_info=(kind, value, trace))

    if args.serve:
        from aegis.server import Runtime

        runtime = Runtime()
        runtime.port = _free_port(args.port)
        server = _start_server(runtime, runtime.port)
        print(f"Aegis is serving http://127.0.0.1:{runtime.port}", flush=True)
        try:
            while server.started and not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True
        time.sleep(1)
        return

    from aegis.shell import Shell, already_running

    if already_running():
        return
    shell = Shell()
    from aegis.server import Runtime

    runtime = Runtime(shell=shell)
    runtime.port = _free_port(args.port)
    server = _start_server(runtime, runtime.port)
    code = shell.run(f"http://127.0.0.1:{runtime.port}", runtime)
    server.should_exit = True
    time.sleep(0.8)
    os._exit(code)  # Qt and worker threads have nothing left to flush


if __name__ == "__main__":
    main()
