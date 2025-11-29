#!/usr/bin/env python3
"""
process_api - Init process for sandbox containers.

Runs as PID 1, listens on port 2024 for command execution requests.
Similar to Claude's /process_api binary.

Endpoints:
    POST /exec          - Execute command, return JSON result
    POST /exec/stream   - Execute command, stream output via SSE
    GET  /health        - Health check

Usage:
    ./process_api --addr 0.0.0.0:2024 --memory-limit-bytes 4294967296
"""

import argparse
import asyncio
import json
import logging
import os
import resource
import signal
import sys
from asyncio.subprocess import PIPE
from http import HTTPStatus

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ProcessAPI:
    def __init__(self, memory_limit_bytes: int | None = None):
        self._shutdown_event = asyncio.Event()
        self._apply_memory_limit(memory_limit_bytes)

    def _apply_memory_limit(self, memory_limit_bytes: int | None) -> None:
        """Apply memory limit using resource limits."""
        if memory_limit_bytes is None:
            return

        try:
            # Set both soft and hard limits for virtual memory (RLIMIT_AS)
            # This limits the total address space available to the process
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
            logger.info(f"Memory limit set: {memory_limit_bytes} bytes")
        except OSError as e:
            logger.warning(f"Could not set memory limit: {e}")

    async def health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})

    async def exec_command(self, request: web.Request) -> web.Response:
        """Execute a command and return the result."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST
            )

        command = body.get("command")
        if not command:
            return web.json_response(
                {"error": "Missing 'command' field"}, status=HTTPStatus.BAD_REQUEST
            )

        workdir = body.get("workdir", "/workspace")
        timeout = body.get("timeout", 30)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=PIPE,
                stderr=PIPE,
                cwd=workdir,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                return web.json_response({
                    "exit_code": process.returncode,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "timed_out": False,
                })
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return web.json_response({
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "Command timed out",
                    "timed_out": True,
                })

        except Exception as e:
            return web.json_response({
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "timed_out": False,
            })

    async def exec_stream(self, request: web.Request) -> web.StreamResponse:
        """Execute a command and stream output via Server-Sent Events."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST
            )

        command = body.get("command")
        if not command:
            return web.json_response(
                {"error": "Missing 'command' field"}, status=HTTPStatus.BAD_REQUEST
            )

        workdir = body.get("workdir", "/workspace")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=PIPE,
                stderr=PIPE,
                cwd=workdir,
            )

            async def read_stream(stream, stream_type: str):
                """Read from a stream and yield SSE events."""
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    data = chunk.decode("utf-8", errors="replace")
                    event = {
                        "type": stream_type,
                        "data": data,
                    }
                    await response.write(
                        f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    )

            # Read stdout and stderr concurrently
            await asyncio.gather(
                read_stream(process.stdout, "stdout"),
                read_stream(process.stderr, "stderr"),
            )

            await process.wait()

            # Send exit event
            exit_event = {
                "type": "exit",
                "exit_code": process.returncode,
            }
            await response.write(f"data: {json.dumps(exit_event)}\n\n".encode("utf-8"))

        except Exception as e:
            error_event = {
                "type": "error",
                "data": str(e),
            }
            await response.write(f"data: {json.dumps(error_event)}\n\n".encode("utf-8"))

        await response.write_eof()
        return response

    async def write_file(self, request: web.Request) -> web.Response:
        """Write content to a file."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST
            )

        path = body.get("path")
        content = body.get("content")
        mode = body.get("mode", "w")

        if not path or content is None:
            return web.json_response(
                {"error": "Missing 'path' or 'content' field"},
                status=HTTPStatus.BAD_REQUEST,
            )

        # Security: only allow writes to /workspace or /mnt/user-data/outputs
        if not path.startswith("/"):
            path = f"/workspace/{path}"

        # Resolve symlinks and normalize path to prevent traversal attacks
        # (e.g., /workspace/../../../etc/passwd)
        resolved_path = os.path.realpath(path)

        allowed_prefixes = ["/workspace", "/mnt/user-data/outputs"]
        if not any(resolved_path.startswith(p) for p in allowed_prefixes):
            return web.json_response(
                {"error": "Path not allowed"}, status=HTTPStatus.FORBIDDEN
            )

        try:
            # Ensure parent directory exists
            os.makedirs(os.path.dirname(resolved_path), exist_ok=True)

            write_mode = "w" if mode == "w" else "a"
            with open(resolved_path, write_mode, encoding="utf-8") as f:
                f.write(content)

            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    async def read_file(self, request: web.Request) -> web.Response:
        """Read content from a file."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST
            )

        path = body.get("path")
        if not path:
            return web.json_response(
                {"error": "Missing 'path' field"}, status=HTTPStatus.BAD_REQUEST
            )

        if not path.startswith("/"):
            path = f"/workspace/{path}"

        # Resolve symlinks and normalize path to prevent traversal attacks
        resolved_path = os.path.realpath(path)

        # Security: only allow reads from /workspace or /mnt/user-data
        allowed_prefixes = ["/workspace", "/mnt/user-data"]
        if not any(resolved_path.startswith(p) for p in allowed_prefixes):
            return web.json_response(
                {"success": False, "error": "Path not allowed"},
            )

        try:
            with open(resolved_path, encoding="utf-8") as f:
                content = f.read()
            return web.json_response({"success": True, "content": content})
        except FileNotFoundError:
            return web.json_response({"success": False, "error": "File not found"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)})

    def create_app(self) -> web.Application:
        """Create the aiohttp application."""
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/exec", self.exec_command)
        app.router.add_post("/exec/stream", self.exec_stream)
        app.router.add_post("/file/write", self.write_file)
        app.router.add_post("/file/read", self.read_file)
        return app


def reap_zombies():
    """Reap zombie child processes (PID 1 responsibility)."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break


async def zombie_reaper():
    """Periodically reap zombie processes."""
    while True:
        await asyncio.sleep(1)
        reap_zombies()


def main():
    parser = argparse.ArgumentParser(description="Sandbox process API")
    parser.add_argument(
        "--addr",
        default="0.0.0.0:2024",
        help="Address to listen on (default: 0.0.0.0:2024)",
    )
    parser.add_argument(
        "--memory-limit-bytes",
        type=int,
        default=None,
        help="Memory limit in bytes (enforced via resource limits)",
    )
    args = parser.parse_args()

    host, port = args.addr.rsplit(":", 1)
    port = int(port)

    api = ProcessAPI(memory_limit_bytes=args.memory_limit_bytes)
    app = api.create_app()

    # Setup signal handlers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig):
        logger.info(f"Received signal {sig}, shutting down...")
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    # Start zombie reaper task
    loop.create_task(zombie_reaper())

    logger.info(f"process_api listening on {host}:{port}")

    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
