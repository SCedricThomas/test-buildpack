#!/usr/bin/env python3
"""Verify APT and APK extraction without root, then relocate an APT application."""
import argparse
import json
import uuid
from integration import ROOT, RESTRICTIONS, STACK, docker

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('managers', nargs='*', choices=['apt', 'apk'])
managers = parser.parse_args().managers or ['apt', 'apk']

volume = 'docker-buildpack-packages-' + uuid.uuid4().hex
docker('volume', 'create', volume, capture=True)
try:
    docker('run', '--rm', '-v', volume + ':/artifacts', '--entrypoint', '/bin/sh', STACK,
           '-c', 'chown 10000:10000 /artifacts')
    for manager in managers:
        docker('run', '--rm', *RESTRICTIONS, '-v', str(ROOT) + ':/buildpack:ro',
               '-v', volume + ':/artifacts', '--entrypoint', 'python3', STACK,
               '/buildpack/tests/package_sample.py', manager)
    if 'apt' in managers:
        output = docker('run', '--rm', *RESTRICTIONS,
                        '--mount', f'type=volume,src={volume},dst=/app,volume-subpath=apt',
                        '--entrypoint', '/app/.docker-runtime/launch',
                        'scalingo/scalingo-26-minimal:latest', capture=True).stdout
        assert json.loads(output) == {'answer': 42}, output
        print('PASS APT: extracted executable and libraries work after relocation on minimal runtime', flush=True)
finally:
    docker('volume', 'rm', volume, capture=True)
