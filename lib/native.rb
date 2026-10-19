# Shared build/runtime adapter. No namespaces, tracing, or guest shell.
require 'json'
require 'shellwords'
require 'fileutils'

module Native
  PATH_KEYS = %w[HOME JAVA_HOME GOROOT GOPATH GOBIN GOCACHE GOMODCACHE CARGO_HOME RUSTUP_HOME CARGO_TARGET_DIR TMPDIR].freeze

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

  def self.environment(root, values, build: false)
    env = values.reject { |key, _| key.start_with?('PROOT_', 'LD_') }
    defaults = {'HOME' => '/root', 'TMPDIR' => '/tmp', 'GOCACHE' => '/tmp/go-cache', 'GOPATH' => '/go'}
    defaults.each { |key, value| env[key] ||= value }
    env['GOROOT'] ||= '/usr/local/go' if File.directory?(path(root, '/usr/local/go'))
    PATH_KEYS.each do |key|
      next unless env[key]
      raise "#{key} must be an absolute image path" unless env[key].start_with?('/')
      env[key] = path(root, env[key])
    end
    # Build commands can use any image executable, with stack tools as fallback.
    # Runtime keeps its existing toolchain-only PATH.
    bins = %w[GOROOT JAVA_HOME CARGO_HOME].filter_map { |key| File.join(env[key], 'bin') if env[key] }
    if build
      image_path = values.fetch('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
      bins += image_path.split(':').map do |entry|
        raise 'build PATH entries must be absolute image paths' unless entry.start_with?('/')
        path(root, entry)
      end
    end
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
    env = environment(root, values, build: build)
    unless build
      protected = PATH_KEYS + %w[PATH DOCKER_AUTH_CONFIG DOCKER_BUILD_ARGS]
      ENV.each { |key, val| env[key] = val unless protected.include?(key) || key.start_with?('PROOT_', 'LD_') }
    end
    cmd = argv(override || config.fetch('argv'), env)
    raise 'empty command' unless cmd.is_a?(Array) && !cmd.empty? && cmd.all? { |x| x.is_a?(String) && !x.include?("\0") }
    executable = cmd.first
    if !build && %w[sh bash dash ash zsh ksh env].include?(File.basename(executable))
      raise 'shell/interpreter entrypoints are unsupported; override with ENTRYPOINT [] and a native CMD'
    end
    if build
      # Preserve the Go adapter default for Go invoked directly or by a script.
      env['CGO_ENABLED'] ||= '0' if env['GOROOT']
    elsif executable != 'java' && !executable.start_with?('/')
      raise 'runtime requires java or an absolute image path to a native executable; shell entrypoints are unsupported'
    end
    resolved = if executable.start_with?('/')
                 path(root, executable)
               elsif build && executable.include?('/')
                 path(root, File.join(config.fetch('workdir'), executable))
               elsif build
                 env.fetch('PATH').split(':').map { |directory| File.join(directory, executable) }
                    .find { |candidate| File.file?(candidate) && File.executable?(candidate) }
               else
                 key = {'go' => 'GOROOT', 'cargo' => 'CARGO_HOME', 'java' => 'JAVA_HOME', 'javac' => 'JAVA_HOME'}.fetch(executable)
                 raise "image must define #{key} or contain the supported toolchain layout" unless env[key]
                 path(root, File.join(values[key] || '/usr/local/go', 'bin', executable))
               end
    raise "missing executable: #{executable}" unless resolved && File.file?(resolved) && File.executable?(resolved)
    resolved = File.realpath(resolved) if build
    raise 'runtime script executables are unsupported; use a native binary or java' unless build || File.binread(resolved, 4) == "\x7fELF".b
    # A shell program is source text, not a filesystem argument. In particular,
    # do not relocate an entire script merely because it begins with '/'.
    shell = build && %w[sh bash dash ash zsh ksh].include?(File.basename(executable))
    command = [[resolved, executable]] + cmd.drop(1).map { |value| shell ? value : argument(root, value) }
    [env, command, path(root, config.fetch('workdir'))]
  end
end
