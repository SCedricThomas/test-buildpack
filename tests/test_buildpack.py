import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

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
        for contents in ['FROM alpine\nRUN true\nFROM alpine\n', 'FROM alpine\nCOPY --from=build /app /app\n', 'FROM alpine\nADD . /app\n']:
            (self.source / 'Dockerfile').write_text(contents)
            with self.assertRaises(BuildError):
                parse(self.source / 'Dockerfile')

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
