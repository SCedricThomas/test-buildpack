import io
import gzip
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from image import BuildError, unpack
from package_manager import arguments, apt, apk, host_environment, relative_links


class PackageTests(unittest.TestCase):
    def test_supported_package_commands(self):
        self.assertEqual(arguments('apt-get', ['-y', 'install', '--no-install-recommends', 'jq']),
                         ('install', ['-y', '--no-install-recommends'], ['jq']))
        self.assertEqual(arguments('apk', ['add', '--no-cache', 'zlib-dev']),
                         ('add', ['--no-cache'], ['zlib-dev']))
        for manager, argv in [('apt-get', ['upgrade']), ('apk', ['del', 'musl']),
                              ('apt-get', ['install', '-o', 'Dir=/']),
                              ('apk', ['add', '--allow-untrusted', 'curl']),
                              ('apt-get', ['install', '/tmp/package.deb']),
                              ('apt-get', ['install']), ('apk', ['update', 'curl'])]:
            with self.subTest(manager=manager, argv=argv), self.assertRaises(BuildError):
                arguments(manager, argv)

    def test_apt_uses_download_only_and_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch('package_manager.subprocess.run') as run:
                apt(runtime, 'install', ['--no-install-recommends'], ['jq'])
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ['/usr/bin/apt-get', 'update'])
            self.assertIn('--download-only', commands[1])
            self.assertIn('--reinstall', commands[1])
            self.assertIn('--no-remove', commands[1])
            config = (runtime / 'package-state/apt/apt.conf').read_text()
            self.assertIn('Dir::Etc::parts "/dev/null"', config)
            self.assertIn('Dir::State::status "/var/lib/dpkg/status"', config)
            self.assertIn(str(runtime / 'package-state/apt/cache'), config)
            self.assertNotIn('LD_LIBRARY_PATH', host_environment())

    def test_package_merge_preserves_directory_symlinks_and_confines_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            (root / 'usr/bin').mkdir(parents=True)
            (root / 'bin').symlink_to('usr/bin')
            archive = Path(directory) / 'package.tar'
            with tarfile.open(archive, 'w') as tar:
                folder = tarfile.TarInfo('bin')
                folder.type = tarfile.DIRTYPE
                tar.addfile(folder)
                member = tarfile.TarInfo('bin/tool')
                member.size = 5
                tar.addfile(member, io.BytesIO(b'hello'))
                link = tarfile.TarInfo('usr/bin/alias')
                link.type = tarfile.SYMTYPE
                link.linkname = '/bin/tool'
                tar.addfile(link)
            unpack(archive, root, merge=True)
            relative_links(root)
            self.assertTrue((root / 'bin').is_symlink())
            self.assertEqual((root / 'usr/bin/alias').read_bytes(), b'hello')
            moved = Path(directory) / 'moved'
            root.rename(moved)
            self.assertEqual((moved / 'bin/alias').read_bytes(), b'hello')

    def test_apk_uses_private_resolver_and_fetch_without_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            config = runtime / 'rootfs/etc/apk'
            (config / 'keys').mkdir(parents=True)
            (config / 'repositories').write_text('https://example.com/alpine/main\n')
            for operation, packages in [('update', []), ('add', ['zlib-dev'])]:
                with patch('package_manager.subprocess.run') as run:
                    apk(runtime, operation, ['--no-cache'], packages)
                calls = [call.args[0] for call in run.call_args_list]
                for cmd in calls:
                    self.assertIn(str(runtime / 'package-state/apk'), cmd)
                    self.assertNotIn('--allow-untrusted', cmd)
                    if 'add' in cmd:
                        self.assertIn('--initdb', cmd)
                        self.assertIn('--no-scripts', cmd)
                        self.assertIn('--no-chown', cmd)
                        self.assertNotIn('zlib-dev', cmd)
                if operation == 'add':
                    self.assertIn('fetch', calls[-1])
                    self.assertIn('--recursive', calls[-1])

    def test_apk_payload_excludes_control_scripts_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'package.apk'
            for index, (name, contents) in enumerate([
                ('.SIGN.RSA.key', b'signature'), ('.post-install', b'exit 1'),
                ('usr/share/example', b'payload'),
            ]):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w') as tar:
                    member = tarfile.TarInfo(name)
                    member.size = len(contents)
                    tar.addfile(member, io.BytesIO(contents))
                with archive.open('ab' if index else 'wb') as output:
                    output.write(gzip.compress(stream.getvalue()))
            root = Path(directory) / 'root'
            unpack(archive, root, apk=True)
            self.assertEqual((root / 'usr/share/example').read_bytes(), b'payload')
            self.assertFalse((root / '.post-install').exists())
            self.assertFalse((root / '.SIGN.RSA.key').exists())


if __name__ == '__main__':
    unittest.main()
