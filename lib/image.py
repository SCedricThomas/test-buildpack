"""Download verified tools and unpack crane's flattened container filesystem."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import urllib.request


class BuildError(Exception):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_tools(cache, manifest):
    tools = {}
    for name, spec in manifest.items():
        folder = cache / "tools" / spec["sha256"]
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "download"
        if not archive.exists() or sha256(archive) != spec["sha256"]:
            print(f"-----> Downloading {name} {spec['version']}", flush=True)
            temporary = archive.with_suffix(".partial")
            try:
                with urllib.request.urlopen(spec["url"], timeout=60) as source, temporary.open("wb") as target:
                    shutil.copyfileobj(source, target)
                if sha256(temporary) != spec["sha256"]:
                    raise BuildError(f"{name} checksum mismatch; update tools.json only after verifying the upstream release")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
        binary = folder / name
        if "member" in spec:
            with tarfile.open(archive) as tar:
                member = tar.getmember(spec["member"])
                if not member.isfile():
                    raise BuildError(f"invalid {name} archive")
                with tar.extractfile(member) as source, binary.open("wb") as target:
                    shutil.copyfileobj(source, target)
        else:
            shutil.copyfile(archive, binary)
        binary.chmod(0o755)
        tools[name] = binary
    return tools


def guest_path(root, path, follow_final=True):
    """Resolve links as the guest would, never following absolute links on host."""
    pending = list(PurePosixPath(path).parts)
    resolved = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in {"/", "", "."}:
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink() and (pending or follow_final):
            links += 1
            if links > 40:
                raise BuildError(f"too many symlinks resolving guest path {path}")
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending = list(PurePosixPath(target).parts) + pending
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def archive_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\0" in name:
        raise BuildError("unsafe path in image archive")
    return path.as_posix()


def unpack(archive, root):
    """Extract a flattened tar without chown, device creation or host escapes."""
    root.mkdir()
    hardlinks = []
    with tarfile.open(archive) as tar:
        for member in tar:
            name = archive_path(member.name)
            if name == ".":
                continue
            path = guest_path(root, name, follow_final=False)
            if path == root:
                raise BuildError("image archive entry resolves to root")
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                if path.is_symlink() or path.exists() and not path.is_dir():
                    remove(path)
                path.mkdir(exist_ok=True)
                path.chmod((member.mode & 0o777) | 0o700)
            elif member.issym():
                remove(path)
                path.symlink_to(member.linkname)
            elif member.islnk():
                hardlinks.append((name, archive_path(member.linkname)))
            elif member.isfile():
                remove(path)
                with tar.extractfile(member) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                # Actual ownership stays with the unprivileged Scalingo user.
                path.chmod((member.mode & 0o777) | 0o600)
            elif member.isdev():
                # /dev is supplied by the enclosing Scalingo container.
                if PurePosixPath(name).parts[0] != "dev":
                    raise BuildError("device entries outside /dev are unsupported")
            else:
                raise BuildError(f"unsupported image archive entry: {name}")
    while hardlinks:
        remaining = []
        for name, link in hardlinks:
            path = guest_path(root, name, follow_final=False)
            target = guest_path(root, link)
            if not target.is_file():
                remaining.append((name, link))
                continue
            remove(path)
            os.link(target, path)
        if len(remaining) == len(hardlinks):
            raise BuildError("image contains unresolved hardlinks")
        hardlinks = remaining


def pull(crane, reference, workspace, root, auth=None):
    if reference.startswith("-") or "://" in reference or any(c.isspace() for c in reference):
        raise BuildError("FROM requires an OCI registry image reference")
    authdir = workspace / "auth"
    authdir.mkdir(mode=0o700)
    if auth:
        # Credential helpers are intentionally excluded; accept registry auths only.
        data = json.loads(auth)
        if not isinstance(data, dict) or not isinstance(data.get("auths"), dict):
            raise BuildError("DOCKER_AUTH_CONFIG must contain an auths object")
        authfile = authdir / "config.json"
        authfile.write_text(json.dumps({"auths": data["auths"]}))
        authfile.chmod(0o600)
    env = {"PATH": os.defpath, "HOME": str(authdir), "DOCKER_CONFIG": str(authdir)}

    def run(*args, capture=True):
        process = subprocess.run([str(crane), "--platform", "linux/amd64", *args], env=env, text=True, stdout=subprocess.PIPE if capture else None, check=False)
        if process.returncode:
            raise BuildError(f"crane {args[0]} failed; check the image reference and registry access")
        return process.stdout.strip() if capture else None

    print(f"-----> Resolving FROM {reference} for linux/amd64", flush=True)
    digest = run("digest", reference)
    # Replace a tag or supplied digest with the resolved platform digest.
    repository = reference.split("@")[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    pinned = repository + "@" + digest
    print(f"       Resolved {pinned}", flush=True)
    config = json.loads(run("config", pinned))
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise BuildError("only native linux/amd64 images are supported")
    archive = workspace / "rootfs.tar"
    print("-----> Downloading and merging image layers", flush=True)
    run("export", pinned, str(archive), capture=False)
    print("-----> Unpacking image filesystem", flush=True)
    unpack(archive, root)
    archive.unlink()
    shutil.rmtree(authdir)
    return pinned, config.get("config") or {}
