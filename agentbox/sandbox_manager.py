import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Optional
from dataclasses import dataclass, field

import docker
import httpx
from docker.models.containers import Container
from docker.errors import NotFound, APIError

from agentbox.models import ExecResponse, RuntimeType

logger = logging.getLogger(__name__)

# Default allowed hosts for egress proxy
DEFAULT_ALLOWED_HOSTS = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "crates.io",
    "static.crates.io",
]


@dataclass
class SandboxSession:
    """Represents an active sandbox session."""

    session_id: str
    container: Container
    created_at: float
    api_host: str  # Host to connect to (localhost for port mapping)
    api_port: int  # Port for process_api
    tenant_id: Optional[str] = None
    allowed_hosts: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)

    @property
    def api_url(self) -> str:
        """URL for the container's process_api."""
        return f"http://{self.api_host}:{self.api_port}"


class SandboxManager:
    """Manages sandbox container lifecycle.

    Architecture aligned with Claude's sandbox:
    - gVisor (runsc) as the security boundary
    - process_api as PID 1 for command execution
    - Egress proxy with JWT allowlist for network control
    - Session-scoped containers
    """

    def __init__(
        self,
        image_name: str = "agentbox-sandbox:latest",
        runtime: RuntimeType = RuntimeType.RUNSC,
        session_timeout: int = 1800,  # 30 minutes
        cleanup_interval: int = 60,
        storage_path: Optional[str] = None,
        proxy_host: Optional[str] = None,
        proxy_port: int = 15004,
        signing_key: Optional[str] = None,
    ):
        self.image_name = image_name
        self.runtime = runtime
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval

        # Egress proxy configuration
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.signing_key = signing_key or secrets.token_hex(32)

        # Persistent storage directory
        self.storage_path = Path(storage_path) if storage_path else None
        if self.storage_path:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Persistent storage enabled at: {self.storage_path}")

        self.docker_client = docker.from_env()
        self.sessions: dict[str, SandboxSession] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create a shared httpx client for connection reuse."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
        return self._http_client

    async def start(self):
        """Start the manager and cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("SandboxManager started")
        if self.proxy_host:
            logger.info(f"Egress proxy configured at {self.proxy_host}:{self.proxy_port}")

    async def stop(self) -> None:
        """Stop the manager and cleanup all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Cleanup all active sessions
        for session_id in list(self.sessions.keys()):
            await self.destroy_session(session_id)

        # Close HTTP client
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("SandboxManager stopped")

    def _generate_proxy_jwt(
        self,
        session_id: str,
        tenant_id: Optional[str],
        allowed_hosts: list[str],
    ) -> str:
        """Generate a JWT for the egress proxy."""
        header = {"typ": "JWT", "alg": "HS256"}
        payload = {
            "iss": "sandbox-egress-control",
            "session_id": session_id,
            "tenant_id": tenant_id,
            "allowed_hosts": ",".join(allowed_hosts),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=4)).timestamp()),
        }

        def b64encode(data: dict) -> str:
            return base64.urlsafe_b64encode(
                json.dumps(data, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()

        header_b64 = b64encode(header)
        payload_b64 = b64encode(payload)
        message = f"{header_b64}.{payload_b64}"

        signature = hmac.new(
            self.signing_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).digest()
        signature_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

        return f"{message}.{signature_b64}"

    def _generate_proxy_url(
        self,
        session_id: str,
        tenant_id: Optional[str],
        allowed_hosts: list[str],
    ) -> str:
        """Generate the proxy URL with embedded JWT credentials."""
        if not self.proxy_host:
            return ""

        token = self._generate_proxy_jwt(session_id, tenant_id, allowed_hosts)
        return f"http://sandbox:jwt_{token}@{self.proxy_host}:{self.proxy_port}"

    async def create_session(
        self,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        allowed_hosts: Optional[list[str]] = None,
    ) -> SandboxSession:
        """Create a new sandbox session with its own container.

        Args:
            session_id: Optional session ID. Generated if not provided.
            tenant_id: Optional tenant ID for persistent storage.
            allowed_hosts: List of allowed egress hosts. Defaults to package
                          registries and GitHub. Pass empty list to disable network.
        """
        async with self._lock:
            if session_id is None:
                session_id = str(uuid.uuid4())

            if session_id in self.sessions:
                session = self.sessions[session_id]
                session.last_activity = time.time()
                return session

            # Use default hosts if not specified
            hosts = allowed_hosts if allowed_hosts is not None else DEFAULT_ALLOWED_HOSTS

            # Create tenant storage directory if needed
            tenant_storage = None
            if tenant_id and self.storage_path:
                tenant_storage = self._ensure_tenant_storage(tenant_id)

            # Create container
            container = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._create_container(
                    session_id, tenant_id, tenant_storage, hosts
                ),
            )

            # Get address for process_api communication
            api_host, api_port = await self._get_container_api_address(container)

            # Wait for process_api to be ready
            await self._wait_for_process_api(api_host, api_port)

            session = SandboxSession(
                session_id=session_id,
                container=container,
                created_at=time.time(),
                api_host=api_host,
                api_port=api_port,
                tenant_id=tenant_id,
                allowed_hosts=hosts,
            )
            self.sessions[session_id] = session

            network_info = f"allowed_hosts={hosts}" if hosts else "network disabled"
            logger.info(
                f"Created session {session_id} with container {container.short_id} "
                f"at {api_host}:{api_port} ({network_info})"
                + (f" (tenant: {tenant_id})" if tenant_id else "")
            )
            return session

    def _ensure_tenant_storage(self, tenant_id: str) -> Path:
        """Create and return the storage directory for a tenant."""
        tenant_dir = self.storage_path / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)

        (tenant_dir / "workspace").mkdir(exist_ok=True)
        (tenant_dir / "outputs").mkdir(exist_ok=True)

        return tenant_dir

    def _create_container(
        self,
        session_id: str,
        tenant_id: Optional[str],
        tenant_storage: Optional[Path],
        allowed_hosts: list[str],
    ) -> Container:
        """Create and start a container (sync, runs in executor)."""
        container_name = f"sandbox-{session_id[:8]}"
        runtime = self.runtime.value

        # Volume mounts
        volumes = {}
        if tenant_storage:
            volumes[str(tenant_storage / "workspace")] = {
                "bind": "/workspace",
                "mode": "rw",
            }
            volumes[str(tenant_storage / "outputs")] = {
                "bind": "/mnt/user-data/outputs",
                "mode": "rw",
            }

        # Network configuration
        # If proxy is configured, use bridge network with proxy env vars
        # If no proxy and no allowed hosts, use network_mode=none
        # If no proxy but allowed hosts, use bridge (less secure but functional)
        environment = {}
        network_mode = None

        if self.proxy_host and allowed_hosts:
            # Use egress proxy for controlled network access
            proxy_url = self._generate_proxy_url(session_id, tenant_id, allowed_hosts)
            environment["HTTP_PROXY"] = proxy_url
            environment["HTTPS_PROXY"] = proxy_url
            environment["http_proxy"] = proxy_url
            environment["https_proxy"] = proxy_url
        elif not allowed_hosts:
            # No network access
            network_mode = "none"
        # else: full network access (no proxy configured but hosts allowed)

        container = self.docker_client.containers.run(
            self.image_name,
            detach=True,
            name=container_name,
            runtime=runtime,
            # Resource limits matching Claude's sandbox
            mem_limit="4g",
            cpu_period=100000,
            cpu_quota=400000,  # 4 CPUs
            # Security
            security_opt=["no-new-privileges"],
            # Network
            network_mode=network_mode,
            environment=environment,
            # Port mapping for process_api (required on macOS where container IPs aren't accessible)
            ports={"2024/tcp": None},  # Assign random host port
            # Volumes
            volumes=volumes,
            labels={
                "agentbox": "true",
                "session-id": session_id,
            },
        )

        return container

    async def _get_container_api_address(self, container: Container) -> tuple[str, int]:
        """Get the host and port to connect to the container's process_api."""
        def get_address():
            container.reload()
            # Get the mapped port for 2024/tcp
            ports = container.attrs["NetworkSettings"]["Ports"]
            port_mapping = ports.get("2024/tcp")
            if port_mapping:
                # Use localhost with the mapped port
                host_port = int(port_mapping[0]["HostPort"])
                return ("127.0.0.1", host_port)
            # Fallback to container IP if no port mapping
            networks = container.attrs["NetworkSettings"]["Networks"]
            for network in networks.values():
                if network.get("IPAddress"):
                    return (network["IPAddress"], 2024)
            raise RuntimeError("Container has no accessible address")

        return await asyncio.get_event_loop().run_in_executor(None, get_address)

    async def _wait_for_process_api(
        self, api_host: str, api_port: int, timeout: float = 30.0
    ) -> None:
        """Wait for the process_api to be ready."""
        url = f"http://{api_host}:{api_port}/health"
        start = time.time()
        client = await self._get_http_client()

        while time.time() - start < timeout:
            try:
                response = await client.get(url, timeout=1.0)
                if response.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                pass
            await asyncio.sleep(0.1)

        raise RuntimeError(f"process_api did not become ready within {timeout}s")

    async def get_session(self, session_id: str) -> Optional[SandboxSession]:
        """Get an existing session by ID."""
        session = self.sessions.get(session_id)
        if session:
            session.last_activity = time.time()
        return session

    async def destroy_session(self, session_id: str) -> bool:
        """Destroy a session and its container."""
        async with self._lock:
            session = self.sessions.pop(session_id, None)
            if not session:
                return False

            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: session.container.remove(force=True),
                )
                logger.info(f"Destroyed session {session_id}")
                return True
            except NotFound:
                logger.warning(f"Container for session {session_id} already gone")
                return True
            except APIError as e:
                logger.error(f"Failed to destroy session {session_id}: {e}")
                return False

    async def exec_command(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
        workdir: str = "/workspace",
    ) -> ExecResponse:
        """Execute a command in a session's container via process_api."""
        session = await self.get_session(session_id)
        if not session:
            return ExecResponse(
                exit_code=-1,
                stdout="",
                stderr="Session not found",
                timed_out=False,
            )

        try:
            client = await self._get_http_client()
            response = await client.post(
                f"{session.api_url}/exec",
                json={
                    "command": command,
                    "workdir": workdir,
                    "timeout": timeout,
                },
                timeout=timeout + 5,
            )

            data = response.json()
            return ExecResponse(
                exit_code=data.get("exit_code", -1),
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
                timed_out=data.get("timed_out", False),
            )

        except httpx.TimeoutException:
            return ExecResponse(
                exit_code=-1,
                stdout="",
                stderr="Request to process_api timed out",
                timed_out=True,
            )
        except Exception as e:
            return ExecResponse(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                timed_out=False,
            )

    async def exec_stream(
        self,
        session_id: str,
        command: str,
        workdir: str = "/workspace",
    ) -> AsyncIterator[dict]:
        """Execute a command and stream output via process_api SSE."""
        session = await self.get_session(session_id)
        if not session:
            yield {"type": "error", "data": "Session not found"}
            return

        try:
            client = await self._get_http_client()
            async with client.stream(
                "POST",
                f"{session.api_url}/exec/stream",
                json={
                    "command": command,
                    "workdir": workdir,
                },
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        yield data

        except Exception as e:
            logger.error(f"Streaming exec error: {e}")
            yield {"type": "error", "data": str(e)}

    async def write_file(
        self,
        session_id: str,
        path: str,
        content: str,
        mode: str = "w",
    ) -> tuple[bool, Optional[str]]:
        """Write a file in the sandbox via process_api."""
        session = await self.get_session(session_id)
        if not session:
            return False, "Session not found"

        try:
            client = await self._get_http_client()
            response = await client.post(
                f"{session.api_url}/file/write",
                json={
                    "path": path,
                    "content": content,
                    "mode": mode,
                },
            )

            data = response.json()
            if data.get("success"):
                return True, None
            return False, data.get("error", "Unknown error")

        except Exception as e:
            return False, str(e)

    async def read_file(self, session_id: str, path: str) -> tuple[bool, str]:
        """Read a file from the sandbox via process_api."""
        session = await self.get_session(session_id)
        if not session:
            return False, "Session not found"

        try:
            client = await self._get_http_client()
            response = await client.post(
                f"{session.api_url}/file/read",
                json={"path": path},
            )

            data = response.json()
            if data.get("success"):
                return True, data.get("content", "")
            return False, data.get("error", "Unknown error")

        except Exception as e:
            return False, str(e)

    # PEP 508 package name pattern: alphanumeric, hyphens, underscores, dots
    # With optional extras [extra1,extra2] and version specifiers
    _PIP_PACKAGE_PATTERN = re.compile(
        r"^"
        r"[A-Za-z0-9]"  # Must start with alphanumeric
        r"[A-Za-z0-9._-]*"  # Followed by alphanumeric, dots, underscores, hyphens
        r"(?:\[[A-Za-z0-9,._-]+\])?"  # Optional extras like [dev,test]
        r"(?:[<>=!~]+[A-Za-z0-9.*,<>=!~]+)?"  # Optional version specifier
        r"$"
    )

    async def pip_install(
        self,
        session_id: str,
        packages: list[str],
        timeout: int = 120,
    ) -> ExecResponse:
        """Install Python packages in the sandbox.

        Requires egress proxy to be configured with pypi.org in allowed hosts.
        """
        session = await self.get_session(session_id)
        if not session:
            return ExecResponse(
                exit_code=-1,
                stdout="",
                stderr="Session not found",
                timed_out=False,
            )

        # Check if pypi is allowed
        pypi_hosts = ["pypi.org", "files.pythonhosted.org"]
        if not all(h in session.allowed_hosts for h in pypi_hosts):
            return ExecResponse(
                exit_code=-1,
                stdout="",
                stderr="pip install requires pypi.org and files.pythonhosted.org in allowed_hosts",
                timed_out=False,
            )

        # Validate package names against PEP 508 pattern
        sanitized = []
        for pkg in packages:
            if not self._PIP_PACKAGE_PATTERN.match(pkg):
                return ExecResponse(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Invalid package specifier: {pkg}",
                    timed_out=False,
                )
            sanitized.append(pkg)

        packages_str = " ".join(f"'{p}'" for p in sanitized)
        command = f"pip install --user {packages_str}"

        logger.info(f"Installing packages in session {session_id}: {sanitized}")
        return await self.exec_command(session_id, command, timeout=timeout)

    async def _cleanup_loop(self):
        """Periodically cleanup expired sessions."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_expired(self):
        """Remove sessions that have been inactive too long."""
        now = time.time()
        expired = [
            sid
            for sid, session in self.sessions.items()
            if now - session.last_activity > self.session_timeout
        ]

        for session_id in expired:
            logger.info(f"Cleaning up expired session {session_id}")
            await self.destroy_session(session_id)

    def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        return [
            {
                "session_id": s.session_id,
                "container_id": s.container.short_id,
                "created_at": s.created_at,
                "last_activity": s.last_activity,
                "status": s.container.status,
                "tenant_id": s.tenant_id,
                "allowed_hosts": s.allowed_hosts,
            }
            for s in self.sessions.values()
        ]
