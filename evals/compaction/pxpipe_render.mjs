// Render text to pxpipe dense pages, as the study's image arm needs it.
// Usage (tsx): tsx pxpipe_render.mjs <input.txt> <outdir> [cols]
// The pxpipe checkout is a third-party clone, not vendored here; point PXPPIPE_DIR at it
// (default ~/Projects/spikes/pxpipe).
// Prints one JSON line: {pages:[{file,width,height,chars}], charsRendered, dropped}
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [, , input, outdir, colsArg] = process.argv;
if (!input || !outdir) {
  console.error('usage: tsx pxpipe_render.mjs <input.txt> <outdir> [cols]');
  process.exit(2);
}
const pxpipeDir = process.env.PXPPIPE_DIR || join(homedir(), 'Projects', 'spikes', 'pxpipe');
const renderMod = await import(pathToFileURL(join(pxpipeDir, 'src', 'core', 'render.ts')).href);
const text = readFileSync(input, 'utf8');
mkdirSync(outdir, { recursive: true });
const pages = await renderMod.renderDensePages(text, {
  cols: colsArg ? Number(colsArg) : undefined,
  reflow: true,
});
const out = pages.map((p, i) => {
  const file = join(outdir, `page-${i}.png`);
  writeFileSync(file, p.png);
  return { file, width: p.width, height: p.height, chars: p.charsRendered };
});
// Which codepoints the atlas has no glyph for: they render as BLANK cells, so
// they are content the reader never sees. Reported, not silently swallowed.
const dropped = new Map();
for (const p of pages) {
  for (const [cp, n] of p.droppedCodepoints ?? []) dropped.set(cp, (dropped.get(cp) ?? 0) + n);
}
const topDropped = [...dropped.entries()]
  .sort((a, b) => b[1] - a[1])
  .slice(0, 12)
  .map(([cp, n]) => ({ cp: `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`, char: String.fromCodePoint(cp), n }));
console.log(
  JSON.stringify({
    pages: out,
    charsRendered: out.reduce((a, p) => a + p.chars, 0),
    dropped: pages.reduce((a, p) => a + p.droppedChars, 0),
    topDropped,
  }),
);
