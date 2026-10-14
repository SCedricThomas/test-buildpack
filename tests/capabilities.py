#!/usr/bin/env python3
"""Report only the kernel facilities needed by candidate Dockerfile builders."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


def child_probe(operation):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        libc = ctypes.CDLL(None, use_errno=True)
        if operation == "ptrace_child":
            libc.ptrace.restype = ctypes.c_long
            result = libc.ptrace(0, 0, None, None)  # PTRACE_TRACEME
        else:
            result = libc.unshare(0x10000000)  # CLONE_NEWUSER
        error = ctypes.get_errno() if result == -1 else 0
        os.write(write_fd, json.dumps({"allowed": result == 0, "errno": error, "error": os.strerror(error) if error else None}).encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        answer = json.load(pipe)
    os.waitpid(pid, 0)
    return answer


def main():
    selected = {"CapInh", "CapPrm", "CapEff", "CapBnd", "NoNewPrivs", "Seccomp", "Seccomp_filters"}
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in selected:
            status[key] = value.strip()
    result = {"uid": os.getuid(), "gid": os.getgid(), "kernel": os.uname().release, "status": status}
    for operation in ["ptrace_child", "user_namespace"]:
        result[operation] = child_probe(operation)
    if len(sys.argv) == 2:
        probe = subprocess.run(
            [sys.argv[1], "-r", "/", "-w", "/tmp", "/bin/sh", "-c", "test $(id -u) -ne 0 && printf proot-ok"],
            text=True, capture_output=True, timeout=20,
        )
        result["proot"] = {"exit_code": probe.returncode, "stdout": probe.stdout, "stderr": probe.stderr}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
