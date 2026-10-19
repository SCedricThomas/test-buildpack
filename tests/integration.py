#!/usr/bin/env python3
"""Compile with the buildpack, run its output under Scalingo-like restrictions."""
import argparse
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
STACK = 'scalingo/scalingo-26:latest'
RESTRICTIONS = ['--user', '10000:10000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '256', '--security-opt', 'seccomp=' + str(ROOT / 'tests/no-ptrace.json')]


def docker(*args, capture=False):
    return subprocess.run(['docker', *args], check=True, text=True, capture_output=capture)


def check(language, cache, runtime_image):
    volume = 'docker-buildpack-test-' + uuid.uuid4().hex
    container = None
    docker('volume', 'create', volume, capture=True)
    try:
        # Provision empty volumes, just as Scalingo prepares ownership before
        # handing control to an unprivileged buildpack.
        docker('run', '--rm', '-v', volume + ':/artifacts', '-v', cache + ':/cache',
               '--entrypoint', '/bin/sh', STACK, '-c', 'chown 10000:10000 /artifacts /cache')
        docker('run', '--rm', *RESTRICTIONS,
               '-v', str(ROOT) + ':/buildpack:ro', '-v', volume + ':/artifacts', '-v', cache + ':/cache',
               '--entrypoint', 'python3', STACK, '/buildpack/tests/build_sample.py', language)
        container = docker('run', '--detach', *RESTRICTIONS, '--env', 'PORT=9090',
                           '--publish', '127.0.0.1::9090',
                           '--mount', f'type=volume,src={volume},dst=/app,volume-subpath=app',
                           '--entrypoint', '/app/.docker-runtime/launch', runtime_image, capture=True).stdout.strip()
        address = docker('port', container, '9090/tcp', capture=True).stdout.strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 60
        while True:
            try:
                with opener.open('http://' + address + '/health', timeout=2) as response:
                    assert response.status == 200 and response.read() == b'ok\n'
                break
            except (OSError, urllib.error.URLError):
                if docker('inspect', '--format', '{{.State.Running}}', container, capture=True).stdout.strip() != 'true':
                    raise RuntimeError('runtime exited before becoming ready')
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        with opener.open('http://' + address + '/', timeout=5) as response:
            assert response.read() == f'Hello from {language.title()}\n'.encode()
        # SIGTERM goes to the launcher (PID 1), not directly to the app.
        docker('kill', '--signal', 'TERM', container, capture=True)
        deadline = time.monotonic() + 10
        while docker('inspect', '--format', '{{.State.Running}}', container, capture=True).stdout.strip() == 'true':
            if time.monotonic() >= deadline:
                raise AssertionError('launcher failed to stop on SIGTERM')
            time.sleep(0.2)
        print(f'PASS buildpack {language}: image pull, source build, relocated runtime, HTTP, PORT, SIGTERM; ptrace denied, no capabilities', flush=True)
    finally:
        if container:
            try:
                docker('logs', container)
            finally:
                docker('rm', '--force', container, capture=True)
        docker('volume', 'rm', volume, capture=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', default=STACK, help='outer runtime image (for example scalingo/scalingo-26-minimal:latest)')
    parser.add_argument('languages', nargs='*', choices=['go', 'rust', 'java'])
    args = parser.parse_args()
    cache = 'docker-buildpack-cache-' + uuid.uuid4().hex
    docker('volume', 'create', cache, capture=True)
    try:
        for language in args.languages or ['go', 'rust', 'java']:
            check(language, cache, args.runtime_image)
    finally:
        docker('volume', 'rm', cache, capture=True)
