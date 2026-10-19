require 'minitest/autorun'
require 'tmpdir'
require_relative '../lib/native'

class NativeTest < Minitest::Test
  def setup
    @root = Dir.mktmpdir
    FileUtils.mkdir_p(File.join(@root, 'app'))
    @config = {'env' => {}, 'workdir' => '/app', 'argv' => ['/app/server']}
    elf('/app/server')
  end

  def teardown
    FileUtils.remove_entry(@root)
  end

  def elf(path)
    file = File.join(@root, path)
    FileUtils.mkdir_p(File.dirname(file))
    File.binwrite(file, "\x7fELF")
    File.chmod(0755, file)
  end

  def test_relocation_and_port_override
    old = ENV['PORT']
    ENV['PORT'] = '9090'
    @config['env'] = {'PORT' => '8080', 'JAVA_HOME' => '/opt/jdk', 'LD_PRELOAD' => '/bad.so'}
    @config['argv'] += ['-cp', '/app/out:/app/lib', '--config=/app/config']
    env, cmd, cwd = Native.prepare(@root, @config)
    assert_equal '9090', env['PORT']
    assert_equal File.join(@root, 'app'), cwd
    assert_equal [File.join(@root, 'app/server'), '/app/server'], cmd[0]
    assert_equal "#{@root}/app/out:#{@root}/app/lib", cmd[2]
    assert_equal "--config=#{@root}/app/config", cmd[3]
    refute env.key?('LD_PRELOAD')
    assert_equal "#{@root}/opt/jdk", env['JAVA_HOME']
  ensure
    ENV['PORT'] = old
  end

  def test_cargo_proxy_keeps_argv_zero
    elf('/usr/local/cargo/bin/rustup')
    File.symlink('rustup', File.join(@root, 'usr/local/cargo/bin/cargo'))
    @config['env'] = {'CARGO_HOME' => '/usr/local/cargo', 'RUSTUP_HOME' => '/usr/local/rustup'}
    @config['argv'] = ['cargo', 'build', '--release']
    _, cmd, = Native.prepare(@root, @config, build: true)
    assert_equal ["#{@root}/usr/local/cargo/bin/rustup", 'cargo'], cmd[0]
  end

  def test_unsupported_build_commands_and_cgo
    [['apt-get', 'update'], ['/bin/sh', '-c', 'true'], ['go', 'test']].each do |argv|
      @config['argv'] = argv
      assert_raises(RuntimeError) { Native.prepare(@root, @config, build: true) }
    end
    @config['argv'] = ['go', 'build']
    @config['env']['CGO_ENABLED'] = '1'
    assert_raises(RuntimeError) { Native.prepare(@root, @config, build: true) }
  end

  def test_runtime_scripts_and_shell_syntax_rejected
    File.write(File.join(@root, 'app/server'), "#!/bin/sh\necho hello")
    assert_raises(RuntimeError) { Native.prepare(@root, @config) }
    ['java Main && echo done', 'java $FLAGS Main', 'java *.java'].each do |text|
      assert_raises(RuntimeError) { Native.simple(text, {}) }
    end
  end

  def test_native_shell_entrypoint_rejected
    elf('/bin/sh')
    @config['argv'] = ['/bin/sh', '-c', 'echo hello']
    assert_raises(RuntimeError) { Native.prepare(@root, @config) }
  end

  def test_guest_symlinks_cannot_resolve_outside_image
    File.symlink('/app/server', File.join(@root, 'absolute'))
    File.symlink('../../app/server', File.join(@root, 'relative'))
    assert_equal "#{@root}/app/server", Native.path(@root, '/absolute')
    assert_equal "#{@root}/app/server", Native.path(@root, '/relative')
  end
end
