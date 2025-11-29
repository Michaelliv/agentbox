#!/usr/bin/env python3
"""Example Python gRPC client for the Sandbox service.

Usage:
    PYTHONPATH=gen/python uv run python scripts/grpc_client_example.py

Requires the gRPC server to be running (make dev).
"""

import sys
import os

# Add gen directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gen", "python"))

import grpc
from sandbox.v1 import sandbox_pb2, sandbox_pb2_grpc


def main():
    # Connect to gRPC server
    channel = grpc.insecure_channel("localhost:50051")
    stub = sandbox_pb2_grpc.SandboxServiceStub(channel)

    print("=== Python gRPC Client Test ===\n")

    # Create session with default allowed hosts
    print("Creating session (with default allowed hosts)...")
    response = stub.CreateSession(
        sandbox_pb2.CreateSessionRequest()
    )
    session_id = response.session.session_id
    print(f"  Session ID: {session_id}")
    print(f"  Container: {response.session.container_id}")
    print(f"  Allowed hosts: {list(response.session.allowed_hosts)}")

    try:
        # Execute command
        print("\nExecuting 'python3 --version'...")
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session_id,
                command="python3 --version",
                timeout=30,
                workdir="/workspace",
            )
        )
        print(f"  Exit code: {response.exit_code}")
        print(f"  Output: {response.stdout.strip()}")

        # Write file
        print("\nWriting test.py...")
        response = stub.WriteFile(
            sandbox_pb2.WriteFileRequest(
                session_id=session_id,
                path="test.py",
                content="print('Hello from gRPC!')",
                mode="w",
            )
        )
        print(f"  Success: {response.success}")

        # Read file
        print("\nReading test.py...")
        response = stub.ReadFile(
            sandbox_pb2.ReadFileRequest(
                session_id=session_id,
                path="test.py",
            )
        )
        print(f"  Content: {response.content}")

        # Execute file
        print("\nExecuting test.py...")
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session_id,
                command="python3 test.py",
                timeout=30,
                workdir="/workspace",
            )
        )
        print(f"  Output: {response.stdout.strip()}")

        # Check kernel (should show gVisor if using runsc)
        print("\nChecking kernel...")
        response = stub.Exec(
            sandbox_pb2.ExecRequest(
                session_id=session_id,
                command="uname -r",
                timeout=30,
                workdir="/workspace",
            )
        )
        print(f"  Kernel: {response.stdout.strip()}")

        # List sessions
        print("\nListing sessions...")
        response = stub.ListSessions(sandbox_pb2.ListSessionsRequest())
        print(f"  Active sessions: {len(response.sessions)}")

    finally:
        # Cleanup
        print("\nDestroying session...")
        response = stub.DestroySession(
            sandbox_pb2.DestroySessionRequest(session_id=session_id)
        )
        print(f"  Success: {response.success}")

    print("\nDone!")


if __name__ == "__main__":
    main()
