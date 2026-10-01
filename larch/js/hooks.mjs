// Larch module hooks (registered by adapter.mjs in the project's Node process).
//
// A URL carrying `?larch=N` loads the module under test with two changes:
//   * `src=<file>`: the module's source is replaced (a mutant or a proposed fix),
//     while its URL, and so its relative imports, stay those of the original file;
//   * `export=<name>`: the top-level binding <name> is additionally exported as
//     `__larch_target`, so non-exported functions can be called too.
// Nothing is ever written next to the user's files.
import { readFileSync } from 'node:fs';

export async function load(url, context, nextLoad) {
  if (!url.startsWith('file:') || !url.includes('larch=')) return nextLoad(url, context);
  const u = new URL(url);
  if (!u.searchParams.has('larch')) return nextLoad(url, context);
  const base = new URL(url);
  base.search = '';
  const r = await nextLoad(base.href, context);
  const srcFile = u.searchParams.get('src');
  let source = srcFile ? readFileSync(srcFile, 'utf8') : (r.source == null ? readFileSync(base, 'utf8') : String(r.source));
  const name = u.searchParams.get('export');
  if (name) {
    const cjs = String(r.format || '').startsWith('commonjs');
    source += cjs ? `\n;module.exports.__larch_target = ${name};\n` : `\n;export { ${name} as __larch_target };\n`;
  }
  return { format: r.format, source, shortCircuit: true };
}
