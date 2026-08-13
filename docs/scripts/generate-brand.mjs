import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import sharp from 'sharp';

const docs = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const project = resolve(docs, '..');
const assets = join(docs, 'src', 'assets');
const checking = process.argv.includes('--check');

const vectorCopies = [
  ['icon.svg', join(assets, 'favicon.svg')],
  ['icon.svg', join(docs, 'public', 'favicon.svg')],
  ['icon.svg', join(project, 'src', 'web', 'static', 'favicon.svg')],
];

const rasterExports = [
  ['icon.svg', join(assets, 'icon-512.png'), 512, 512],
  ['icon.svg', join(docs, 'public', 'apple-touch-icon.png'), 180, 180],
  ['icon.svg', join(project, 'src', 'web', 'static', 'apple-touch-icon.png'), 180, 180],
  ['logo.svg', join(assets, 'logo.png'), 1120, 400],
  ['banner.svg', join(assets, 'banner.png'), 2560, 640],
  ['social-card.svg', join(assets, 'social-card.png'), 1200, 630],
  ['social-card.svg', join(docs, 'public', 'social-card.png'), 1200, 630],
  ['social-card.svg', join(project, 'src', 'web', 'static', 'social-card.png'), 1200, 630],
  ['social-card.svg', join(project, 'hackathon', 'thumbnail.png'), 1200, 630],
];

async function synchronize(target, expected) {
  if (checking) {
    const current = await readFile(target);
    if (!current.equals(expected)) {
      throw new Error(`${target} does not match its canonical brand source`);
    }
    return;
  }
  await writeFile(target, expected);
}

for (const [source, target] of vectorCopies) {
  await synchronize(target, await readFile(join(assets, source)));
}

for (const [source, target, width, height] of rasterExports) {
  const rendered = await sharp(await readFile(join(assets, source)), {
    density: 192,
  })
    .resize(width, height, { fit: 'fill' })
    .png({ compressionLevel: 9, adaptiveFiltering: true })
    .toBuffer();
  await synchronize(target, rendered);
}

process.stdout.write(checking ? 'brand assets are synchronized\n' : 'brand assets generated\n');
