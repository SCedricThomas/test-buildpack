# Shared native build/runtime adapter. No namespaces or tracing.
require 'json'
require 'shellwords'
require 'fileutils'

module Native
  PATH_KEYS = %w[HOME JAVA_HOME GOROOT GOPATH GOBIN GOCACHE GOMODCACHE CARGO_HOME RUSTUP_HOME CARGO_TARGET_DIR TMPDIR].freeze
  PACKAGE_KEYS = %w[LIBRARY_PATH INCLUDE_PATH CPATH CPPPATH PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR GIT_EXEC_PATH APK_ROOT].freeze

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
    return ['/bin/sh', '-c', value] if value.match?(/[;&|<>`\n*?\[\](){}$#~]/)
    args = Shellwords.split(value)
    builtins = %w[. : break cd continue eval exec exit export read readonly return set shift test times trap type ulimit umask unset wait alias unalias command getopts hash jobs fg bg fc if for while until case !]
    if args.first && (builtins.include?(args.first) || args.first.match?(/\A[A-Za-z_][A-Za-z0-9_]*=/))
      return ['/bin/sh', '-c', value]
    end
    args
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
    # Build and runtime share image command resolution, with stack fallbacks.
    bins = %w[GOROOT JAVA_HOME CARGO_HOME].filter_map { |key| File.join(env[key], 'bin') if env[key] }
    image_path = values.fetch('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    bins += image_path.split(':').map do |entry|
      raise 'PATH entries must be absolute image paths' unless entry.start_with?('/')
      path(root, entry)
    end
    packages = File.join(File.dirname(root), 'packages')
    apt = File.join(packages, 'apt')
    if File.directory?(apt)
      bins = %w[usr/bin usr/sbin bin sbin].map { |part| File.join(apt, part) } + bins
      libraries = %w[lib/x86_64-linux-gnu usr/lib/x86_64-linux-gnu lib usr/lib].map { |part| File.join(apt, part) }.join(':')
      includes = %w[usr/include usr/include/x86_64-linux-gnu].map { |part| File.join(apt, part) }.join(':')
      env['LD_LIBRARY_PATH'] = libraries
      env['LIBRARY_PATH'] = libraries
      %w[INCLUDE_PATH CPATH CPPPATH].each { |key| env[key] = includes }
      env['PKG_CONFIG_PATH'] = %w[usr/lib/x86_64-linux-gnu/pkgconfig usr/lib/pkgconfig usr/share/pkgconfig].map { |part| File.join(apt, part) }.join(':')
      env['PKG_CONFIG_SYSROOT_DIR'] = apt
      git_core = File.join(apt, 'usr/lib/git-core')
      env['GIT_EXEC_PATH'] = git_core if File.directory?(git_core)
    end
    env['APK_ROOT'] = File.join(packages, 'apk') if File.directory?(File.join(packages, 'apk'))
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
      protected = PATH_KEYS + PACKAGE_KEYS + %w[PATH DOCKER_AUTH_CONFIG DOCKER_BUILD_ARGS]
      ENV.each { |key, val| env[key] = val unless protected.include?(key) || key.start_with?('PROOT_', 'LD_') }
    end
    cmd = argv(override || config.fetch('argv'), env)
    raise 'empty command' unless cmd.is_a?(Array) && !cmd.empty? && cmd.all? { |x| x.is_a?(String) && !x.include?("\0") }
    executable = cmd.first
    if build
      # Preserve the Go adapter default for Go invoked directly or by a script.
      env['CGO_ENABLED'] ||= '0' if env['GOROOT']
      wrappers = File.join(File.dirname(root), 'build-bin')
      env['PATH'] = wrappers + ':' + env['PATH'] if File.directory?(wrappers)
    end
    package_manager = build && %w[apt apt-get apk /usr/bin/apt /usr/bin/apt-get /sbin/apk /usr/sbin/apk].include?(executable)
    resolved = if package_manager && File.directory?(wrappers)
                 File.join(wrappers, File.basename(executable))
               elsif executable.start_with?('/')
                 path(root, executable)
               elsif executable.include?('/')
                 path(root, File.join(config.fetch('workdir'), executable))
               else
                 env.fetch('PATH').split(':').map { |directory| File.join(directory, executable) }
                    .find { |candidate| File.file?(candidate) && File.executable?(candidate) }
               end
    raise "missing executable: #{executable}" unless resolved && File.file?(resolved) && File.executable?(resolved)
    resolved = File.realpath(resolved)
    # A shell program is source text, not a filesystem argument. In particular,
    # do not relocate an entire script merely because it begins with '/'.
    shell = %w[sh bash dash ash zsh ksh].include?(File.basename(executable))
    command = [[resolved, executable]] + cmd.drop(1).map { |value| shell ? value : argument(root, value) }
    [env, command, path(root, config.fetch('workdir'))]
  end
end
