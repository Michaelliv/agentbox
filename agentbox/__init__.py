# agentbox - A computer for your agent
"""
agentbox - Sandboxed code execution for AI agents.

Run untrusted code safely in isolated containers with controlled network access.
"""

from agentbox.models import ExecResponse, RuntimeType
from agentbox.sandbox_manager import SandboxManager, SandboxSession

__all__ = [
    "SandboxManager",
    "SandboxSession",
    "ExecResponse",
    "RuntimeType",
]

__version__ = "0.1.0"
