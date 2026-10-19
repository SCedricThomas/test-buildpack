#!/usr/bin/python3
"""Buildpack-style package extraction, never a host package installation."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from image import BuildError, guest_path, unpack


def arguments(manager, argv):
    allowed = ({'-y', '--yes', '--assume-yes', '-q', '-qq', '--quiet',
                '--no-install-recommends', '--no-install-suggests'} if manager != 'apk'
               else {'--no-cache', '-U', '--update-cache', '-q', '--quiet'})
    options, positional = [], []
    for arg in argv:
        if arg in allowed:
            options.append(arg)
        elif arg.startswith('-'):
            raise BuildError(f'{manager} wrapper: unsupported option {arg}')
        else:
            positional.append(arg)
    operations = {'update', 'add'} if manager == 'apk' else {'update', 'install', 'clean'}
    if not positional or positional[0] not in operations:
        raise BuildError(f'{manager} wrapper supports only {", ".join(sorted(operations))}; package scripts and system changes are unsupported')
    operation, packages = positional[0], positional[1:]
    if operation in {'install', 'add'}:
        if not packages or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.:=~<>-]*', name) for name in packages):
            raise BuildError('provide repository package names, optionally with versions; URLs and local packages are unsupported')
    elif packages:
        raise BuildError(f'{operation} does not accept package names')
    return operation, options, packages


def host_environment():
    # Do not load extracted package libraries into the host package managers.
    return {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LC_ALL': 'C.UTF-8',
            'SSL_CERT_FILE': '/etc/ssl/certs/ca-certificates.crt'}


def relative_links(root):
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


def apt(runtime, operation, options, packages):
    state = runtime / 'package-state/apt'
    destination = runtime / 'packages/apt'
    for directory in ['cache/archives/partial', 'state/lists/partial', 'log']:
        (state / directory).mkdir(parents=True, exist_ok=True)
    # APT_CONFIG is read before apt.conf.d: avoid host hooks and keep all mutable
    # state private, but use the stack's repositories, keys and installed status.
    settings = {
        'Dir::Etc::parts': '/dev/null', 'Dir::Etc::main': '/dev/null',
        'Dir::Etc::sourcelist': '/etc/apt/sources.list',
        'Dir::Etc::sourceparts': '/etc/apt/sources.list.d',
        'Dir::State': str(state / 'state'), 'Dir::State::status': '/var/lib/dpkg/status',
        'Dir::Cache': str(state / 'cache'), 'Dir::Log': str(state / 'log'),
        'Dir::Bin::methods': '/usr/lib/apt/methods',
        'Debug::NoLocking': 'true', 'APT::Architecture': 'amd64',
        'Acquire::Languages': 'none', 'APT::Update::Error-Mode': 'any',
    }
    config = state / 'apt.conf'
    config.write_text(''.join(f'{key} {json.dumps(value)};\n' for key, value in settings.items()))
    env = host_environment() | {'APT_CONFIG': str(config)}
    if operation == 'install':
        if not (state / 'updated').exists():
            subprocess.run(['/usr/bin/apt-get', 'update'], env=env, check=True)
            (state / 'updated').touch()
        subprocess.run(['/usr/bin/apt-get', *options, '-y', '--download-only', '--reinstall',
                        '--no-remove', 'install', *packages], env=env, check=True)
        destination.mkdir(parents=True, exist_ok=True)
        for package in sorted((state / 'cache/archives').glob('*.deb')):
            print(f'-----> Extracting {package.name} (no maintainer scripts)', flush=True)
            with tempfile.NamedTemporaryFile(dir=state, suffix='.tar') as archive:
                subprocess.run(['/usr/bin/dpkg-deb', '--fsys-tarfile', str(package)],
                               env=host_environment(), stdout=archive, check=True)
                unpack(archive.name, destination, merge=True)
            package.unlink()
        relative_links(destination)
    else:
        subprocess.run(['/usr/bin/apt-get', *options, operation], env=env, check=True)
        if operation == 'update':
            (state / 'updated').touch()


def apk(runtime, operation, options, packages):
    root = runtime / 'rootfs'
    destination = runtime / 'packages/apk'
    state = runtime / 'package-state/apk'
    config = guest_path(root, '/etc/apk')
    if not (config / 'repositories').is_file() or not (config / 'keys').is_dir():
        raise BuildError('apk wrapper requires an Alpine image with /etc/apk/repositories and signing keys')
    target = state / 'etc/apk'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config / 'repositories', target / 'repositories')
    shutil.copytree(config / 'keys', target / 'keys', dirs_exist_ok=True)
    binary = runtime / 'apk-static'
    base = [str(binary), '--root', str(state), '--arch', 'x86_64']
    if not (state / 'initialized').exists():
        # Initialize only an empty resolver database, never install packages.
        subprocess.run([*base, '--repositories-file', '/dev/null', 'add', '--initdb',
                        '--no-network', '--no-scripts', '--no-commit-hooks', '--no-chown'],
                       env=host_environment(), check=True)
        (state / 'initialized').touch()
    if operation == 'add':
        downloads = state / 'downloads'
        downloads.mkdir(exist_ok=True)
        subprocess.run([*base, *options, '--update-cache', 'fetch', '--recursive',
                        '--output', str(downloads), *packages], env=host_environment(), check=True)
        destination.mkdir(parents=True, exist_ok=True)
        for package in sorted(downloads.glob('*.apk')):
            subprocess.run([*base, 'verify', str(package)], env=host_environment(), check=True)
            print(f'-----> Extracting {package.name} (no package scripts)', flush=True)
            unpack(package, destination, merge=True, apk=True)
            package.unlink()
        relative_links(destination)
    else:
        subprocess.run([*base, *options, 'update'], env=host_environment(), check=True)


def main():
    manager, *argv = sys.argv[1:]
    operation, options, packages = arguments(manager, argv)
    runtime = Path(__file__).resolve().parent
    print(f'-----> {manager} compatibility wrapper: extracting packages only; scripts and services are not configured', flush=True)
    if manager == 'apk':
        apk(runtime, operation, options, packages)
    else:
        apt(runtime, operation, options, packages)


if __name__ == '__main__':
    try:
        main()
    except (BuildError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f'Package wrapper failed: {exc}', file=sys.stderr)
        sys.exit(1)
