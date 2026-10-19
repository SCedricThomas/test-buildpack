"""Explicit single-stage Dockerfile subset; unsupported syntax fails closed."""

import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

from image import BuildError, archive_path, guest_path, remove


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
OPS = {"FROM", "ARG", "ENV", "WORKDIR", "COPY", "ADD", "RUN", "CMD", "ENTRYPOINT", "USER", "EXPOSE", "LABEL"}


def words(text, env):
    result, token, quote, started = [], "", None, False
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() and quote is None:
            if started:
                result.append(token)
                token, started = "", False
        elif char in "\"'" and (quote is None or quote == char):
            quote = char if quote is None else None
            started = True
        elif char == "\\" and quote != "'":
            index += 1
            if index == len(text):
                raise BuildError("unfinished escape")
            token += text[index]
            started = True
        elif char == "$" and quote != "'":
            match = re.match(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", text[index:])
            if not match:
                raise BuildError("only $NAME and ${NAME} substitutions are supported")
            token += env.get(match[1] or match[2], "")
            index += len(match[0]) - 1
            started = True
        else:
            token += char
            started = True
        index += 1
    if quote:
        raise BuildError("unclosed quote")
    if started:
        result.append(token)
    return result


def command(value, shell=None):
    if value.startswith("["):
        try:
            argv = json.loads(value)
        except ValueError as exc:
            raise BuildError("invalid JSON command") from exc
        if not isinstance(argv, list) or not all(isinstance(v, str) and "\0" not in v for v in argv):
            raise BuildError("exec form must be a JSON array of strings")
        if argv and not argv[0]:
            raise BuildError("empty executable")
        return argv
    return (shell or ["/bin/sh", "-c"]) + [value]


def parse(path):
    result, pending, start = [], "", 0
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            if re.match(r"#\s*(syntax|escape|check)\s*=", line, re.I):
                raise BuildError(f"line {number}: parser directives are unsupported")
            continue
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line, pending = pending + line, ""
        match = re.fullmatch(r"([A-Za-z]+)\s+(.+)", line)
        if not match:
            raise BuildError(f"line {start}: expected INSTRUCTION arguments")
        op, value = match[1].upper(), match[2]
        if op not in OPS:
            raise BuildError(f"line {start}: {op} is unsupported")
        if op in {"CMD", "ENTRYPOINT"} and not value.startswith("["):
            raise BuildError(f"line {start}: native runner requires JSON exec form for {op}")
        if op == "USER":
            raise BuildError(f"line {start}: USER is unsupported; commands run as Scalingo user")
        if value.startswith("--") or (not value.startswith("[") and "<<" in value):
            raise BuildError(f"line {start}: instruction flags and heredocs are unsupported")
        if op in {"RUN", "CMD", "ENTRYPOINT"}:
            argv = command(value)
            if op == "RUN" and not argv:
                raise BuildError(f"line {start}: RUN needs a command")
        result.append((start, op, value))
    if pending:
        raise BuildError(f"line {start}: unfinished continuation")
    if sum(op == "FROM" for _, op, _ in result) != 1:
        raise BuildError("exactly one FROM is required; multi-stage builds are unsupported")
    found = False
    for number, op, _ in result:
        if op == "FROM":
            found = True
        elif not found and op != "ARG":
            raise BuildError(f"line {number}: only ARG may precede FROM")
    return result


class Ignore:
    def __init__(self, source):
        self.rules = []
        path = source / ".dockerignore"
        if not path.exists():
            return
        if path.is_symlink():
            raise BuildError(".dockerignore must not be a symlink")
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line == ".":
                continue
            include = line.startswith("!")
            pattern = (line[1:] if include else line).strip("/")
            if not pattern or ".." in PurePosixPath(pattern).parts or any(c in pattern for c in "[]\\"):
                raise BuildError(".dockerignore supports paths, *, ?, ** and ! only")
            self.rules.append((pattern.split("/"), include))

    @staticmethod
    def matches(pattern, parts):
        if not pattern:
            return not parts
        if pattern[0] == "**":
            return Ignore.matches(pattern[1:], parts) or bool(parts) and Ignore.matches(pattern, parts[1:])
        return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and Ignore.matches(pattern[1:], parts[1:])

    def excluded(self, relative):
        parts = PurePosixPath(relative).parts
        if parts and parts[0] in {".git", "Dockerfile", ".dockerignore", ".docker-runtime"}:
            return True
        ignored = False
        for pattern, include in self.rules:
            if any(self.matches(pattern, parts[:i]) for i in range(1, len(parts) + 1)):
                ignored = not include
        return ignored


def copy(source, root, cwd, value, env, ignore, op="COPY"):
    paths = json.loads(value) if value.startswith("[") else words(value, env)
    if not isinstance(paths, list) or len(paths) < 2 or not all(isinstance(x, str) and x and "\0" not in x for x in paths):
        raise BuildError(f"{op} requires sources and a destination")
    sources, destination = paths[:-1], paths[-1]
    target = guest_path(root, str(PurePosixPath(cwd) / destination))
    directory = destination.endswith("/") or target.is_dir()
    if len(sources) > 1 and not directory:
        raise BuildError(f"multiple {op} sources require a directory destination ending in /")

    def put(origin, logical):
        output = guest_path(root, logical)
        if origin.is_symlink():
            raise BuildError(f"{op} of source symlinks is unsupported")
        if origin.is_dir():
            output.mkdir(parents=True, exist_ok=True)
        elif origin.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            # Break image hardlinks before replacing a file.
            if output.is_file():
                remove(output)
            shutil.copy2(origin, output)
            output.chmod((origin.stat().st_mode & 0o777) | 0o600)
        else:
            raise BuildError(f"{op} supports regular files and directories only")

    logical_dest = PurePosixPath(cwd) / destination
    for name in sources:
        if op == "ADD" and name.startswith(("http://", "https://")):
            url = urllib.parse.urlsplit(name)
            if not url.netloc or url.username or url.password or url.fragment or url.path.endswith(".git"):
                raise BuildError("ADD requires an HTTP(S) file URL without credentials or fragments; Git sources are unsupported")
            filename = PurePosixPath(url.path).name
            if directory and (not filename or filename in {".", ".."}):
                raise BuildError("ADD URL needs a filename for a directory destination")
            logical = str(logical_dest / filename if directory else logical_dest)
            # Download completely before replacing any existing destination.
            with tempfile.TemporaryDirectory(prefix="docker-add-") as temporary:
                download = Path(temporary) / "download"
                with urllib.request.urlopen(name, timeout=60) as response, download.open("wb") as output:
                    if urllib.parse.urlsplit(response.geturl()).scheme not in {"http", "https"}:
                        raise BuildError("ADD redirects must use HTTP(S)")
                    shutil.copyfileobj(response, output)
                download.chmod(0o600)
                put(download, logical)
            continue
        if op == "ADD" and ("://" in name or name.startswith("git@")):
            raise BuildError("ADD supports local paths and HTTP(S) file URLs only; Git sources are unsupported")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or any(c in name for c in "*?[]"):
            raise BuildError(f"{op} sources must be literal relative paths; globs and .. are unsupported")
        origin = source / name
        current = source
        for part in parts:
            current /= part
            if current.is_symlink():
                raise BuildError(f"{op} of source symlinks is unsupported")
        if not origin.exists() or ignore.excluded(origin.relative_to(source).as_posix()):
            raise BuildError(f"{op} source missing or ignored: {name}")
        if op == "ADD" and origin.is_file() and tarfile.is_tarfile(origin):
            try:
                with tarfile.open(origin) as archive:
                    members = archive.getmembers()
                    # Validate before writing; links and special files are outside
                    # this runner's supported source-file subset.
                    for member in members:
                        archive_path(member.name)
                        if not (member.isdir() or member.isfile()):
                            raise BuildError("ADD archives support regular files and directories only")
                    guest_path(root, str(logical_dest)).mkdir(parents=True, exist_ok=True)
                    for member in members:
                        output = guest_path(root, str(logical_dest / member.name))
                        if member.isdir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            if output.is_file():
                                remove(output)
                            with archive.extractfile(member) as stream, output.open("wb") as target:
                                shutil.copyfileobj(stream, target)
                            output.chmod((member.mode & 0o777) | 0o600)
            except tarfile.TarError as exc:
                raise BuildError(f"invalid ADD archive: {exc}") from exc
        elif origin.is_dir():
            put(origin, str(logical_dest))
            for folder, dirs, files in os.walk(origin, followlinks=False):
                for name in dirs + files:
                    item = Path(folder) / name
                    if not ignore.excluded(item.relative_to(source).as_posix()):
                        put(item, str(logical_dest / item.relative_to(origin)))
        else:
            put(origin, str(logical_dest / origin.name if directory else logical_dest))
