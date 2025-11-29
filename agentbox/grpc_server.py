"""gRPC server implementation for the Sandbox service.

This server is compatible with Connect clients (browser, Node.js) and native gRPC clients.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from concurrent import futures
from typing import TYPE_CHECKING

import grpc

# Add gen directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gen", "python"))

from sandbox.v1 import sandbox_pb2, sandbox_pb2_grpc
from agentbox.sandbox_manager import SandboxManager, SandboxSession
from agentbox.models import RuntimeType

if TYPE_CHECKING:
    from grpc import ServicerContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AsyncLoopThread:
    """Manages a dedicated event loop running in a background thread.

    This allows sync gRPC handlers to schedule async work on a shared loop,
    which is more efficient than creating a new loop per request and ensures
    background tasks (like session cleanup) keep running.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background event loop thread."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        """Run the event loop forever in the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro) -> any:
        """Run a coroutine on the background loop and wait for result.

        This is thread-safe and can be called from any thread.
        """
        if self._loop is None:
            raise RuntimeError("Event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def stop(self) -> None:
        """Stop the background event loop."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)


class SandboxServicer(sandbox_pb2_grpc.SandboxServiceServicer):
    """gRPC servicer implementation."""

    def __init__(self, manager: SandboxManager, loop_thread: AsyncLoopThread) -> None:
        self.manager = manager
        self._loop = loop_thread

    def CreateSession(
        self, request: sandbox_pb2.CreateSessionRequest, context: ServicerContext
    ) -> sandbox_pb2.CreateSessionResponse:
        """Create a new sandbox session."""
        # Convert repeated field to list, None if empty (use defaults)
        allowed_hosts = list(request.allowed_hosts) if request.allowed_hosts else None

        session = self._loop.run(
            self.manager.create_session(
                session_id=request.session_id if request.session_id else None,
                tenant_id=request.tenant_id if request.tenant_id else None,
                allowed_hosts=allowed_hosts,
            )
        )
        return sandbox_pb2.CreateSessionResponse(
            session=self._session_to_proto(session)
        )

    def DestroySession(
        self, request: sandbox_pb2.DestroySessionRequest, context: ServicerContext
    ) -> sandbox_pb2.DestroySessionResponse:
        """Destroy an existing session."""
        success = self._loop.run(self.manager.destroy_session(request.session_id))
        return sandbox_pb2.DestroySessionResponse(success=success)

    def GetSession(
        self, request: sandbox_pb2.GetSessionRequest, context: ServicerContext
    ) -> sandbox_pb2.GetSessionResponse:
        """Get session info."""
        session = self._loop.run(self.manager.get_session(request.session_id))
        if not session:
            context.abort(grpc.StatusCode.NOT_FOUND, "Session not found")
            return sandbox_pb2.GetSessionResponse()  # Never reached, but satisfies type checker
        return sandbox_pb2.GetSessionResponse(session=self._session_to_proto(session))

    def ListSessions(
        self, request: sandbox_pb2.ListSessionsRequest, context: ServicerContext
    ) -> sandbox_pb2.ListSessionsResponse:
        """List all active sessions."""
        sessions = self.manager.list_sessions()
        return sandbox_pb2.ListSessionsResponse(
            sessions=[
                sandbox_pb2.SessionInfo(
                    session_id=s["session_id"],
                    container_id=s["container_id"],
                    created_at=s["created_at"],
                    last_activity=s["last_activity"],
                    status=s["status"],
                    tenant_id=s.get("tenant_id"),
                    allowed_hosts=s.get("allowed_hosts", []),
                )
                for s in sessions
            ]
        )

    def Exec(
        self, request: sandbox_pb2.ExecRequest, context: ServicerContext
    ) -> sandbox_pb2.ExecResponse:
        """Execute a command and wait for completion."""
        result = self._loop.run(
            self.manager.exec_command(
                session_id=request.session_id,
                command=request.command,
                timeout=request.timeout or 30,
                workdir=request.workdir or "/workspace",
            )
        )
        return sandbox_pb2.ExecResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )

    def ExecStream(
        self, request: sandbox_pb2.ExecStreamRequest, context: ServicerContext
    ):
        """Execute a command and stream output."""

        async def collect_chunks():
            chunks = []
            async for chunk in self.manager.exec_stream(
                session_id=request.session_id,
                command=request.command,
                workdir=request.workdir or "/workspace",
            ):
                chunks.append(chunk)
            return chunks

        chunks = self._loop.run(collect_chunks())
        for chunk in chunks:
            exit_code = chunk.get("exit_code") if chunk["type"] == "exit" else None
            yield sandbox_pb2.ExecStreamResponse(
                type=chunk["type"],
                data=str(chunk.get("data", "")) if chunk["type"] != "exit" else "",
                exit_code=exit_code,
            )

    def WriteFile(
        self, request: sandbox_pb2.WriteFileRequest, context: ServicerContext
    ) -> sandbox_pb2.WriteFileResponse:
        """Write a file to the sandbox."""
        success, error = self._loop.run(
            self.manager.write_file(
                session_id=request.session_id,
                path=request.path,
                content=request.content,
                mode=request.mode or "w",
            )
        )
        return sandbox_pb2.WriteFileResponse(success=success, error=error)

    def ReadFile(
        self, request: sandbox_pb2.ReadFileRequest, context: ServicerContext
    ) -> sandbox_pb2.ReadFileResponse:
        """Read a file from the sandbox."""
        success, content = self._loop.run(
            self.manager.read_file(
                session_id=request.session_id,
                path=request.path,
            )
        )
        if success:
            return sandbox_pb2.ReadFileResponse(success=True, content=content)
        return sandbox_pb2.ReadFileResponse(success=False, error=content)

    def PipInstall(
        self, request: sandbox_pb2.PipInstallRequest, context: ServicerContext
    ) -> sandbox_pb2.ExecResponse:
        """Install Python packages."""
        result = self._loop.run(
            self.manager.pip_install(
                session_id=request.session_id,
                packages=list(request.packages),
            )
        )
        return sandbox_pb2.ExecResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )

    def _session_to_proto(self, session: SandboxSession) -> sandbox_pb2.SessionInfo:
        """Convert SandboxSession to protobuf SessionInfo."""
        return sandbox_pb2.SessionInfo(
            session_id=session.session_id,
            container_id=session.container.short_id,
            created_at=session.created_at,
            last_activity=session.last_activity,
            status=session.container.status,
            tenant_id=session.tenant_id,
            allowed_hosts=session.allowed_hosts,
        )


def serve(port: int = 50051) -> None:
    """Start the gRPC server."""
    # Initialize sandbox manager
    runtime_str = os.getenv("SANDBOX_RUNTIME", "runc")
    runtime = RuntimeType(runtime_str)
    storage_path = os.getenv("STORAGE_PATH")
    proxy_host = os.getenv("PROXY_HOST")
    proxy_port = int(os.getenv("PROXY_PORT", "15004"))
    signing_key = os.getenv("SIGNING_KEY")

    manager = SandboxManager(
        image_name=os.getenv("SANDBOX_IMAGE", "agentbox-sandbox:latest"),
        runtime=runtime,
        session_timeout=int(os.getenv("SESSION_TIMEOUT", "1800")),
        storage_path=storage_path,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        signing_key=signing_key,
    )

    # Start background event loop for async operations
    loop_thread = AsyncLoopThread()
    loop_thread.start()

    # Start the manager's cleanup task on the background loop
    loop_thread.run(manager.start())

    # Create gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    sandbox_pb2_grpc.add_SandboxServiceServicer_to_server(
        SandboxServicer(manager, loop_thread), server
    )

    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"gRPC server started on port {port}")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        loop_thread.run(manager.stop())
        loop_thread.stop()
        server.stop(grace=5)


if __name__ == "__main__":
    port = int(os.getenv("GRPC_PORT", "50051"))
    serve(port)
