// Verifies the ES-module graph of game_ui/static evaluates cleanly (circular imports,
// top-level statements, boot()). Uses a permissive DOM shim; a TDZ/circular error
// throws immediately here, exactly like in the browser.
const path = require('path');
const { pathToFileURL } = require('url');

function makeEl() {
  const target = {
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    // permissive style: property sets (`el.style.x = …`) AND method calls
    // (`el.style.setProperty(…)`) — both appear in the modules
    style: new Proxy({}, {
      get(t, k) { return k in t ? t[k] : () => {}; },
      set(t, k, v) { t[k] = v; return true; },
    }),
    dataset: {},
    children: [],
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
    closest() { return null; },
    contains() { return false; },
    remove() {},
    textContent: '',
    innerHTML: '',
  };
  return new Proxy(target, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => '';
      if (k in t) return t[k];
      if (typeof k === 'symbol') return undefined;
      t[k] = function () { return makeEl(); };
      return t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}

globalThis.window = { addEventListener() {}, matchMedia: null };
globalThis.document = {
  body: makeEl(),
  querySelector: () => makeEl(),
  querySelectorAll: () => [],
  getElementById: () => makeEl(),
  createElement: () => makeEl(),
  addEventListener() {},
  removeEventListener() {},
};
globalThis.location = { protocol: 'http:', host: '127.0.0.1:8002', href: 'http://127.0.0.1:8002/' };
globalThis.WebSocket = class { static OPEN = 1; };
globalThis.navigator = { clipboard: { writeText: async () => {} } };

(async () => {
  const entry = pathToFileURL(path.join(__dirname, '..', 'game_ui', 'static', 'app.mjs'));
  try {
    await import(entry);
    console.log('MODULE GRAPH OK — app.mjs evaluated, boot() ran');
  } catch (e) {
    console.error('MODULE GRAPH FAILED:', e && e.stack || e);
    process.exit(1);
  }
  setTimeout(() => process.exit(0), 300);
})();
