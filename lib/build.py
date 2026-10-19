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


def native_command(value, env):
    if value.startswith("["):
        return command(value)
    if re.search(r"[;&|<>`\n*?\[\](){}]", value):
        raise BuildError("shell operators, substitutions and globs are unsupported; use separate RUN instructions")
    return words(value, env)


def relocate_links(root):
    # Native tools follow symlinks themselves: convert image-absolute links
    # into relative links before executing anything. Resolve all before editing.
    links = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            link = Path(folder) / name
            if link.is_symlink():
                target = guest_path(root, '/' + link.relative_to(root).as_posix())
                links.append((link, os.path.relpath(target, link.parent)))
    for link, target in links:
        link.unlink()
        link.symlink_to(target)


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
    cwd, entrypoint, cmd = "/", [], []
    explicit_cmd, in_stage = False, False
    ignored = Ignore(source)
    pinned = None
    for name in ("launch.rb", "native.rb"):
        shutil.copy2(Path(__file__).with_name(name), runtime / ("launch" if name == "launch.rb" else name))
    (runtime / "launch").chmod(0o755)
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
                relocate_links(root)
                if config.get("User") not in (None, "", "0", "root", "0:0", "root:root"):
                    raise BuildError("image USER is unsupported; commands run as Scalingo user")
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
                entrypoint, cmd = config.get("Entrypoint") or [], config.get("Cmd") or []
                for field in (entrypoint, cmd):
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
                runenv = args | env
                argv = native_command(value, runenv)
                metadata = {"argv": argv, "env": runenv, "workdir": cwd}
                (runtime / "config.json").write_text(json.dumps(metadata))
                completed = subprocess.run(["/usr/bin/ruby", str(runtime / "launch"), "--build", str(runtime)],
                                           env={"PATH": DEFAULT_PATH}, check=False)
                if completed.returncode:
                    raise BuildError(f"RUN failed with exit code {completed.returncode}; image tools must be compatible with the Scalingo stack")
            elif op == "CMD":
                cmd, explicit_cmd = command(value), True
            elif op == "ENTRYPOINT":
                entrypoint = command(value)
                if not explicit_cmd:
                    cmd = []
            elif op == "USER":
                raise BuildError("USER is unsupported; build and runtime use Scalingo's unprivileged user")
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
    if argv[0] != "java" and not argv[0].startswith("/"):
        raise BuildError("startup requires java or an absolute image path to a native executable")
    metadata = {"image": pinned, "argv": argv, "env": env, "workdir": cwd, "mode": "native"}
    (runtime / "config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    # Validate startup before publishing; do not execute the application.
    subprocess.run(["/usr/bin/ruby", "-r", str(runtime / "native.rb"), "-e",
                    'Native.prepare(ARGV[0], JSON.parse(File.read(ARGV[1])))',
                    str(root), str(runtime / "config.json")], check=True,
                   env={"PATH": DEFAULT_PATH})


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
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        print(f" ! Dockerfile build failed: {exc}", file=sys.stderr)
        sys.exit(1)
