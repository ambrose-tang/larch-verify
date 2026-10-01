// Larch call adapter for JavaScript and TypeScript: runs in the PROJECT's Node.js.
//
// Started as `node [type-stripping flags] adapter.mjs LOG`. Uses only Node built-ins.
// Same JSON-lines protocol as larch/py/adapter.py (values: see larch/wire.py):
//   {"op": "ping"}                      -> {"ok": true, "node": "22.3.0", ...}
//   {"op": "load", "path", "function", "binding", "member"?, "arg_kinds", "source"?}
//                                       -> {"ok": true} | {"ok": false, "error", "error_type", "missing"?}
//   {"op": "call", "args", "timeout"}   -> {"status": "ok", "value"} | {"status": "exception", "exc"}
//                                          | {"status": "timeout"}
// The user's console output goes to the log file; the protocol uses fd 1 directly.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { inspect } from 'node:util';
import { createRequire, register } from 'node:module';
import { pathToFileURL } from 'node:url';

const MAX = 2n ** 53n - 1n;
const logPath = process.argv[2];
const logFd = logPath ? fs.openSync(logPath, 'a') : null;
const require = createRequire(import.meta.url);

// --- keep the protocol channel clean -------------------------------------------------------
const toLog = (chunk) => {
  if (logFd !== null) {
    try { fs.writeSync(logFd, typeof chunk === 'string' ? chunk : Buffer.from(chunk)); } catch { /* ignore */ }
  }
  return true;
};
const send = (obj) => fs.writeSync(1, JSON.stringify(obj) + '\n');
process.stdout.write = (chunk, enc, cb) => { toLog(chunk); if (typeof cb === 'function') cb(); return true; };
process.stderr.write = (chunk, enc, cb) => { toLog(chunk); if (typeof cb === 'function') cb(); return true; };
process.on('uncaughtException', (e) => toLog(`uncaught: ${e && e.stack ? e.stack : e}\n`));
process.on('unhandledRejection', (e) => toLog(`unhandled rejection: ${e && e.stack ? e.stack : e}\n`));

register(new URL('./hooks.mjs', import.meta.url));

// --- values -------------------------------------------------------------------------------------
function toWire(v, depth = 0) {
  if (v === null || v === undefined) return null;
  switch (typeof v) {
    case 'boolean':
    case 'string':
      return v;
    case 'number':
      if (Number.isInteger(v)) return Number.isSafeInteger(v) ? (Object.is(v, -0) ? 0 : v) : { $int: BigInt(v).toString() };
      return { $float: String(v) };
    case 'bigint':
      return v >= -MAX && v <= MAX ? Number(v) : { $int: v.toString() };
    default:
      break;
  }
  if (depth < 200 && Array.isArray(v)) return Array.from(v, (x) => toWire(x, depth + 1));
  if (depth < 200 && v instanceof Map) return { $dict: [...v].map(([k, x]) => [toWire(k, depth + 1), toWire(x, depth + 1)]) };
  let text;
  try { text = inspect(v, { depth: 3, breakLength: Infinity }); } catch { text = String(v); }
  const type = (v && v.constructor && v.constructor.name) || typeof v;
  return { $repr: text.length > 400 ? text.slice(0, 397) + '...' : text, $type: type };
}

function fromWire(j, kind) {
  if (j === null) return kind.undef ? undefined : null;
  if (Array.isArray(j)) return j.map((x) => fromWire(x, kind));
  if (typeof j === 'number') return kind.bigint && Number.isInteger(j) ? BigInt(j) : j;
  if (typeof j === 'object') {
    if ('$tuple' in j) return j.$tuple.map((x) => fromWire(x, kind));
    if ('$int' in j) return kind.bigint ? BigInt(j.$int) : Number(j.$int);
    if ('$float' in j) return Number(j.$float);
    if ('$dict' in j) return new Map(j.$dict.map(([a, b]) => [fromWire(a, kind), fromWire(b, kind)]));
  }
  return j;
}

function describeError(e) {
  if (e && typeof e === 'object' && 'message' in e) return `${e.name || 'Error'}: ${e.message}`;
  return `Thrown: ${typeof e === 'string' ? e : inspect(e)}`;
}

// --- loading -------------------------------------------------------------------------------------
let fn = null;
let kinds = [];
let methodKinds = {};
let counter = 0;

async function load(req) {
  fn = null;
  const filename = path.resolve(req.path);
  delete require.cache[filename]; // CommonJS modules are cached by file name
  counter += 1;
  const url = pathToFileURL(filename);
  url.searchParams.set('larch', String(counter));
  url.searchParams.set('export', req.binding);
  let tmp = null;
  if (req.source != null) {
    tmp = path.join(process.cwd(), `larch-src-${process.pid}-${counter}${path.extname(filename)}`);
    fs.writeFileSync(tmp, req.source);
    url.searchParams.set('src', tmp);
  }
  try {
    const mod = await import(url.href);
    let target = mod.__larch_target ?? (mod.default && mod.default.__larch_target);
    if (target === undefined) throw new Error(`${req.binding} is not a top-level binding of ${path.basename(filename)}`);
    if (req.member) target = target[req.member];
    if (typeof target !== 'function') throw new TypeError(`${req.function} is not a function`);
    fn = target;
    kinds = req.arg_kinds || [];
    methodKinds = req.method_kinds || {};
    return { ok: true };
  } catch (e) {
    const msg = describeError(e);
    const out = { ok: false, error: msg.slice(0, 2000), error_type: (e && e.code) || (e && e.name) || 'Error' };
    const m = /Cannot find (?:package|module) '([^']+)'/.exec(msg);
    if (m) {
      out.missing = m[1];
      out.error_type = 'ModuleNotFound';
    }
    if (e && e.stack) out.traceback = String(e.stack).slice(-4000);
    return out;
  } finally {
    if (tmp) { try { fs.unlinkSync(tmp); } catch { /* ignore */ } }
  }
}

// --- calling -------------------------------------------------------------------------------------
const callScript = new vm.Script('globalThis.__larch_call()');

function run(thunk, timeout) {
  globalThis.__larch_call = thunk;
  let value;
  try {
    // vm's watchdog interrupts synchronous infinite loops anywhere in the call.
    value = callScript.runInThisContext({ timeout: Math.max(1, Math.round(1000 * (timeout || 1))) });
  } catch (e) {
    if (e && e.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT') return [{ status: 'timeout' }, undefined];
    if (e instanceof RangeError && /call stack/i.test(e.message)) return [{ status: 'exception', exc: `RangeError: ${e.message}` }, undefined];
    return [{ status: 'exception', exc: describeError(e).slice(0, 300) }, undefined];
  } finally {
    globalThis.__larch_call = undefined;
  }
  return [{ status: 'ok', value: toWire(value) }, value];
}

function decodeArgs(req, argKinds) {
  return (req.args || []).map((a, i) => fromWire(a, argKinds[i] || {}));
}

function call(req) {
  if (!fn) return { status: 'exception', exc: 'LarchError: no function loaded' };
  let args;
  try {
    args = decodeArgs(req, kinds);
  } catch (e) {
    return { status: 'exception', exc: `LarchError: ${describeError(e)}` };
  }
  const f = fn;
  return run(() => f(...args), req.timeout)[0];
}

// --- objects (stateful components) -----------------------------------------------------------------
let obj = null;

function construct(req) {
  obj = null;
  if (!fn) return { status: 'exception', exc: 'LarchError: no class loaded' };
  const Cls = fn;
  const args = decodeArgs(req, kinds);
  const [res, value] = run(() => new Cls(...args), req.timeout || 2);
  if (res.status !== 'ok') return res;
  obj = value;
  return { status: 'ok' };
}

function invoke(req) {
  if (obj == null) return { status: 'exception', exc: 'LarchError: no instance' };
  const target = obj;
  let args;
  try {
    args = decodeArgs(req, (methodKinds && methodKinds[req.method]) || []);
  } catch (e) {
    return { status: 'exception', exc: `LarchError: ${describeError(e)}` };
  }
  if (typeof target[req.method] !== 'function') return { status: 'exception', exc: `TypeError: ${req.method} is not a method` };
  return run(() => target[req.method](...args), req.timeout)[0];
}

function observe(req) {
  if (obj == null) return { status: 'exception', exc: 'LarchError: no instance' };
  const target = obj;
  const out = {};
  for (const o of req.observers || []) {
    const thunk = o.access === 'call' ? () => target[o.name]() : () => target[o.name];
    out[o.name] = run(thunk, req.timeout)[0];
  }
  return { status: 'ok', observers: out };
}

function ping() {
  return { ok: true, node: process.versions.node, executable: process.execPath, typescript: Boolean(process.features && process.features.typescript) };
}

// --- main loop -----------------------------------------------------------------------------------
let buf = '';
let chain = Promise.resolve();
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i).trim();
    buf = buf.slice(i + 1);
    if (!line) continue;
    chain = chain.then(async () => {
      let resp;
      try {
        const req = JSON.parse(line);
        if (req.op === 'ping') resp = ping();
        else if (req.op === 'load') resp = await load(req);
        else if (req.op === 'call') resp = call(req);
        else if (req.op === 'new') resp = construct(req);
        else if (req.op === 'invoke') resp = invoke(req);
        else if (req.op === 'observe') resp = observe(req);
        else resp = { ok: false, error: `unknown op ${req.op}` };
      } catch (e) {
        resp = { ok: false, error: `adapter error: ${describeError(e)}` };
      }
      try { send(resp); } catch (e) { send({ status: 'exception', exc: `LarchError: unserializable result: ${describeError(e)}` }); }
    });
  }
});
process.stdin.on('end', () => process.exit(0));
