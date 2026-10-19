#!/usr/bin/env python3
"""Invoked inside an unprivileged, capability-free Scalingo build container."""
from pathlib import Path
import shutil
import subprocess
import sys
import ctypes
import errno

libc = ctypes.CDLL(None, use_errno=True)
assert libc.ptrace(0, 0, 0, 0) == -1 and ctypes.get_errno() == errno.EPERM, 'test must deny ptrace'
source = Path('/artifacts/app')
shutil.copytree(Path('/buildpack/examples') / sys.argv[1], source)
if sys.argv[1] == 'go':
    # Exercise the deployment that motivated general RUN support, including
    # versioned tool installation, local installation and shell expansion.
    dockerfile = source / 'Dockerfile'
    dockerfile.write_text(dockerfile.read_text().replace(
        'RUN go build -trimpath -o /app/server .',
        'RUN go install github.com/cespare/reflex@v0.3.1\n'
        'RUN go install -buildvcs=false && test -x "$GOPATH/bin/scalingo-dockerfile-sample"'
    ).replace('CMD ["/app/server"]', 'CMD /go/bin/scalingo-dockerfile-sample'))
envdir = Path('/tmp/buildpack-env')
envdir.mkdir()
subprocess.run(['/buildpack/bin/detect', str(source)], check=True)
subprocess.run(['/buildpack/bin/compile', str(source), '/cache', str(envdir)], check=True)
with (source / '.release').open('w') as release:
    subprocess.run(['/buildpack/bin/release', str(source)], stdout=release, check=True)
