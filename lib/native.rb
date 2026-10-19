# Shared build/runtime adapter. No namespaces, tracing, or guest shell.
require 'json'
require 'shellwords'
require 'fileutils'

module Native
  PATH_KEYS = %w[HOME JAVA_HOME GOROOT GOPATH GOCACHE GOMODCACHE CARGO_HOME RUSTUP_HOME CARGO_TARGET_DIR TMPDIR].freeze

  def self.path(root, value)
    # Resolve image symlinks within the image, including absolute targets.
    pending = value.split('/')
    parts = []
    links = 0
    until pending.empty?
      part = pending.shift
      next if part.empty? || part == '.'
      if part == '..'
        parts.pop
        next
      end
      candidate = File.join(root, *parts, part)
      if File.symlink?(candidate)
        links += 1
        raise 'too many image symlinks' if links > 40
        target = File.readlink(candidate)
        parts = [] if target.start_with?('/')
        pending = target.split('/') + pending
      else
        parts << part
      end
    end
    File.join(root, *parts)
  end

  def self.simple(value, env)
    raise 'shell operators, substitutions and globs are unsupported; use one command or JSON form' if value.match?(/[;&|<>`\n*?\[\](){}]/)
    raise 'variable expansion in Procfile commands is unsupported; read environment variables in the application' if value.include?('$')
    Shellwords.split(value)
  end

  def self.argv(value, env)
    value.is_a?(String) ? simple(value, env) : value
  end

  def self.environment(root, values)
    env = values.reject { |key, _| key.start_with?('PROOT_', 'LD_') }
    defaults = {'HOME' => '/root', 'TMPDIR' => '/tmp', 'GOCACHE' => '/tmp/go-cache', 'GOPATH' => '/go'}
    defaults.each { |key, value| env[key] ||= value }
    env['GOROOT'] ||= '/usr/local/go' if File.directory?(path(root, '/usr/local/go'))
    PATH_KEYS.each do |key|
      next unless env[key]
      raise "#{key} must be an absolute image path" unless env[key].start_with?('/')
      env[key] = path(root, env[key])
    end
    # Use only relocatable toolchains from the image; linker and OS utilities
    # come from the Scalingo stack, not the image's /usr/bin.
    bins = %w[GOROOT JAVA_HOME CARGO_HOME].filter_map { |key| File.join(env[key], 'bin') if env[key] }
    env['PATH'] = (bins + ['/usr/bin', '/bin']).join(':')
    env
  end

  def self.argument(root, value)
    if value.start_with?('/')
      value.split(':', -1).map { |part| part.start_with?('/') ? path(root, part) : part }.join(':')
    elsif value.match?(/\A-[^=]+=\//)
      key, val = value.split('=', 2)
      key + '=' + argument(root, val)
    else
      value
    end
  end

  def self.prepare(root, config, build: false, override: nil)
    values = config.fetch('env')
    env = environment(root, values)
    unless build
      protected = PATH_KEYS + %w[PATH DOCKER_AUTH_CONFIG DOCKER_BUILD_ARGS]
      ENV.each { |key, val| env[key] = val unless protected.include?(key) || key.start_with?('PROOT_', 'LD_') }
    end
    cmd = argv(override || config.fetch('argv'), env)
    raise 'empty command' unless cmd.is_a?(Array) && !cmd.empty? && cmd.all? { |x| x.is_a?(String) && !x.include?("\0") }
    executable = cmd.first
    if %w[sh bash dash ash zsh ksh env].include?(File.basename(executable))
      raise 'shell/interpreter entrypoints are unsupported; override with ENTRYPOINT [] and a native CMD'
    end
    if build
      allowed = (executable == 'go' && (cmd[1] == 'build' || cmd[1, 2] == %w[mod download])) ||
                (executable == 'cargo' && %w[build fetch].include?(cmd[1])) || executable == 'javac'
      raise "unsupported RUN command #{executable.inspect}; supported: go build/mod download, cargo build/fetch, javac" unless allowed
      raise 'Go adapter requires CGO_ENABLED=0' if executable == 'go' && values.fetch('CGO_ENABLED', '0') != '0'
      env['CGO_ENABLED'] = '0' if executable == 'go'
    elsif executable != 'java' && !executable.start_with?('/')
      raise 'runtime requires java or an absolute image path to a native executable; shell entrypoints are unsupported'
    end
    resolved = if executable.start_with?('/')
                 path(root, executable)
               else
                 key = {'go' => 'GOROOT', 'cargo' => 'CARGO_HOME', 'java' => 'JAVA_HOME', 'javac' => 'JAVA_HOME'}.fetch(executable)
                 raise "image must define #{key} or contain the supported toolchain layout" unless env[key]
                 path(root, File.join(values[key] || '/usr/local/go', 'bin', executable))
               end
    raise "missing image executable: #{executable}" unless File.file?(resolved) && File.executable?(resolved)
    raise 'script executables are unsupported; use a native binary or java' unless File.binread(resolved, 4) == "\x7fELF".b
    command = [[resolved, executable]] + cmd.drop(1).map { |value| argument(root, value) }
    [env, command, path(root, config.fetch('workdir'))]
  end
end
