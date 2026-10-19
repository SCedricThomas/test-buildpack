#!/usr/bin/env python3
"""Package-wrapper fixtures executed as the buildpack's unprivileged user."""
import ctypes
import errno
from pathlib import Path
import subprocess
import sys

libc = ctypes.CDLL(None, use_errno=True)
assert libc.ptrace(0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM
manager = sys.argv[1]
source = Path('/artifacts') / manager
source.mkdir()
cache = Path('/artifacts/cache')
cache.mkdir(exist_ok=True)
env = Path('/artifacts/env')
env.mkdir(exist_ok=True)
if manager == 'apt':
    dockerfile = '''FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends jq libjq1 libonig5
RUN jq -n '{"answer":42}' > answer.json
RUN apt-get clean
CMD ["jq", "-n", "{\\"answer\\":42}"]
'''
else:
    dockerfile = '''FROM alpine:3.22
RUN ["apk", "update"]
RUN ["apk", "add", "--no-cache", "zlib-dev"]
CMD ["/bin/busybox"]
'''
(source / 'Dockerfile').write_text(dockerfile)
subprocess.run(['/buildpack/bin/compile', str(source), str(cache), str(env)], check=True)
runtime = source / '.docker-runtime'
assert not (runtime / 'package-state').exists()
for helper in ['build-bin', 'apk-static', 'package_manager.py', 'image.py', '__pycache__']:
    assert not (runtime / helper).exists(), helper
if manager == 'apk':
    assert (runtime / 'packages/apk/usr/include/zlib.h').is_file()
    assert (runtime / 'packages/apk/usr/lib/libz.so').is_file()
    assert not (runtime / 'packages/apk/.PKGINFO').exists()
    # The package prefix is deliberately not on the glibc loader's search path.
print(f'PASS {manager}: unprivileged package resolution and extraction', flush=True)
