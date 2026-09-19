"""Linux namespace isolation; no silent fallback to an unrestricted process."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def sandbox_command(argv, workspace, policy, *, readonly=(), controller_root=None):
    if policy.get("backend") != "bubblewrap":
        raise ValueError("An enforced worker requires the bubblewrap backend")
    executable = shutil.which("bwrap")
    if not executable:
        raise ValueError(
            "bubblewrap is unavailable; unrestricted fallback is forbidden"
        )
    workspace = Path(workspace).resolve(strict=True)
    controller = Path(controller_root or Path(__file__).resolve().parents[1]).resolve()
    if controller.is_relative_to(workspace):
        raise ValueError("Worker workspace must not contain the controller")
    if not workspace.is_dir() or workspace == Path("/") or workspace == Path.home():
        raise ValueError("An isolated workspace directory is required")
    network = policy.get("network", "none")
    if network not in {"none", "host"}:
        raise ValueError("Sandbox network must be none or explicitly host")
    command = [
        executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
    ]
    if network == "host":
        command += ["--share-net"]
    command += ["--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/sbin", "/lib", "/lib64"):
        if Path(path).is_symlink():
            command += ["--symlink", os.readlink(path), path]
        elif Path(path).exists():
            command += ["--ro-bind", path, path]
    command += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--dir",
        "/home/worker",
        "--dir",
        "/etc",
    ]
    for path in (
        "/etc/ssl/certs",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
    ):
        if Path(path).exists():
            command += ["--ro-bind", str(Path(path).resolve()), path]
    for path in [*policy.get("readonly", []), *readonly]:
        internal_file = path in readonly
        path = Path(path).resolve(strict=True)
        # Code/runtime mounts must not expose the host's home, credentials or DB.
        if (
            path
            in {
                Path("/"),
                Path.home(),
                Path("/home"),
                Path("/etc"),
                Path("/var"),
                Path("/run"),
            }
            or controller.is_relative_to(path)
            or any(
                p
                in {
                    ".ssh",
                    ".config",
                    ".aws",
                    ".codex",
                    ".claude",
                    ".gemini",
                    ".kube",
                    ".gnupg",
                }
                for p in path.parts
            )
            or ("runtime" in path.parts and not internal_file)
            or path == workspace
            or path in workspace.parents
        ):
            raise ValueError(
                "Sandbox mount would expose controller state or host credentials"
            )
        if internal_file and not path.is_file():
            raise ValueError(
                "Controller may expose only individual runner/prompt files"
            )
        command += ["--ro-bind", str(path), str(path)]
    command += ["--bind", str(workspace), str(workspace), "--chdir", str(workspace)]
    git_path = workspace / ".git"
    if git_path.is_dir():
        command += ["--tmpfs", str(git_path), "--remount-ro", str(git_path)]
    elif git_path.exists():
        command += ["--ro-bind", "/dev/null", str(git_path)]
    environment = {
        "HOME": "/home/worker",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    for name, value in policy.get("environment", {}).items():
        if name in {
            "HOME",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "BASH_ENV",
            "ENV",
        }:
            raise ValueError("Unsafe sandbox environment override")
        environment[name] = str(value)
    for name in policy.get("env_keys", []):
        if not name.endswith("_API_KEY"):
            raise ValueError(
                "Only explicitly named provider API keys may enter the sandbox"
            )
        if name in os.environ:
            environment[name] = os.environ[name]
    return command + ["--", *map(str, argv)], environment


def bounded_command(
    argv, *, cwd, timeout=120, env=None, input_data=None, limit=65536, allowed=None
):
    """Bound process-group lifetime and output for trusted evaluators/adapters."""
    if not 0 < timeout <= 600:
        raise ValueError("Command timeout must be in (0,600]")
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as output:
        if input_data:
            stdin.write(input_data)
        stdin.seek(0)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=output,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if allowed is not None and not allowed():
                    from orchestrator.delivery_store import DeliveryConflict

                    raise DeliveryConflict("Process authority was revoked")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if output.tell() > limit:
                    raise ValueError("Command output exceeded the allowed limit")
                time.sleep(0.05)
            output.seek(0)
            data = output.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Command output exceeded the allowed limit")
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, argv)
            return data
        finally:
            # Descendants must not survive a successful parent exit either.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
