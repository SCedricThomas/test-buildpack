# Scalingo Dockerfile buildpack — native runner

Build a restricted, single-stage Dockerfile through `BUILDPACK_URL`, without
Docker, PRoot, ptrace, chroot, mounts, or user namespaces. Initial adapters cover
Go, Rust, and Java. This is experimental and is **not a general Docker runtime**.

The previous PRoot implementation failed on Scalingo because child tracing was
blocked. This version runs extracted toolchains directly on the Scalingo stack.
The tools and application must be compatible with that stack's native libraries.

## Deploy

```sh
scalingo --app my-app env-set BUILDPACK_URL=https://github.com/SCedricThomas/test-buildpack
```

Deploy a repository containing a root `Dockerfile`. Use this buildpack alone.
The samples are at https://github.com/SCedricThomas/sample on branches `go`,
`rust`, and `java`. Each listens on `0.0.0.0:$PORT` (8080 locally).
`EXPOSE` is informational; no port forwarding occurs.

## Build and runtime

1. Validate the Dockerfile and download checksum-pinned `crane` into the cache.
2. Resolve `FROM` to a Linux amd64 digest, download the actual image and merge
   its layers, including whiteouts. Optional registry auth is supported.
3. Extract the filesystem without root. Convert image symlinks to relative
   links so native tools can follow them after the artifact is relocated.
4. Apply `COPY`, `ADD`, `.dockerignore`, `WORKDIR`, `ARG`, and `ENV`.
5. Run build commands and scripts natively on the Scalingo stack, resolving
   image executables through PATH and adapting paths for simple commands.
6. Replace the deployment source directory with `.docker-runtime/`, containing
   the filesystem, metadata and Ruby launcher/adapter. This changes the build
   copy, not your Git checkout.
7. On startup, reconstruct paths for the deployed location and start the
   application directly under a Ruby supervisor that forwards signals and exits
   with the application status, including when the launcher is container PID 1.

Crane remains the downloader; changing it to Skopeo would not solve execution
restrictions. No PRoot binary is downloaded or packaged.

## Supported commands

| Dockerfile operation | Native-runner behavior |
| --- | --- |
| `FROM` | One Linux amd64 registry image; tag/digest and global ARG expansion |
| `RUN` | Arbitrary native commands and executable scripts; image PATH, then stack fallback; shell expressions use image `/bin/sh` |
| `COPY` | Literal relative files/directories, multiple sources; no source symlinks/globs |
| `ADD` | Local files/directories like COPY, local tar extraction (plain/gzip/bzip2/xz), HTTP(S) file downloads |
| `WORKDIR`, `ENV`, `ARG` | Paths and build environment; `ENV` uses KEY=value syntax |
| `CMD` | JSON exec form or a simple command with quoted arguments; native ELF executable at absolute image path, or `java` |
| `ENTRYPOINT` | JSON exec form only; native ELF executable at absolute image path, or `java` |
| `EXPOSE`, `LABEL` | Validated metadata only |

Simple `RUN` commands execute directly, with quoted arguments and path
adaptation. There is no language or subcommand whitelist: `go install`, `go test`,
`make`, and executable build scripts can run when the required tools are available
and compatible with the stack. Go defaults to `CGO_ENABLED=0`; an explicit value
is respected.

`RUN` expressions containing shell syntax or variable expansion execute through
the image's `/bin/sh -c`. This supports `&&`, pipes, redirections, substitutions,
globs, assignments and shell builtins. JSON arrays retain literal arguments;
use `RUN ["/bin/bash", "-c", "..."]` to select another installed shell explicitly.
Scripts require an executable bit and a host-compatible shebang interpreter.

Shell source and script contents are **not path-rewritten**. Use paths relative
to WORKDIR or relocated variables, for example:

```dockerfile
RUN go install github.com/cespare/reflex@latest
RUN go install -buildvcs=false && test -x "$GOPATH/bin/sample-go-gin"
RUN ./scripts/build.sh
```

An absolute path written inside a shell expression or script addresses the outer
Scalingo filesystem. These commands remain unprivileged; adding general RUN
support does not enable root package installation or emulate a Docker rootfs.

`CMD /go/bin/sample-go-gin` is supported and runs the executable directly.
Arguments can be quoted, for example `CMD java -cp /app/out Main "hello world"`.
Non-JSON `CMD` does not invoke a shell: variable expansion, operators such as
`&&` and `|`, redirections, globs, and scripts remain unsupported. JSON `CMD`
continues to support literal arguments and default arguments for `ENTRYPOINT`.

`USER`, multi-stage builds, `scratch`, stage aliases, instruction flags,
`VOLUME`, `ONBUILD`, `SHELL`, `HEALTHCHECK`, and `STOPSIGNAL` are rejected.
Base images with non-root USER declarations, volumes, ONBUILD or custom stop
signals are rejected. Root defaults do not confer root privileges: all commands
run as Scalingo's user. Inherited script entrypoints must be explicitly cleared
with `ENTRYPOINT []` (see Java sample). Image health checks are not executed.

`ADD` supports shell and JSON forms and the same local path and `.dockerignore`
restrictions as `COPY`. Local archives are detected by content and extracted into
the destination directory; only regular files and directories are accepted.
Archive paths containing `..` or absolute paths are rejected. Archives inside a
copied directory are copied unchanged. HTTP(S) URLs download a file with mode
0600 and are never automatically extracted; a destination ending in `/` uses
the URL path's filename. Git sources, URL credentials, instruction flags, source
globs, and archive links or special files are unsupported.

## What path adaptation does — and does not do

The adapter rewrites absolute command arguments and `--option=/path` arguments
into paths under the extracted filesystem. Colon-separated absolute classpath
entries are adapted too. It resolves `WORKDIR` under the same filesystem.

Known path variables (`HOME`, `JAVA_HOME`, `GOROOT`, `GOPATH`, `GOCACHE`,
`GOBIN`, `GOMODCACHE`, `CARGO_HOME`, `RUSTUP_HOME`, `CARGO_TARGET_DIR`, `TMPDIR`) are
relocated. These variables must contain a single absolute image path.
`PATH` starts with the selected image toolchains. During builds, relocated image
PATH directories follow; `/usr/bin:/bin` from the Scalingo stack are fallbacks.
Runtime PATH retains only the selected toolchains and stack directories.

This does **not** rewrite paths embedded in binaries, source code or config
files. An application that opens `/app/data` internally still opens the outer
Scalingo path; use paths relative to WORKDIR instead. Absolute command arguments
are treated as image paths, not literal strings. Relative paths passed to tools
are ordinary host-relative paths; this adapter is not a filesystem sandbox.

Executables use the host dynamic loader and native libraries. We do not rebind
image libc or install image system packages. Alpine/musl tools and binaries that
require newer or missing host libraries are not supported. Go CGO defaults off;
Rust uses the stack's linker and must target a compatible runtime stack. Java
uses the extracted JDK/JRE with host libraries. Dependencies with native system
requirements need further adapter support. Build scripts and compiler plugins
remain ordinary code executing inside the enclosing Scalingo container.

## Environment and processes

Runtime application settings, including `PORT` and addon credentials, override
image ENV. Adapter-controlled path variables and PATH cannot be overridden by
outer-stack values. `LD_*` and legacy `PROOT_*` variables are filtered.
Registry auth and build-argument settings are not inherited from the runtime's
outer environment. Application variables are not passed to RUN automatically.

- `DOCKER_BUILD_ARGS`: JSON object of string overrides for declared ARGs.
- `DOCKER_AUTH_CONFIG`: Docker credential JSON with an `auths` object; no helpers.

A Procfile can specify simple commands such as:

```procfile
web: java -cp /app/out Main
worker: /app/worker
```

Commands bypass the image entrypoint and use the same native adapter. Shell
operators, expansion and scripts are unsupported. One-off native commands can
use `/app/.docker-runtime/launch -- /app/worker`; a guest shell is not provided.

## Size, caching and reproducibility

The entire resulting image filesystem is packaged, including toolchains and
compiler caches. There is no automatic dependency pruning. Only the downloader
is cached between builds; layers and application dependencies are fetched again.
Pin FROM by digest for reproducibility. Builds require Python 3 and Ruby; runtime
requires Ruby from the Scalingo stack. Do not assume arbitrary base images work.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
ruby tests/test_native.rb
python3 tests/integration.py go rust java
python3 tests/integration.py --runtime-image scalingo/scalingo-26-minimal:latest java
```

Integration tests use UID/GID 10000, no capabilities, no-new-privileges and a
256-process limit. `tests/no-ptrace.json` explicitly denies ptrace, process_vm
calls, mounts, unshare and setns; other syscalls use an allow default. This is a
focused test profile, not a copy of Scalingo's production seccomp policy.
The build test asserts that `ptrace(TRACEME)` fails with EPERM. Build and runtime
both use the profile. Checks cover source compilation, relocation, HTTP, PORT
and SIGTERM. Actual deployment on Scalingo remains to be validated.

`tools.json` pins crane (Apache-2.0) to an upstream release and SHA-256 checksum:
https://github.com/google/go-containerregistry
