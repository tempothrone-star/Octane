const { Session } = require('node:inspector');
const { writeFileSync } = require('node:fs');

const session = new Session();
session.connect();
session.post('Profiler.enable');
session.post('Profiler.setSamplingInterval', { interval: 100 });
session.post('Profiler.start');

process.once('exit', () => {
  session.post('Profiler.stop', (error, capture) => {
    if (error || !capture?.profile) return;
    const { nodes, samples = [] } = capture.profile;
    const byId = new Map(nodes.map(node => [node.id, node.callFrame]));
    const parents = new Map();
    for (const node of nodes) {
      for (const child of node.children ?? []) parents.set(child, node.id);
    }
    const stacks = [];
    let truncated = false;
    let bytes = 0;
    // A leaf node has a single ancestry chain in a V8 profile. Deduplicate
    // samples to retain distinct observed paths within the tool output budget.
    for (const leaf of new Set(samples)) {
      if (stacks.length >= 256) { truncated = true; break; }
      let id = leaf;
      const stack = [];
      while (id !== undefined && stack.length < 24) {
        const frame = byId.get(id);
        if (frame?.url && (frame.url.startsWith('file:///workspace/') || frame.url.startsWith('/workspace/'))) {
          stack.push({ name: (frame.functionName || '(anonymous)').slice(0, 160),
            file: frame.url.slice(0, 400), line: frame.lineNumber + 1 });
        } else {
          stack.push(null);
        }
        id = parents.get(id);
      }
      truncated ||= id !== undefined;
      if (stack.some(Boolean)) {
        stack.reverse();
        bytes += Buffer.byteLength(JSON.stringify(stack));
        if (bytes > 480_000) { truncated = true; break; }
        stacks.push(stack);
      }
    }
    try {
      writeFileSync('/octane-trace-output', JSON.stringify({ stacks, truncated }));
    } catch (error) {
      process.stderr.write(`Octane Node trace output: ${error.message}\n`);
    }
  });
});
