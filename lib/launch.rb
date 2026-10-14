#!/usr/bin/ruby
# This file is copied into /app/.docker-runtime; Python is not needed at runtime.
require 'json'

begin
  directory = File.expand_path(__dir__)
  config = JSON.parse(File.read(File.join(directory, 'config.json')))
  proot = File.join(directory, 'proot')
  root = File.join(directory, 'rootfs')
  unless system({ 'PATH' => '/usr/bin:/bin' }, proot, '-r', '/', '-w', '/', '/bin/true', unsetenv_others: true)
    abort 'Dockerfile runtime requires ptrace for child processes; this container blocks PRoot. No capability escalation is attempted.'
  end

  if ARGV.empty?
    command = config.fetch('argv')
  elsif ARGV.length == 2 && ARGV[0] == '--shell'
    command = ['/bin/sh', '-c', ARGV[1]]
  elsif ARGV[0] == '--' && ARGV.length > 1
    command = ARGV.drop(1)
  else
    abort 'Usage: launch [--shell COMMAND | -- EXECUTABLE ARG...]'
  end

  # The image supplies executable search paths and HOME. Scalingo supplies PORT,
  # service credentials, and other application settings, overriding image ENV.
  environment = config.fetch('env').merge(ENV.to_h.reject { |key, _| ['PATH', 'HOME', 'DOCKER_AUTH_CONFIG', 'DOCKER_BUILD_ARGS'].include?(key) })
  environment.reject! { |key, _| key.start_with?('PROOT_') }
  environment['PROOT_TMP_DIR'] = '/tmp'
  options = [proot, '--kill-on-exit', '-r', root, '-i', config.fetch('identity'), '-w', config.fetch('workdir'),
             '-b', '/dev', '-b', '/proc', '-b', '/sys',
             '-b', '/etc/hosts:/etc/hosts!', '-b', '/etc/resolv.conf:/etc/resolv.conf!']
  child = Process.spawn(environment, *options, *command, unsetenv_others: true, pgroup: true)
  ['TERM', 'INT', 'QUIT', 'HUP', 'USR1', 'USR2'].each do |signal|
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
  warn "Dockerfile runtime failed: #{error.message}"
  exit 1
end
