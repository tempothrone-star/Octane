require 'json'

stacks = []
truncated = false
bytes = 0
seen = {}
tracer = TracePoint.new(:call) do |event|
  next unless event.path && File.expand_path(event.path).start_with?('/workspace/')

  if stacks.length >= 256
    truncated = true
    tracer.disable
    next
  end
  frames = caller_locations(1, 25)
  truncated ||= frames.length > 24
  stack = frames.first(24).reverse.map do |location|
    { name: location.base_label[0, 160], file: (location.absolute_path || location.path)[0, 400], line: location.lineno }
  end
  encoded = JSON.generate(stack)
  next if seen.key?(encoded)
  if bytes + encoded.bytesize > 480_000
    truncated = true
    tracer.disable
    next
  end
  seen[encoded] = true
  bytes += encoded.bytesize
  stacks << stack
end

at_exit do
  tracer.disable
  File.write('/octane-trace-output', JSON.generate({ stacks: stacks, truncated: truncated }))
rescue SystemCallError => e
  warn "Octane Ruby trace output: #{e.message}"
end
tracer.enable
