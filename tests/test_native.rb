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

  def test_package_prefix_relocation_and_wrapper_precedence
    Dir.mktmpdir do |directory|
      runtime = File.join(directory, 'original')
      root = File.join(runtime, 'rootfs')
      FileUtils.mkdir_p(File.join(root, 'app'))
      FileUtils.mkdir_p(File.join(runtime, 'packages/apt/usr/bin'))
      FileUtils.mkdir_p(File.join(runtime, 'packages/apk/usr/lib'))
      FileUtils.mkdir_p(File.join(runtime, 'build-bin'))
      tool = File.join(runtime, 'packages/apt/usr/bin/packaged-tool')
      File.write(tool, "#!/bin/sh\necho packaged\n")
      File.chmod(0755, tool)
      wrapper = File.join(runtime, 'build-bin/apt-get')
      File.write(wrapper, "#!/bin/sh\nexit 0\n")
      File.chmod(0755, wrapper)
      config = {'env' => {}, 'argv' => ['/usr/bin/apt-get', 'install', 'jq'], 'workdir' => '/app'}
      env, cmd, = Native.prepare(root, config, build: true)
      assert_equal wrapper, cmd.first.first
      assert_equal File.join(runtime, 'build-bin'), env['PATH'].split(':').first
      moved = File.join(directory, 'moved')
      File.rename(runtime, moved)
      config['argv'] = ['packaged-tool']
      env, cmd, cwd = Native.prepare(File.join(moved, 'rootfs'), config)
      assert_equal File.join(moved, 'packages/apt/usr/bin/packaged-tool'), cmd.first.first
      assert_includes env['LD_LIBRARY_PATH'], File.join(moved, 'packages/apt/usr/lib')
      refute_includes env['LD_LIBRARY_PATH'], '/packages/apk'
      refute_includes env['LD_LIBRARY_PATH'], runtime
      assert_equal File.join(moved, 'packages/apk'), env['APK_ROOT']
      output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
      assert status.success?
      assert_equal "packaged\n", output
    end
  end

  def test_extracted_python_modules_work_during_build_and_after_relocation
    old_pythonpath = ENV['PYTHONPATH']
    ENV['PYTHONPATH'] = '/outer/python/modules'
    Dir.mktmpdir do |directory|
      runtime = File.join(directory, 'build')
      FileUtils.mkdir_p(File.join(runtime, 'rootfs/app'))
      bin = File.join(runtime, 'packages/apt/usr/bin')
      modules = File.join(runtime, 'packages/apt/usr/lib/python3/dist-packages')
      FileUtils.mkdir_p([bin, modules])
      File.write(File.join(modules, 'extracted_example.py'), "value = 'imported'\n")
      executable = File.join(bin, 'python-tool')
      File.write(executable, "#!/usr/bin/python3\nimport extracted_example\nprint(extracted_example.value)\n")
      File.chmod(0755, executable)
      config = {'env' => {'PYTHONPATH' => 'project-modules'}, 'argv' => ['python-tool'], 'workdir' => '/app'}
      [true, false].each do |build|
        unless build
          moved = File.join(directory, 'deployed')
          File.rename(runtime, moved)
          runtime = moved
        end
        env, cmd, cwd = Native.prepare(File.join(runtime, 'rootfs'), config, build: build)
        assert_equal [File.join(runtime, 'packages/apt/usr/lib/python3/dist-packages'), 'project-modules'], env['PYTHONPATH'].split(':')
        output, status = Open3.capture2(env, *cmd, chdir: cwd, unsetenv_others: true)
        assert status.success?
        assert_equal "imported\n", output
      end
    end
  ensure
    ENV['PYTHONPATH'] = old_pythonpath
  end

  def test_guest_symlinks_cannot_resolve_outside_image
    File.symlink('/app/server', File.join(@root, 'absolute'))
    File.symlink('../../app/server', File.join(@root, 'relative'))
    assert_equal "#{@root}/app/server", Native.path(@root, '/absolute')
    assert_equal "#{@root}/app/server", Native.path(@root, '/relative')
  end
end
