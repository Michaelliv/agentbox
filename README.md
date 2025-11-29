# agentbox

*A computer for your agent.*

Run untrusted code safely in isolated containers with controlled network access. Give your AI agent a full Linux environment with Python, shell, and filesystem access.

## Architecture

```
┌─────────────┐     gRPC      ┌─────────────────┐     HTTP      ┌─────────────────────┐
│   Client    │──────────────▶│   gRPC Server   │──────────────▶│  Container:2024     │
│  (TS/Python)│               │  (SandboxManager)│               │  (process_api)      │
└─────────────┘               └─────────────────┘               └─────────────────────┘
                                      │                                   │
                                      ▼                                   ▼
                              ┌─────────────────┐               ┌─────────────────────┐
                              │  Egress Proxy   │◀──────────────│  HTTP_PROXY env     │
                              │  (JWT allowlist)│               │  (pip, git, etc.)   │
                              └─────────────────┘               └─────────────────────┘
```

**Key components:**

- **process_api** - Init process (PID 1) inside each container. HTTP server for command execution.
- **SandboxManager** - Manages container lifecycle, communicates with process_api.
- **Egress Proxy** - Optional HTTP proxy that validates outbound requests against a JWT-encoded allowlist.
- **gVisor (runsc)** - Userspace kernel providing strong isolation. Containers run as root inside the sandbox.

## Quick Start

```bash
# Install dependencies
uv sync

# Generate protobuf code
make proto

# Build sandbox image
make build-sandbox

# Run server (full network access)
make dev

# Run server with egress proxy (controlled network)
make dev-proxy
```

## Usage

### Python Client

```python
import grpc
from sandbox.v1 import sandbox_pb2, sandbox_pb2_grpc

channel = grpc.insecure_channel("localhost:50051")
stub = sandbox_pb2_grpc.SandboxServiceStub(channel)

# Create session (uses default allowed hosts)
response = stub.CreateSession(sandbox_pb2.CreateSessionRequest())
session_id = response.session.session_id

# Execute command
result = stub.Exec(sandbox_pb2.ExecRequest(
    session_id=session_id,
    command="python3 -c 'print(1+1)'",
    timeout=30,
))
print(result.stdout)  # "2\n"

# Write and read files
stub.WriteFile(sandbox_pb2.WriteFileRequest(
    session_id=session_id,
    path="script.py",
    content="print('hello')",
    mode="w",
))

# Install packages (requires pypi.org in allowed_hosts)
stub.PipInstall(sandbox_pb2.PipInstallRequest(
    session_id=session_id,
    packages=["requests"],
))

# Cleanup
stub.DestroySession(sandbox_pb2.DestroySessionRequest(session_id=session_id))
```

### TypeScript Client

```typescript
import { Sandbox } from '@agentbox/client';

const sandbox = new Sandbox('http://localhost:50051');

// Create with default allowed hosts
await sandbox.create();

// Or specify custom hosts
await sandbox.create({ allowedHosts: ['pypi.org', 'files.pythonhosted.org'] });

// Execute commands
const result = await sandbox.exec('python3 --version');
console.log(result.stdout);

// File operations
await sandbox.writeFile('script.py', 'print("hello")');
const content = await sandbox.readFile('script.py');

// Run Python directly
const output = await sandbox.runPython('print(1 + 1)');

// Install packages
await sandbox.pipInstall(['requests']);

// Cleanup
await sandbox.destroy();
```

## API Reference

### CreateSession

Create a new sandbox session.

| Field | Type | Description |
|-------|------|-------------|
| session_id | string (optional) | Custom session ID. Generated if not provided. |
| tenant_id | string (optional) | Tenant ID for persistent storage. |
| allowed_hosts | string[] | Allowed egress hosts. Omit or empty = defaults. Specify hosts for custom allowlist. |

**Default allowed hosts:** `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `crates.io`, `static.crates.io`

> **Note:** Due to proto3 semantics, empty `allowed_hosts` uses defaults (can't distinguish empty from unset). To disable network, use a non-routable host like `["localhost"]`.

### Exec

Execute a command and wait for completion.

| Field | Type | Description |
|-------|------|-------------|
| session_id | string | Session ID |
| command | string | Shell command to execute |
| timeout | int | Timeout in seconds (default: 30) |
| workdir | string | Working directory (default: /workspace) |

### ExecStream

Execute a command and stream output via Server-Sent Events.

### WriteFile / ReadFile

Write or read files in the sandbox. Paths are relative to `/workspace` unless absolute.

### PipInstall

Install Python packages. Requires `pypi.org` and `files.pythonhosted.org` in allowed_hosts.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| GRPC_PORT | 50051 | gRPC server port |
| SANDBOX_IMAGE | agentbox-sandbox:latest | Docker image for sandboxes |
| SANDBOX_RUNTIME | runsc | Container runtime (runc, runsc) |
| STORAGE_PATH | - | Path for persistent tenant storage |
| SESSION_TIMEOUT | 1800 | Session timeout in seconds |
| PROXY_HOST | - | Egress proxy host (enables proxy mode) |
| PROXY_PORT | 15004 | Egress proxy port |
| SIGNING_KEY | - | Key for JWT signing (auto-generated if not set) |

### Resource Limits

Each container has:
- **Memory:** 4GB
- **CPU:** 4 cores
- **Network:** Controlled via egress proxy or network_mode

## Security

### Isolation Layers

1. **gVisor (runsc)** - Intercepts syscalls in userspace. Primary security boundary.
2. **Egress Proxy** - JWT-authenticated allowlist for outbound network.
3. **Resource Limits** - Memory and CPU constraints.
4. **Filesystem Mounts** - Only `/workspace` and `/mnt/user-data/outputs` are writable.

### Why Root Inside Sandbox?

gVisor provides strong enough isolation that running as root inside the sandbox is safe. "Root" inside the sandbox has no privileges outside gVisor's userspace kernel.

## Development

```bash
# Sync dependencies
uv sync

# Regenerate protobuf
make proto

# Build sandbox image
make build-sandbox

# Run Python example
make test

# Run TypeScript example
make test-ts

# Run integration tests (requires server running)
make test-integration

# Build TypeScript client
make build-ts

# Install gVisor (Linux only)
make install-gvisor
```

## Project Structure

```
agentbox/
├── proto/sandbox/v1/sandbox.proto  # API definition
├── agentbox/
│   ├── sandbox_manager.py          # Container orchestration
│   ├── grpc_server.py              # gRPC service
│   ├── egress_proxy.py             # HTTP proxy with JWT auth
│   └── models.py                   # Data models
├── sandbox/
│   ├── Dockerfile                  # Container image
│   └── process_api.py              # Init process (PID 1)
├── ts-client/
│   ├── src/index.ts                # TypeScript SDK
│   └── scripts/grpc_client_example.ts  # TypeScript example
├── scripts/
│   ├── grpc_client_example.py      # Python example
│   └── setup-gvisor.sh             # gVisor installer
└── gen/                            # Generated protobuf code
```

## License

MIT
