#!/usr/bin/ruby
require_relative 'native'

begin
  directory = File.expand_path(__dir__)
  build = ARGV[0] == '--build'
  if build
    ARGV.shift
    directory = File.expand_path(ARGV.shift || raise('missing build runtime directory'))
  end
  config = JSON.parse(File.read(File.join(directory, 'config.json')))
  override = if ARGV.empty?
               nil
             elsif ARGV.length == 2 && ARGV[0] == '--shell'
               ARGV[1]
             elsif ARGV[0] == '--' && ARGV.length > 1
               ARGV.drop(1)
             else
               raise 'Usage: launch [--shell COMMAND | -- EXECUTABLE ARG...]'
             end
  env, command, cwd = Native.prepare(File.join(directory, 'rootfs'), config, build: build, override: override)
  %w[HOME TMPDIR GOCACHE].each { |key| FileUtils.mkdir_p(env.fetch(key)) }
  if build
    exec(env, *command, chdir: cwd, unsetenv_others: true)
  end
  # A native program with default handlers ignores TERM as container PID 1.
  # Keep a supervisor and forward signals to the application's process group.
  child = Process.spawn(env, *command, chdir: cwd, unsetenv_others: true, pgroup: true)
  %w[TERM INT QUIT HUP USR1 USR2].each do |signal|
    Signal.trap(signal) do
      begin
        Process.kill(signal, -child)
      rescue Errno::ESRCH
        # Child already exited.
      end
    end
  end
  _, status = Process.wait2(child)
  exit(status.exitstatus || 128 + status.termsig)
rescue StandardError => error
  warn "Dockerfile native runner failed: #{error.message}"
  exit 1
end
