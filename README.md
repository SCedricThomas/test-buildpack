# Scalingo Dockerfile buildpack

Build and run a single-stage Dockerfile through Scalingo's `BUILDPACK_URL`.
The buildpack downloads the actual `FROM` image, unpacks its filesystem and
dependencies, builds the application inside it, and includes that filesystem
in the Scalingo application output. It does not require a Docker daemon,
privileged containers, mounts, or user namespaces.

This is an experimental buildpack, tested locally in Scalingo stack containers.
An actual Scalingo deployment remains to be validated. Linux **amd64** is the
only supported architecture in this version.

## Deploy

Publish this repository to a Git URL reachable by Scalingo, then configure your
application (replace the URL and app name):

```sh
scalingo --app my-app env-set BUILDPACK_URL=https://github.com/YOUR-ORG/scalingo-docker-buildpack
```

Deploy a repository with a `Dockerfile` at its root through your usual Scalingo
Git deployment. Use this buildpack on its own, not in a multi-buildpack chain.
For a monorepo, Scalingo's `PROJECT_DIR` selects the application context before
the buildpack runs.

The Dockerfile/image's `ENTRYPOINT` and `CMD` become the default `web` process.
Applications must bind to **`0.0.0.0:$PORT`**; `EXPOSE` does not select Scalingo's
port. Existing images' `ENV`, `WORKDIR`, `USER`, `ENTRYPOINT`, `CMD`, and shell
configuration are inherited.

A source `Procfile` can override the default or add workers. Its commands run
**inside the image filesystem**, using its working directory and environment:

```procfile
web: java -cp /app/out Main
worker: /app/worker
```

These overrides bypass the image entrypoint. One-off commands can explicitly
enter the image runtime:

```sh
scalingo --app my-app run /app/.docker-runtime/launch -- /bin/sh
```

Without this launcher, one-off commands run in the outer Scalingo stack.

## How it works

1. `bin/detect` looks for `Dockerfile`.
2. `bin/compile` validates the supported instruction set and installs
   SHA-256-pinned `crane` and PRoot tools into the deployment cache.
3. `crane` resolves `FROM` to a platform-specific digest, then downloads and
   merges the image layers, including their deletions/whiteouts.
4. The buildpack extracts the merged filesystem without root, preserving
   guest symlinks and hardlinks. Absolute guest symlinks are resolved inside
   that filesystem during extraction and `COPY`, never against the host root.
5. PRoot runs `RUN` instructions with the image's libraries, tools and paths.
   Toolchains come from the image; application dependencies are installed by
   the Dockerfile's commands, such as `cargo`, `go`, `mvn`, or `npm`.
6. The source directory is replaced with `.docker-runtime/`, containing the
   resulting filesystem, PRoot, runtime configuration, and a Ruby launcher.
7. `bin/release` supplies a default `web` process. The launcher runs the image's
   command, passes application environment variables, and forwards signals.

Image paths such as `/usr/local/go`, `/opt/java/openjdk`, and `/app` remain valid
inside PRoot. Nothing relies on the host stack providing the image's libraries.
Python 3 is required only at build time; runtime uses the Ruby supplied by the
Scalingo stack. Only tools are cached: image files and registry credentials are
not retained in the deployment cache. Images are resolved anew on each build;
pin `FROM ...@sha256:...` for reproducible base images.

## Supported subset

| Instruction | Support |
| --- | --- |
| `FROM` | One Linux amd64 registry image; tags and digests; global ARG substitution |
| `RUN` | Shell or JSON exec form, executed inside the extracted image |
| `COPY` | Literal relative sources, directories, multiple sources, shell/JSON forms |
| `WORKDIR` | Absolute or relative guest path |
| `ARG` | Defaults and explicit overrides; redeclare globals after FROM to use in RUN |
| `ENV` | `KEY=value` form; inherited by later build steps and runtime |
| `CMD`, `ENTRYPOINT` | Shell and JSON exec forms; inherited image defaults supported |
| `USER` | Name/UID and optional group/GID from the image; simulated identity |
| `EXPOSE` | Validated, informational only |
| `LABEL` | Accepts `KEY=value`; does not change Scalingo image labels |

Variable expansion in word-based instructions supports `$NAME` and `${NAME}`,
quotes, and escapes. JSON `COPY` paths are literal in this version. Shell-form
`RUN` uses the image shell and its normal shell expansion; JSON commands do not
expand variables.

`.dockerignore` supports relative paths, `*`, `?`, `**`, comments, and ordered
`!` negation. Patterns are relative to the context root (`**/*.key` matches at
any depth). Bracket patterns and backslashes are rejected. `.git`, `Dockerfile`,
`.dockerignore`, and the reserved `.docker-runtime` directory are never copied.
Source symlinks and COPY glob sources are rejected; symlinks in base images are
supported. Keep credentials out of the context or exclude them explicitly.

Unsupported syntax fails rather than being silently interpreted: multi-stage
builds, `scratch`, stage aliases, instruction flags (`--mount`, `--from`,
`--chown`, etc.), heredocs, parser directives, `ADD`, `VOLUME`, `ONBUILD`,
`SHELL`, `HEALTHCHECK`, and `STOPSIGNAL`. Base images declaring `ONBUILD`,
volumes, or a non-SIGTERM stop signal are rejected. Inherited health checks
are reported but not executed; Scalingo manages application readiness.

## Configuration

| Scalingo environment variable | Meaning |
| --- | --- |
| `DOCKER_BUILD_ARGS` | JSON object of string values for declared ARGs; default `{}` |
| `DOCKER_AUTH_CONFIG` | Optional Docker credential JSON containing `auths` for private registries; no external credential helpers |

```sh
scalingo --app my-app env-set 'DOCKER_BUILD_ARGS={"VERSION":"1.2.3"}'
```

Unrelated Scalingo application variables are not passed to `RUN`. Only image
environment and declared ARGs are provided. Arguments are not saved in runtime
metadata unless explicitly persisted by `ENV` or a build command. Avoid
using ARG for secrets: commands can echo them or embed them in artifacts.

At runtime, Scalingo environment values (including `PORT` and addon credentials)
override image `ENV`, except `PATH` and `HOME`, which come from the image to
avoid substituting outer-stack paths. `PROOT_*` settings are reserved.
Registry-auth and build-argument control variables are not forwarded from the
outer Scalingo environment into the launched application.

## Container restrictions and limitations

Scalingo documents that `CAP_SYS_ADMIN` is not granted and seccomp blocks user
namespace creation. That rules out ordinary nested rootless BuildKit:
[Scalingo's container restriction details](https://doc.scalingo.com/security/bulletins/ssb-2026-002).
The local Scalingo builder code also runs buildpacks as an unprivileged user.

[PRoot](https://proot-me.github.io/) uses `ptrace` on its child processes instead
of mounts or namespaces. The local capability probe passed with UID/GID 10000,
all capabilities dropped, default Docker seccomp and `no-new-privileges`.
This is evidence for the local host, not a guarantee about Scalingo's deployed
kernel, AppArmor or seccomp configuration. Build and startup perform a PRoot
probe and fail clearly if tracing is blocked. The buildpack never requests
extra capabilities or disables platform security settings.

PRoot is a compatibility layer, **not a security boundary or a full container
runtime**. The enclosing Scalingo container provides isolation. PRoot shares its
network and kernel, and receives `/dev`, `/proc`, `/sys`, and DNS/hosts files
from it. No Docker socket, host home directory, or host temporary directory is
bound into the guest. Root/USER identities are simulated; real ownership stays
with Scalingo's user and extracted files gain owner read/write permissions.
Setuid bits and image file capabilities are not reproduced. Software requiring
real capabilities, systemd, kernel mounts, or device creation is unsupported.
Tracing also adds overhead, especially to syscall-heavy builds and workloads.

The entire resulting filesystem is shipped. Remove compiler caches/tools in a
final `RUN` when appropriate. Scalingo currently documents a 30-minute build
limit and a 2-GiB application-image limit:
[deployment limits](https://doc.scalingo.com/platform/deployment/limits).
The outer stack also contributes to the final image. There is no automatic
dependency pruning or conversion to a minimal runtime image.

## Samples and tests

`examples/go`, `examples/rust`, and `examples/java` contain independent HTTP apps
with source-building Dockerfiles. Each serves `/` and `/health`, uses only the
language standard library, and responds on `0.0.0.0:$PORT` (default 8080).

```sh
# Docker baseline:
docker build -t scalingo-sample-go examples/go
docker run --rm --user 10000:10000 -e PORT=8080 -p 127.0.0.1:8080:8080 scalingo-sample-go

# Pure-Python tests (no network or Docker):
python3 -m unittest discover -s tests -p 'test_*.py'

# Baseline source-build and HTTP checks for all three Dockerfiles:
python3 tests/smoke.py

# Actual buildpack, including a separate runtime container:
python3 tests/integration.py
# Or select examples:
python3 tests/integration.py rust java
# Verify runtime independence from full-stack language tools:
python3 tests/integration.py --runtime-image scalingo/scalingo-26-minimal:latest java
```

Integration tests require Docker with volume-subpath support and internet
access. They run compilation and startup as UID 10000 with **all capabilities
dropped**, default seccomp, no-new-privileges, and a 256-process limit. They
check a PORT override, HTTP responses, artifact relocation, and SIGTERM delivery.
Test volumes/containers are removed afterward; downloaded Docker images remain.

`tests/capabilities.py` can be run inside a real Scalingo build/runtime container
to report capability masks, seccomp status, child tracing and user namespaces.
It accepts an optional PRoot binary path to test execution too.

## Tool provenance

`tools.json` pins downloads by SHA-256. Crane uses a versioned upstream release.
The PRoot upstream binary URL is rolling; a changed binary fails checksum
verification until the manifest is deliberately updated. A cached verified
download remains usable. Version: `5.4.1-32-g25dc6a3-25dc6a31`.

- [crane / go-containerregistry](https://github.com/google/go-containerregistry): Apache-2.0.
- [PRoot](https://gitlab.com/proot/proot): GPL-2.0-or-later; corresponding source
  revision begins `25dc6a31`. See upstream for source and license terms when
  redistributing the bundled runtime binary.
