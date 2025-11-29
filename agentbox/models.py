"""Internal models for the sandbox manager."""

from dataclasses import dataclass
from enum import Enum


@dataclass
class ExecResponse:
    """Response from command execution."""
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class RuntimeType(str, Enum):
    """Container runtime type."""
    RUNC = "runc"  # Standard Docker runtime
    RUNSC = "runsc"  # gVisor runtime
