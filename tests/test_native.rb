require 'minitest/autorun'
require 'tmpdir'
require 'open3'
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

  def test_go_install_and_other_subcommands
    elf('/usr/local/go/bin/go')
    @config['env'] = {'GOPATH' => '/go', 'GOBIN' => '/custom/bin'}
    [%w[go install github.com/cespare/reflex@latest], %w[go install -buildvcs=false], %w[go test ./...]].each do |argv|
      @config['argv'] = argv
      env, cmd, = Native.prepare(@root, @config, build: true)
      assert_equal ["#{@root}/usr/local/go/bin/go", 'go'], cmd.first
      assert_equal argv.drop(1), cmd.drop(1)
      assert_equal "#{@root}/go", env['GOPATH']
      assert_equal "#{@root}/custom/bin", env['GOBIN']
      assert_equal '0', env['CGO_ENABLED']
    end
    @config['env']['CGO_ENABLED'] = '1'
    env, = Native.prepare(@root, @config, build: true)
    assert_equal '1', env['CGO_ENABLED']
  end

  def test_arbitrary_image_commands_and_relative_scripts
    script = File.join(@root, 'app/custom-tool')
    File.write(script, "#!/bin/sh\nprintf '%s' \"$1\"\n")
    File.chmod(0755, script)
    @config['env']['PATH'] = '/app:/usr/bin:/bin'
    ['custom-tool', './custom-tool', '/app/custom-tool'].each do |executable|
      @config['argv'] = [executable, 'hello world']
      env, cmd, cwd = Native.prepare(@root, @config, build: true)
      output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
      assert status.success?
      assert_equal 'hello world', output
    end
    @config['argv'] = ['buildpack-command-that-does-not-exist']
    error = assert_raises(RuntimeError) { Native.prepare(@root, @config, build: true) }
    assert_match 'missing executable', error.message
  end

  def test_build_shell_expressions_expansion_and_exit_status
    FileUtils.mkdir_p(File.join(@root, 'bin'))
    FileUtils.cp('/bin/sh', File.join(@root, 'bin/sh'))
    @config['env']['MESSAGE'] = 'hello world'
    @config['argv'] = ['/bin/sh', '-c', 'printf "%s" "$MESSAGE" | cat > result; test "$(cat result)" = "$MESSAGE" && exit 7']
    env, cmd, cwd = Native.prepare(@root, @config, build: true)
    _, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
    assert_equal 7, status.exitstatus
    assert_equal 'hello world', File.read(File.join(@root, 'app/result'))
    @config['argv'] = ['/bin/sh', '-c', '/bin/echo literal']
    _, cmd, = Native.prepare(@root, @config, build: true)
    assert_equal '/bin/echo literal', cmd.last
  end

  def test_runtime_procfile_resolves_image_path_and_runtime_environment
    FileUtils.mkdir_p(File.join(@root, 'go/bin'))
    script = File.join(@root, 'go/bin/sample-go-gin')
    File.write(script, "#!/bin/sh\nprintf '%s' \"$PORT\"\n")
    File.chmod(0755, script)
    @config['env'] = {'PATH' => '/go/bin:/usr/bin:/bin', 'PORT' => '8080'}
    old_port = ENV['PORT']
    old_path = ENV['PATH']
    ENV['PORT'] = '9090'
    ENV['PATH'] = '/not/the/image/path'
    env, cmd, cwd = Native.prepare(@root, @config, override: 'sample-go-gin')
    assert_equal [script, 'sample-go-gin'], cmd.first
    output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
    assert status.success?
    assert_equal '9090', output
    @config['argv'] = ['sample-go-gin']
    _, cmd, = Native.prepare(@root, @config)
    assert_equal script, cmd.first.first
  ensure
    ENV['PORT'] = old_port
    ENV['PATH'] = old_path
  end

  def test_runtime_shell_override_and_relative_script
    FileUtils.mkdir_p(File.join(@root, 'bin'))
    FileUtils.cp('/bin/sh', File.join(@root, 'bin/sh'))
    File.write(File.join(@root, 'app/server'), "#!/bin/sh\nprintf 'started'\n")
    @config['argv'] = ['./server']
    env, cmd, cwd = Native.prepare(@root, @config)
    output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
    assert status.success?
    assert_equal 'started', output
    env, cmd, cwd = Native.prepare(@root, @config, override: './server | cat; exit 7')
    output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
    assert_equal 7, status.exitstatus
    assert_equal 'started', output
    assert_equal ['/bin/sh', '-c', 'exec app'], Native.simple('exec app', {})
  end

  def test_guest_symlinks_cannot_resolve_outside_image
    File.symlink('/app/server', File.join(@root, 'absolute'))
    File.symlink('../../app/server', File.join(@root, 'relative'))
    assert_equal "#{@root}/app/server", Native.path(@root, '/absolute')
    assert_equal "#{@root}/app/server", Native.path(@root, '/relative')
  end
end
