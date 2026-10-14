#!/usr/bin/env python3
"""Scalingo compile entrypoint: registry image + Dockerfile -> packaged rootfs."""

import json
import os
from pathlib import Path
import platform
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from dockerfile import Ignore, NAME, command, copy, parse, words
from image import BuildError, guest_path, install_tools, pull, remove


DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def identity(root, user):
    user, _, group = (user or "0").partition(":")
    passwd = guest_path(root, "/etc/passwd")
    accounts = [line.split(":") for line in passwd.read_text().splitlines()] if passwd.exists() else []
    entry = next((row for row in accounts if len(row) == 7 and (row[0] == user or row[2] == user)), None)
    uid = user if user.isdecimal() else entry[2] if entry else None
    gid = entry[3] if entry else "0"
    if group:
        if group.isdecimal():
            gid = group
        else:
            groupfile = guest_path(root, "/etc/group")
            groups = [line.split(":") for line in groupfile.read_text().splitlines()] if groupfile.exists() else []
            found = next((row for row in groups if len(row) >= 3 and row[0] == group), None)
            if not found:
                raise BuildError(f"unknown image group: {group}")
            gid = found[2]
    if uid is None or not uid.isdecimal() or not gid.isdecimal():
        raise BuildError(f"unknown image user: {user}")
    return uid + ":" + gid, entry[5] if entry else "/root" if uid == "0" else "/"


def proot_args(binary, root, cwd, user):
    return [str(binary), "--kill-on-exit", "-r", str(root), "-i", user, "-w", cwd,
            "-b", "/dev", "-b", "/proc", "-b", "/sys",
            "-b", "/etc/hosts:/etc/hosts!", "-b", "/etc/resolv.conf:/etc/resolv.conf!"]


def check_proot(binary):
    check = subprocess.run([str(binary), "-r", "/", "-w", "/", "/bin/true"],
                           env={"PATH": DEFAULT_PATH}, capture_output=True, timeout=20)
    if check.returncode:
        raise BuildError("PRoot cannot trace a child process. This builder's ptrace/seccomp policy is incompatible; no capability escalation is attempted. " + check.stderr.decode(errors="replace").strip())


def argument(value, env, overrides, inherited):
    key, separator, default = value.partition("=")
    if not NAME.fullmatch(key):
        raise BuildError("ARG requires NAME or NAME=value")
    expanded = words(default, env) if separator else []
    if len(expanded) > 1:
        raise BuildError("quote ARG defaults containing spaces")
    fallback = expanded[0] if expanded else "" if separator else inherited.get(key, "")
    return key, overrides.get(key, fallback)


def assignments(value, env):
    values = words(value, env)
    result = {}
    for item in values:
        key, sep, val = item.partition("=")
        if not sep or not NAME.fullmatch(key):
            raise BuildError("expected NAME=value assignments")
        result[key] = val
    if not result:
        raise BuildError("expected NAME=value assignments")
    return result


def build(source, runtime, workspace, tools, plan, overrides, auth):
    args, global_args, env = {}, {}, {}
    root = runtime / "rootfs"
    cwd, user, entrypoint, cmd, shell = "/", "0", [], [], ["/bin/sh", "-c"]
    explicit_cmd, in_stage = False, False
    ignored = Ignore(source)
    pinned = None
    for line, op, value in plan:
        print(f"-----> Dockerfile line {line}: {op}", flush=True)
        try:
            if op == "ARG":
                target = args if in_stage else global_args
                key, val = argument(value, args | env if in_stage else global_args, overrides, global_args)
                target[key] = val
            elif op == "FROM":
                references = words(value, global_args)
                if len(references) != 1 or references[0] == "scratch":
                    raise BuildError("FROM requires one registry image; scratch, stage aliases and multiple stages are unsupported")
                pinned, config = pull(tools["crane"], references[0], workspace, root, auth)
                if config.get("OnBuild") or config.get("Volumes"):
                    raise BuildError("base images with ONBUILD or VOLUME declarations are unsupported")
                if config.get("StopSignal") not in (None, "", "SIGTERM", "15"):
                    raise BuildError("base image requires a non-SIGTERM stop signal, unsupported on this runtime")
                if config.get("Healthcheck"):
                    print("       Image HEALTHCHECK is not executed; Scalingo manages readiness", flush=True)
                for item in config.get("Env") or []:
                    key, separator, val = item.partition("=")
                    if not separator or not NAME.fullmatch(key):
                        raise BuildError("invalid base-image environment")
                    env[key] = val
                env.setdefault("PATH", DEFAULT_PATH)
                cwd = config.get("WorkingDir") or "/"
                user = config.get("User") or "0"
                entrypoint, cmd = config.get("Entrypoint") or [], config.get("Cmd") or []
                shell = config.get("Shell") or shell
                for field in (entrypoint, cmd, shell):
                    if not isinstance(field, list) or not all(isinstance(arg, str) and "\0" not in arg for arg in field):
                        raise BuildError("invalid base-image command configuration")
                for folder in (cwd, "/tmp", "/dev", "/proc", "/sys", "/etc"):
                    guest_path(root, folder).mkdir(parents=True, exist_ok=True)
                in_stage = True
            elif op == "ENV":
                env.update(assignments(value, args | env))
            elif op == "WORKDIR":
                paths = words(value, args | env)
                if len(paths) != 1:
                    raise BuildError("WORKDIR requires one path")
                cwd = posixpath.normpath(posixpath.join(cwd, paths[0]))
                guest_path(root, cwd).mkdir(parents=True, exist_ok=True)
            elif op == "COPY":
                copy(source, root, cwd, value, args | env, ignored)
            elif op == "RUN":
                uid, home = identity(root, user)
                runenv = {"HOME": home} | args | env
                runenv = {key: val for key, val in runenv.items() if not key.startswith("PROOT_")}
                runenv["PROOT_TMP_DIR"] = "/tmp"
                completed = subprocess.run(proot_args(tools["proot"], root, cwd, uid) + command(value, shell), env=runenv, check=False)
                if completed.returncode:
                    raise BuildError(f"RUN failed with exit code {completed.returncode}")
            elif op == "CMD":
                cmd, explicit_cmd = command(value, shell), True
            elif op == "ENTRYPOINT":
                entrypoint = command(value, shell)
                if not explicit_cmd:
                    cmd = []
            elif op == "USER":
                users = words(value, args | env)
                if len(users) != 1:
                    raise BuildError("USER requires user[:group]")
                user = users[0]
                identity(root, user)
            elif op == "EXPOSE":
                ports = words(value, args | env)
                if not ports or any(not re.fullmatch(r"\d+(/tcp|/udp)?", p) or not 1 <= int(p.split("/")[0]) <= 65535 for p in ports):
                    raise BuildError("EXPOSE requires valid ports")
                print("       EXPOSE is informational; listen on 0.0.0.0:$PORT", flush=True)
            elif op == "LABEL":
                assignments(value, args | env)
        except (OSError, ValueError, BuildError) as exc:
            raise BuildError(f"line {line} ({op}): {exc}") from exc
    argv = entrypoint + cmd
    if not argv or not argv[0]:
        raise BuildError("no startup command: supply CMD or ENTRYPOINT")
    uid, home = identity(root, user)
    env.setdefault("HOME", home)
    metadata = {"image": pinned, "argv": argv, "env": env, "workdir": cwd, "identity": uid}
    (runtime / "config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copy2(tools["proot"], runtime / "proot")
    shutil.copy2(Path(__file__).with_name("launch.rb"), runtime / "launch")
    shutil.copy2(Path(__file__).parents[1] / "licenses" / "PRoot.txt", runtime / "PRoot-LICENSE.txt")
    (runtime / "launch").chmod(0o755)


def procfile(source):
    path = source / "Procfile"
    if not path.exists():
        return None
    if path.is_symlink():
        raise BuildError("Procfile must not be a symlink")
    lines = []
    names = set()
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) or not value.strip() or name in names:
            raise BuildError("Procfile requires unique process-name: shell-command lines")
        names.add(name)
        launch = "exec /app/.docker-runtime/launch --shell " + shlex.quote(value.strip())
        lines.append(name + ": " + json.dumps(launch))
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) != 4:
        raise BuildError("usage: compile BUILD_DIR CACHE_DIR ENV_DIR")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise BuildError("this buildpack currently supports Linux x86_64 only")
    source, cache, envdir = (Path(arg).resolve() for arg in sys.argv[1:])
    if source == Path("/") or cache.is_relative_to(source) or source.is_relative_to(cache):
        raise BuildError("BUILD_DIR and CACHE_DIR must be separate directories")
    if (source / ".docker-runtime").exists():
        raise BuildError(".docker-runtime is reserved for the buildpack")
    if (source / "Dockerfile").is_symlink():
        raise BuildError("Dockerfile must not be a symlink")
    plan = parse(source / "Dockerfile")
    processes = procfile(source)

    def setting(name, default=None):
        path = envdir / name
        return path.read_text() if path.is_file() else os.environ.get(name, default)

    overrides = json.loads(setting("DOCKER_BUILD_ARGS", "{}"))
    if not isinstance(overrides, dict) or any(not NAME.fullmatch(k) or not isinstance(v, str) or "\0" in v for k, v in overrides.items()):
        raise BuildError("DOCKER_BUILD_ARGS must be a JSON object of string values")
    declared = {value.partition("=")[0] for _, op, value in plan if op == "ARG"}
    if overrides.keys() - declared:
        raise BuildError("DOCKER_BUILD_ARGS contains undeclared arguments")
    cache = cache / "docker-buildpack"
    cache.mkdir(parents=True, exist_ok=True)
    tools = install_tools(cache, json.loads(Path(__file__).parents[1].joinpath("tools.json").read_text()))
    check_proot(tools["proot"])
    # Work under BUILD_DIR's parent so final publication is a rename, not a copy
    # of a potentially large image; nothing is written into the source context.
    with tempfile.TemporaryDirectory(prefix="docker-buildpack-", dir=source.parent) as directory:
        workspace = Path(directory)
        runtime = workspace / ".docker-runtime"
        runtime.mkdir()
        build(source, runtime, workspace, tools, plan, overrides, setting("DOCKER_AUTH_CONFIG"))
        for item in source.iterdir():
            remove(item)
        runtime.rename(source / ".docker-runtime")
        if processes:
            (source / "Procfile").write_text(processes)
    print("-----> Image runtime ready for Scalingo packaging", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f" ! Dockerfile build failed: {exc}", file=sys.stderr)
        sys.exit(1)
