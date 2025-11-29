"""Integration tests for agentbox.

These tests require the gRPC server to be running:
    make dev

Run with:
    PYTHONPATH=gen/python pytest tests/test_integration.py -v
"""

import pytest
import grpc
from sandbox.v1 import sandbox_pb2, sandbox_pb2_grpc


@pytest.fixture
def stub():
    """Create a gRPC stub connected to the server."""
    channel = grpc.insecure_channel("localhost:50051")
    return sandbox_pb2_grpc.SandboxServiceStub(channel)


@pytest.fixture
def session(stub):
    """Create a session and clean it up after the test."""
    response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
    session_id = response.session.session_id
    yield response.session
    stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=session_id))


class TestSessionLifecycle:
    """Test session creation and destruction."""

    def test_create_session_with_defaults(self, stub):
        """Create a session with default allowed hosts."""
        response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
        assert response.session.session_id
        assert response.session.container_id
        assert "pypi.org" in response.session.allowed_hosts

        # Cleanup
        stub.DestroySession(
            sandbox_pb2.DestroySessionRequest(session_id=response.session.session_id)
        )

    def test_create_session_with_custom_hosts(self, stub):
        """Create a session with custom allowed hosts."""
        response = stub.CreateSession(
            sandbox_pb2.CreateSessionRequest(allowed_hosts=["example.com"])
        )
        assert response.session.session_id
        assert list(response.session.allowed_hosts) == ["example.com"]

        # Cleanup
        stub.DestroySession(
            sandbox_pb2.DestroySessionRequest(session_id=response.session.session_id)
        )

    def test_create_session_empty_hosts_uses_defaults(self, stub):
        """Empty allowed_hosts uses defaults (proto3 can't distinguish empty from unset)."""
        # Note: In proto3, repeated fields can't distinguish between [] and unset.
        # So empty list = use defaults. This is intentional for the common case.
        response = stub.CreateSession(
            sandbox_pb2.CreateSessionRequest(allowed_hosts=[])
        )
        assert response.session.session_id
        # Empty list gets defaults, not "no network"
        assert "pypi.org" in response.session.allowed_hosts

        # Cleanup
        stub.DestroySession(
            sandbox_pb2.DestroySessionRequest(session_id=response.session.session_id)
        )

    def test_destroy_session(self, stub):
        """Destroy a session."""
        create_response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
        session_id = create_response.session.session_id

        destroy_response = stub.DestroySession(
            sandbox_pb2.DestroySessionRequest(session_id=session_id)
        )
        assert destroy_response.success

    def test_list_sessions(self, stub, session):
        """List active sessions."""
        response = stub.ListSessions(sandbox_pb2.ListSessionsRequest())
        session_ids = [s.session_id for s in response.sessions]
        assert session.session_id in session_ids

    def test_get_session(self, stub, session):
        """Get session info."""
        response = stub.GetSession(
            sandbox_pb2.GetSessionRequest(session_id=session.session_id)
        )
        assert response.session.session_id == session.session_id
        assert response.session.container_id == session.container_id


class TestExec:
    """Test command execution."""

    def test_exec_simple_command(self, stub, session):
        """Execute a simple command."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="echo hello",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert response.stdout.strip() == "hello"

    def test_exec_python(self, stub, session):
        """Execute Python code."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="python3 -c 'print(1 + 1)'",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert response.stdout.strip() == "2"

    def test_exec_with_stderr(self, stub, session):
        """Execute a command that writes to stderr."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="python3 -c 'import sys; sys.stderr.write(\"error\\n\")'",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert "error" in response.stderr

    def test_exec_nonzero_exit(self, stub, session):
        """Execute a command that fails."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="exit 42",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 42

    def test_exec_workdir(self, stub, session):
        """Execute a command in a specific directory."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="pwd",
                timeout=30,
                workdir="/tmp",
            )
        )
        assert response.exit_code == 0
        assert response.stdout.strip() == "/tmp"


class TestFileOperations:
    """Test file read/write operations."""

    def test_write_and_read_file(self, stub, session):
        """Write a file and read it back."""
        content = "Hello, World!"

        # Write
        write_response = stub.WriteFile(
            sandbox_pb2.WriteFileRequest(
                session_id=session.session_id,
                path="test.txt",
                content=content,
                mode="w",
            )
        )
        assert write_response.success

        # Read
        read_response = stub.ReadFile(
            sandbox_pb2.ReadFileRequest(
                session_id=session.session_id,
                path="test.txt",
            )
        )
        assert read_response.success
        assert read_response.content == content

    def test_append_file(self, stub, session):
        """Append to a file."""
        # Write initial content
        stub.WriteFile(
            sandbox_pb2.WriteFileRequest(
                session_id=session.session_id,
                path="append.txt",
                content="line1\n",
                mode="w",
            )
        )

        # Append
        stub.WriteFile(
            sandbox_pb2.WriteFileRequest(
                session_id=session.session_id,
                path="append.txt",
                content="line2\n",
                mode="a",
            )
        )

        # Read
        response = stub.ReadFile(
            sandbox_pb2.ReadFileRequest(
                session_id=session.session_id,
                path="append.txt",
            )
        )
        assert response.content == "line1\nline2\n"

    def test_write_python_and_execute(self, stub, session):
        """Write a Python script and execute it."""
        script = "print('Hello from script!')"

        stub.WriteFile(
            sandbox_pb2.WriteFileRequest(
                session_id=session.session_id,
                path="script.py",
                content=script,
                mode="w",
            )
        )

        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="python3 script.py",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert response.stdout.strip() == "Hello from script!"

    def test_read_nonexistent_file(self, stub, session):
        """Read a file that doesn't exist."""
        response = stub.ReadFile(
            sandbox_pb2.ReadFileRequest(
                session_id=session.session_id,
                path="nonexistent.txt",
            )
        )
        assert not response.success
        assert response.error


class TestIsolation:
    """Test container isolation."""

    def test_workspace_is_writable(self, stub, session):
        """Verify /workspace is writable."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="touch /workspace/test && echo ok",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert response.stdout.strip() == "ok"

    def test_python_available(self, stub, session):
        """Verify Python is available."""
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session.session_id,
                command="python3 --version",
                timeout=30,
                workdir="/workspace",
            )
        )
        assert response.exit_code == 0
        assert "Python 3" in response.stdout
