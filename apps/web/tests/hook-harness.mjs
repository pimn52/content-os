// Execute component effects and event handlers without DOM, a browser, or network.
// This intentionally does not provide layout/media playback acceptance evidence.
import { transformSync } from 'esbuild';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { webcrypto } from 'node:crypto';
export function componentHarness(file, name, props, environment = {}) {
  let cursor = 0, dirty = true, tree, currentProps = props;
  const cells = [], pending = [];
  const equal = (left, right) => left && right && left.length === right.length && left.every((value, i) => Object.is(value, right[i]));
  const hooks = {
    useState(initial) {
      const index = cursor++;
      if (!cells[index]) cells[index] = { value: typeof initial === 'function' ? initial() : initial };
      return [cells[index].value, value => { const next = typeof value === 'function' ? value(cells[index].value) : value;
        if (!Object.is(next, cells[index].value)) { cells[index].value = next; dirty = true; } }];
    },
    useRef(initial) { const index = cursor++; if (!cells[index]) cells[index] = { current: initial }; return cells[index]; },
    useMemo(factory, deps) { const index = cursor++; if (!cells[index] || !equal(cells[index].deps, deps)) cells[index] = { deps, value: factory() }; return cells[index].value; },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!cells[index] || !equal(cells[index].deps, deps)) { const old = cells[index]; cells[index] = { deps };
        pending.push(() => { old?.cleanup?.(); cells[index].cleanup = effect(); }); }
    },
  };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  const code = transformSync(readFileSync(file, 'utf8'), { loader: 'tsx', format: 'cjs', jsx: 'automatic' }).code;
  const productionCode = transformSync(readFileSync(join(dirname(file), 'production.ts'), 'utf8'), { loader: 'ts', format: 'cjs' }).code;
  const production = { exports: {} };
  runInNewContext(productionCode, { module: production, exports: production.exports, crypto: webcrypto, TextEncoder, Uint8Array, ...environment });
  const componentModule = { exports: {} };
  runInNewContext(code, { module: componentModule, exports: componentModule.exports,
    require: id => id === 'react' ? hooks : id === 'react/jsx-runtime' ? { jsx, jsxs: jsx } : id === './production' ? production.exports : (() => { throw new Error(id); })(),
    crypto: webcrypto, TextEncoder, Uint8Array, Headers, Blob, Response, Error,
    FormData: class { constructor(form) { this.fields = form.fields; } get(name) { return this.fields[name] ?? null; } },
    URL: { createObjectURL: () => 'blob:isolated-fixture', revokeObjectURL() {} },
    window: { sessionStorage: { getItem: () => null }, confirm: () => true },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => { throw new Error('no_network'); }, ...environment,
  });
  const Component = componentModule.exports[name];
  const walk = node => node == null || typeof node === 'boolean' ? [] : Array.isArray(node) ? node.flatMap(walk)
    : typeof node === 'object' ? [node, ...walk(node.props?.children)] : [];
  const text = node => node == null || typeof node === 'boolean' ? '' : Array.isArray(node) ? node.map(text).join('')
    : typeof node === 'object' ? text(node.props?.children) : String(node);
  const harness = {
    setProps(next) { currentProps = { ...currentProps, ...next }; dirty = true; },
    async flush() {
      for (let i = 0; i < 40; i++) {
        if (dirty) { dirty = false; cursor = 0; tree = Component(currentProps); }
        while (pending.length) pending.shift()();
        await new Promise(setImmediate);
      }
      if (dirty) { dirty = false; cursor = 0; tree = Component(currentProps); while (pending.length) pending.shift()(); }
    },
    nodes(type) { return walk(tree).filter(node => node.type === type); },
    text() { return text(tree); },
    find(type, phrase) { const node = harness.nodes(type).find(node => text(node).includes(phrase)); if (!node) throw new Error(`missing ${type}: ${phrase}`); return node; },
    input(label) { const node = harness.find('label', label); return walk(node.props.children).find(node => node.type === 'input' || node.type === 'select'); },
    async change(label, value) { harness.input(label).props.onChange({ target: { value } }); await harness.flush(); },
    async click(phrase) { const button = harness.find('button', phrase); if (button.props.disabled) throw new Error(`disabled: ${phrase}`); button.props.onClick(); await harness.flush(); },
    async submit(phrase, fields = {}) { harness.find('form', phrase).props.onSubmit({ preventDefault() {}, currentTarget: { fields } }); await harness.flush(); },
    unmount() { for (const cell of cells) cell?.cleanup?.(); },
  };
  return harness;
}
