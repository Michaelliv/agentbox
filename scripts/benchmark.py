#!/usr/bin/env python3
"""
Benchmark suite for agentbox.

Measures:
1. Cold start latency - Session creation time
2. Command execution latency - Simple command overhead
3. Memory overhead - Per-session memory consumption
4. Concurrent sessions - Scaling behavior

Usage:
    PYTHONPATH=gen/python uv run python scripts/benchmark.py

Requires the gRPC server to be running (make dev).
"""

import argparse
import asyncio
import gc
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import grpc
from sandbox.v1 import sandbox_pb2, sandbox_pb2_grpc


@dataclass
class BenchmarkResult:
    name: str
    samples: list[float]
    unit: str = "ms"

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples)

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def std_dev(self) -> float:
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0

    @property
    def min(self) -> float:
        return min(self.samples)

    @property
    def max(self) -> float:
        return max(self.samples)

    @property
    def p95(self) -> float:
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def __str__(self) -> str:
        return (
            f"{self.name}:\n"
            f"  Mean:   {self.mean:>8.2f} {self.unit}\n"
            f"  Median: {self.median:>8.2f} {self.unit}\n"
            f"  Std:    {self.std_dev:>8.2f} {self.unit}\n"
            f"  Min:    {self.min:>8.2f} {self.unit}\n"
            f"  Max:    {self.max:>8.2f} {self.unit}\n"
            f"  P95:    {self.p95:>8.2f} {self.unit}\n"
            f"  Samples: {len(self.samples)}"
        )


def get_stub() -> sandbox_pb2_grpc.SandboxServiceStub:
    """Create a gRPC stub."""
    channel = grpc.insecure_channel("localhost:50051")
    return sandbox_pb2_grpc.SandboxServiceStub(channel)


def get_container_memory_mb(container_id: str) -> float:
    """Get memory usage of a container in MB."""
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Output like "50.5MiB / 4GiB"
        mem_str = result.stdout.strip().split("/")[0].strip()
        if "GiB" in mem_str:
            return float(mem_str.replace("GiB", "")) * 1024
        elif "MiB" in mem_str:
            return float(mem_str.replace("MiB", ""))
        elif "KiB" in mem_str:
            return float(mem_str.replace("KiB", "")) / 1024
        return 0
    except Exception:
        return 0


def benchmark_cold_start(stub: sandbox_pb2_grpc.SandboxServiceStub, iterations: int = 10) -> BenchmarkResult:
    """Benchmark session creation time (cold start)."""
    print(f"\n[1/4] Cold Start Latency ({iterations} iterations)...")
    samples = []

    for i in range(iterations):
        gc.collect()

        start = time.perf_counter()
        response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
        elapsed = (time.perf_counter() - start) * 1000  # ms

        samples.append(elapsed)
        print(f"  Iteration {i+1}: {elapsed:.2f} ms")

        # Cleanup
        stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=response.session.session_id))

    return BenchmarkResult("Cold Start Latency", samples)


def benchmark_exec_latency(stub: sandbox_pb2_grpc.SandboxServiceStub, iterations: int = 50) -> BenchmarkResult:
    """Benchmark command execution latency."""
    print(f"\n[2/4] Command Execution Latency ({iterations} iterations)...")

    # Create a session first
    response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
    session_id = response.session.session_id

    # Warm up
    stub.Exec(sandbox_pb2.ExecRequest(session_id=session_id, command="echo warmup", timeout=10))

    samples = []
    for i in range(iterations):
        start = time.perf_counter()
        stub.Exec(sandbox_pb2.ExecRequest(
            session_id=session_id,
            command="echo hello",
            timeout=10,
        ))
        elapsed = (time.perf_counter() - start) * 1000  # ms
        samples.append(elapsed)

        if (i + 1) % 10 == 0:
            print(f"  Progress: {i+1}/{iterations}")

    # Cleanup
    stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=session_id))

    return BenchmarkResult("Exec Latency (echo hello)", samples)


def benchmark_memory_overhead(stub: sandbox_pb2_grpc.SandboxServiceStub, sessions: int = 5) -> BenchmarkResult:
    """Benchmark per-session memory overhead."""
    print(f"\n[3/4] Memory Overhead ({sessions} sessions)...")

    session_ids = []
    container_ids = []
    samples = []

    for i in range(sessions):
        response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
        session_ids.append(response.session.session_id)
        container_ids.append(response.session.container_id)

        # Let container settle
        time.sleep(1)

        # Measure memory
        mem_mb = get_container_memory_mb(response.session.container_id)
        samples.append(mem_mb)
        print(f"  Session {i+1}: {mem_mb:.1f} MB")

    # Cleanup
    for session_id in session_ids:
        stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=session_id))

    return BenchmarkResult("Memory per Session", samples, unit="MB")


def benchmark_concurrent_sessions(
    stub: sandbox_pb2_grpc.SandboxServiceStub,
    max_sessions: int = 20,
    step: int = 5,
) -> list[tuple[int, float]]:
    """Benchmark latency degradation with concurrent sessions."""
    print(f"\n[4/4] Concurrent Sessions (up to {max_sessions})...")

    results = []
    active_sessions = []

    for target_count in range(step, max_sessions + 1, step):
        # Create sessions to reach target
        while len(active_sessions) < target_count:
            response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
            active_sessions.append(response.session.session_id)

        # Measure exec latency across all sessions
        latencies = []

        def exec_on_session(session_id: str) -> float:
            start = time.perf_counter()
            stub.Exec(sandbox_pb2.ExecRequest(
                session_id=session_id,
                command="echo hello",
                timeout=10,
            ))
            return (time.perf_counter() - start) * 1000

        with ThreadPoolExecutor(max_workers=target_count) as executor:
            futures = [executor.submit(exec_on_session, sid) for sid in active_sessions]
            for future in as_completed(futures):
                latencies.append(future.result())

        avg_latency = statistics.mean(latencies)
        results.append((target_count, avg_latency))
        print(f"  {target_count} sessions: avg exec latency = {avg_latency:.2f} ms")

    # Cleanup
    for session_id in active_sessions:
        stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=session_id))

    return results


def print_summary(results: dict):
    """Print benchmark summary."""
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    for result in results.values():
        if isinstance(result, BenchmarkResult):
            print(f"\n{result}")

    if "concurrent" in results:
        print("\nConcurrent Sessions Scaling:")
        for count, latency in results["concurrent"]:
            print(f"  {count:>3} sessions: {latency:>8.2f} ms avg exec latency")


def main():
    parser = argparse.ArgumentParser(description="Benchmark agentbox")
    parser.add_argument("--cold-start-iterations", type=int, default=10)
    parser.add_argument("--exec-iterations", type=int, default=50)
    parser.add_argument("--memory-sessions", type=int, default=5)
    parser.add_argument("--max-concurrent", type=int, default=20)
    parser.add_argument("--skip", nargs="*", choices=["cold", "exec", "memory", "concurrent"], default=[])
    args = parser.parse_args()

    stub = get_stub()
    results = {}

    print("=" * 60)
    print("AGENTBOX BENCHMARK")
    print("=" * 60)

    if "cold" not in args.skip:
        results["cold_start"] = benchmark_cold_start(stub, args.cold_start_iterations)

    if "exec" not in args.skip:
        results["exec"] = benchmark_exec_latency(stub, args.exec_iterations)

    if "memory" not in args.skip:
        results["memory"] = benchmark_memory_overhead(stub, args.memory_sessions)

    if "concurrent" not in args.skip:
        results["concurrent"] = benchmark_concurrent_sessions(stub, args.max_concurrent)

    print_summary(results)


if __name__ == "__main__":
    main()
