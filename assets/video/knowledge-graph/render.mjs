/* Frame capture harness for the PlugICT knowledge-graph film.
 *
 * Usage:
 *   node render.mjs out_dir              # render all frames
 *   FRAMES=10,120,300 node render.mjs d  # render only selected frames (preview)
 *   START=0 END=389 node render.mjs d    # render a frame range
 *
 * Frames are written as out_dir/frame_%04d.png (1920x1080).
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = process.argv[2] || path.join(here, 'frames');
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
});
page.on('pageerror', e => { console.error('PAGE ERROR:', e.message); process.exitCode = 1; });

await page.goto('file://' + path.join(here, 'index.html'));
const meta = await page.evaluate('init()');
console.log('scene:', JSON.stringify(meta));

const total = await page.evaluate('window.FRAMES');
let frames;
if (process.env.FRAMES) {
  frames = process.env.FRAMES.split(',').map(Number);
} else {
  const start = parseInt(process.env.START ?? '0', 10);
  const end = parseInt(process.env.END ?? String(total - 1), 10);
  frames = [];
  for (let i = start; i <= end; i++) frames.push(i);
}

const t0 = Date.now();
for (let k = 0; k < frames.length; k++) {
  const i = frames[k];
  const dataUrl = await page.evaluate(`captureFrame(${i})`);
  const buf = Buffer.from(dataUrl.slice('data:image/png;base64,'.length), 'base64');
  fs.writeFileSync(path.join(outDir, `frame_${String(i).padStart(4, '0')}.png`), buf);
  if (k % 30 === 0 || k === frames.length - 1) {
    const dt = (Date.now() - t0) / 1000;
    console.log(`frame ${i} (${k + 1}/${frames.length}) ${dt.toFixed(1)}s elapsed`);
  }
}
await browser.close();
console.log('done:', frames.length, 'frames ->', outDir);
