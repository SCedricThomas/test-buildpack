import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from build import argument, native_command, procfile, relocate_links
from dockerfile import Ignore, command, copy, parse, words
from image import BuildError, guest_path, unpack


class BuildpackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source'
        self.source.mkdir()

    def archive(self, members):
        archive = self.directory / 'image.tar'
        with tarfile.open(archive, 'w') as tar:
            for name, kind, contents in members:
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                if kind == 'file':
                    member.size = len(contents)
                    tar.addfile(member, io.BytesIO(contents))
                else:
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    member.linkname = contents
                    tar.addfile(member)
        return archive

    def test_absolute_image_symlinks_stay_in_guest(self):
        root = self.directory / 'rootfs'
        archive = self.archive([('bin', 'symlink', '/usr/bin'), ('bin/example', 'file', b'guest')])
        unpack(archive, root)
        self.assertEqual((root / 'usr/bin/example').read_bytes(), b'guest')
        self.assertEqual(guest_path(root, '/bin/example'), root / 'usr/bin/example')

    def test_relative_symlink_cannot_escape_root(self):
        root = self.directory / 'rootfs'
        archive = self.archive([('link', 'symlink', '../../outside'), ('link/file', 'file', b'safe')])
        unpack(archive, root)
        self.assertEqual((root / 'outside/file').read_bytes(), b'safe')
        self.assertFalse((self.directory / 'outside').exists())

    def test_archive_traversal_rejected(self):
        for name in ['../escape', '/absolute', 'a/../../escape']:
            with self.subTest(name=name):
                archive = self.archive([(name, 'file', b'bad')])
                root = self.directory / name.replace('/', '_')
                with self.assertRaises(BuildError):
                    unpack(archive, root)

    def test_forward_hardlink_and_replacement(self):
        root = self.directory / 'rootfs'
        archive = self.archive([('link', 'hardlink', 'original'), ('original', 'file', b'old')])
        unpack(archive, root)
        (self.source / 'new').write_bytes(b'new')
        copy(self.source, root, '/', 'new /original', {}, Ignore(self.source))
        self.assertEqual((root / 'original').read_bytes(), b'new')
        self.assertEqual((root / 'link').read_bytes(), b'old')

    def test_copy_ignore_and_negation(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        (self.source / '.dockerignore').write_text('secret\n**/*.key\n!public.key\n')
        (self.source / 'Dockerfile').write_text('FROM alpine\n')
        (self.source / 'secret').write_text('secret')
        (self.source / 'private.key').write_text('secret')
        (self.source / 'public.key').write_text('public')
        copy(self.source, root, '/app', '. .', {}, Ignore(self.source))
        self.assertEqual(sorted(p.name for p in (root / 'app').iterdir()), ['public.key'])

    def test_copy_context_symlink_rejected(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        (self.source / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(BuildError):
            copy(self.source, root, '/', 'escape /file', {}, Ignore(self.source))

    def test_multistage_and_flags_rejected_before_execution(self):
        for contents in ['FROM alpine\nRUN true\nFROM alpine\n', 'FROM alpine\nCOPY --from=build /app /app\n', 'FROM alpine\nADD --chown=1000 . /app\n']:
            (self.source / 'Dockerfile').write_text(contents)
            with self.assertRaises(BuildError):
                parse(self.source / 'Dockerfile')

    def test_add_local_files_json_and_ignore(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        (self.source / 'hello world').write_text('hello')
        (self.source / '.dockerignore').write_text('secret\n')
        (self.source / 'secret').write_text('hidden')
        copy(self.source, root, '/app', '["hello world", "data/"]', {}, Ignore(self.source), op='ADD')
        self.assertEqual((root / 'app/data/hello world').read_text(), 'hello')
        copy(self.source, root, '/', '. /all/', {}, Ignore(self.source), op='ADD')
        self.assertFalse((root / 'all/secret').exists())
        with self.assertRaises(BuildError):
            copy(self.source, root, '/', 'secret /secret', {}, Ignore(self.source), op='ADD')
        (self.source / 'Dockerfile').write_text('FROM example\nADD . /app\n')
        self.assertEqual(parse(self.source / 'Dockerfile')[1][1], 'ADD')

    def test_add_extracts_local_archives_by_content(self):
        for compression in ['', ':gz', ':bz2', ':xz']:
            with self.subTest(compression=compression):
                archive = self.source / 'archive.data'
                with tarfile.open(archive, 'w' + compression) as tar:
                    member = tarfile.TarInfo('nested/file')
                    member.size = 5
                    member.mode = 0o755
                    tar.addfile(member, io.BytesIO(b'hello'))
                root = self.directory / ('root' + compression.replace(':', '_'))
                root.mkdir()
                (root / 'app').mkdir()
                (root / 'app/keep').write_text('keep')
                copy(self.source, root, '/', 'archive.data /app', {}, Ignore(self.source), op='ADD')
                self.assertEqual((root / 'app/nested/file').read_bytes(), b'hello')
                self.assertEqual((root / 'app/nested/file').stat().st_mode & 0o777, 0o755)
                self.assertEqual((root / 'app/keep').read_text(), 'keep')
                copy(self.source, root, '/', '. /context', {}, Ignore(self.source), op='ADD')
                self.assertEqual((root / 'context/archive.data').read_bytes(), archive.read_bytes())

    def test_add_rejects_unsafe_archives_and_context_paths(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        for member in [('../escape', 'file', b'bad'), ('/escape', 'file', b'bad'),
                       ('link', 'symlink', '/etc/passwd'), ('link', 'hardlink', 'file')]:
            archive = self.archive([member])
            (self.source / 'archive').write_bytes(archive.read_bytes())
            with self.subTest(member=member), self.assertRaises(BuildError):
                copy(self.source, root, '/', 'archive /app', {}, Ignore(self.source), op='ADD')
        (self.source / 'link').symlink_to('/etc/passwd')
        for value in ['link /file', '../image.tar /file', '*.tar /file', 'git://host/repo /app']:
            with self.subTest(value=value), self.assertRaises(BuildError):
                copy(self.source, root, '/', value, {}, Ignore(self.source), op='ADD')

    def test_add_downloads_without_extracting_remote_archive(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        contents = self.archive([('file', 'file', b'hello')]).read_bytes()
        for destination, expected in [('/downloads/', 'downloads/archive.tar'), ('/renamed', 'renamed')]:
            response = io.BytesIO(contents)
            response.geturl = lambda: 'https://example.com/archive.tar'
            with patch('dockerfile.urllib.request.urlopen', return_value=response) as download:
                copy(self.source, root, '/', 'https://example.com/archive.tar ' + destination,
                     {}, Ignore(self.source), op='ADD')
            download.assert_called_once_with('https://example.com/archive.tar', timeout=60)
            self.assertEqual((root / expected).read_bytes(), contents)
            self.assertEqual((root / expected).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(BuildError):
            copy(self.source, root, '/', 'https://example.com/ /downloads/', {}, Ignore(self.source), op='ADD')

    def test_add_multiple_sources_and_variable_destination(self):
        root = self.directory / 'rootfs'
        root.mkdir()
        for name in ['one', 'two']:
            (self.source / name).write_text(name)
        copy(self.source, root, '/app', 'one two $DEST', {'DEST': 'data/'}, Ignore(self.source), op='ADD')
        self.assertEqual(sorted(p.name for p in (root / 'app/data').iterdir()), ['one', 'two'])
        with self.assertRaises(BuildError):
            copy(self.source, root, '/', 'one two /file', {}, Ignore(self.source), op='ADD')

    def test_variable_quoting_and_exec_form(self):
        self.assertEqual(words('A="$X hello" B=\'$X\' C=\\$X', {'X': 'world'}), ['A=world hello', 'B=$X', 'C=$X'])
        self.assertEqual(command('["echo", "$PORT"]'), ['echo', '$PORT'])
        self.assertEqual(command('exec app "$PORT"'), ['/bin/sh', '-c', 'exec app "$PORT"'])

    def test_arg_override_and_global_redeclaration(self):
        self.assertEqual(argument('VERSION=old', {}, {'VERSION': 'new'}, {}), ('VERSION', 'new'))
        self.assertEqual(argument('VERSION', {}, {}, {'VERSION': 'global'}), ('VERSION', 'global'))

    def test_native_run_rejects_shell_and_preserves_exec_arguments(self):
        for text in ['apt-get update && apt-get install gcc', 'go build > output', 'go build $(pwd)', 'cargo build; echo done']:
            with self.assertRaises(BuildError):
                native_command(text, {})
        self.assertEqual(native_command('go build -o /app/server .', {}), ['go', 'build', '-o', '/app/server', '.'])
        self.assertEqual(native_command('["javac", "$LITERAL"]', {}), ['javac', '$LITERAL'])

    def test_native_runtime_rejects_shell_and_user(self):
        for text in ['FROM alpine\nCMD echo hello\n', 'FROM alpine\nUSER 1000\n']:
            (self.source / 'Dockerfile').write_text(text)
            with self.assertRaises(BuildError):
                parse(self.source / 'Dockerfile')

    def test_native_symlinks_survive_artifact_relocation(self):
        root = self.directory / 'rootfs'
        archive = self.archive([('usr/bin/tool', 'file', b'guest'), ('bin', 'symlink', '/usr/bin'), ('escape', 'symlink', '../../usr/bin/tool')])
        unpack(archive, root)
        relocate_links(root)
        moved = self.directory / 'relocated'
        root.rename(moved)
        self.assertEqual((moved / 'bin/tool').read_bytes(), b'guest')
        self.assertEqual((moved / 'escape').read_bytes(), b'guest')

    def test_procfile_runs_inside_guest(self):
        (self.source / 'Procfile').write_text('worker: java -cp /app/out Main\n')
        value = procfile(self.source).partition(': ')[2]
        self.assertEqual(json.loads(value), "exec /app/.docker-runtime/launch --shell 'java -cp /app/out Main'")


if __name__ == '__main__':
    unittest.main()
