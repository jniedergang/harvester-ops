// Minimal Node `process` stub for yjs in the browser.
// yjs only ever touches process.argv, process.env, process.release, process.stdout.
const proc = {
  argv: [],
  env: { NODE_ENV: 'production' },
  release: { name: 'browser' },
  stdout: { write: () => {} },
  stderr: { write: () => {} },
  platform: 'browser',
  versions: {},
  nextTick: (fn, ...args) => queueMicrotask(() => fn(...args)),
};
export default proc;
