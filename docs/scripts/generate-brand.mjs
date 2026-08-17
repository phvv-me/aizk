import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import sharp from 'sharp';

import { vectors } from './brand-spec.mjs';

const docs = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const project = resolve(docs, '..');
const assets = join(docs, 'src', 'assets');
const checking = process.argv.includes('--check');
const manifestPath = join(assets, 'brand-manifest.json');
const iconCrop = { left: 152, top: 152, width: 950, height: 950 };

const vectorSources = Object.entries(vectors).map(([name, content]) => [
  join(assets, name),
  Buffer.from(content),
]);

const directCopies = [
  ['icon.svg', join(assets, 'favicon.svg')],
  ['icon.svg', join(docs, 'public', 'favicon.svg')],
  ['icon.svg', join(project, 'src', 'web', 'static', 'favicon.svg')],
  ['brand-3d/aizk-memory-cube.glb', join(docs, 'public', 'aizk-memory-cube.glb')],
];

const rasterExports = [
  ['brain-box-master.png', join(docs, 'public', 'brain-box.webp'), 1024, 1024],
  ['brain-box-master.png', join(docs, 'public', 'brain-box-icon.webp'), 192, 192, iconCrop],
  ['icon.svg', join(assets, 'icon-512.png'), 512, 512],
  ['icon.svg', join(docs, 'public', 'apple-touch-icon.png'), 180, 180],
  ['icon.svg', join(project, 'src', 'web', 'static', 'apple-touch-icon.png'), 180, 180],
  ['logo.svg', join(assets, 'logo.png'), 1200, 400],
  ['banner.svg', join(assets, 'banner.png'), 2560, 640],
  ['social-card.svg', join(assets, 'social-card.png'), 1200, 630],
  ['social-card.svg', join(docs, 'public', 'social-card.png'), 1200, 630],
  ['social-card.svg', join(project, 'src', 'web', 'static', 'social-card.png'), 1200, 630],
  ['thumbnail.svg', join(project, 'hackathon', 'thumbnail.png'), 1800, 1200],
  ['thumbnail.svg', join(project, 'hackathon', 'media', 'thumbnail-devpost.png'), 1800, 1200],
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

function digest(content) {
  return createHash('sha256').update(content).digest('hex');
}

function targetName(target) {
  return relative(project, target);
}

for (const [target, expected] of vectorSources) {
  await synchronize(target, expected);
}

for (const [source, target] of directCopies) {
  await synchronize(target, await readFile(join(assets, source)));
}

if (checking) {
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const expectedSources = new Set(rasterExports.map(([source]) => source));
  const expectedTargets = new Set(rasterExports.map(([, target]) => targetName(target)));

  if (
    manifest.version !== 1 ||
    Object.keys(manifest.sources).length !== expectedSources.size ||
    Object.keys(manifest.targets).length !== expectedTargets.size
  ) {
    throw new Error(`${manifestPath} does not describe the complete raster brand set`);
  }
  for (const source of expectedSources) {
    const content = await readFile(join(assets, source));
    if (manifest.sources[source] !== digest(content)) {
      throw new Error(`${source} changed without regenerating its raster assets`);
    }
  }
  for (const [source, target, width, height] of rasterExports) {
    const name = targetName(target);
    const current = await readFile(target);
    const metadata = await sharp(current).metadata();
    const recorded = manifest.targets[name];
    if (
      !expectedTargets.has(name) ||
      recorded?.source !== source ||
      recorded?.sha256 !== digest(current) ||
      metadata.width !== width ||
      metadata.height !== height
    ) {
      throw new Error(`${target} does not match its committed brand manifest`);
    }
  }
} else {
  const manifest = { version: 1, sources: {}, targets: {} };
  for (const [source, target, width, height, crop] of rasterExports) {
    const sourceContent = await readFile(join(assets, source));
    let pipeline = sharp(sourceContent, { density: 192 });
    if (crop) pipeline = pipeline.extract(crop);
    pipeline = pipeline.resize(width, height, { fit: 'fill' });
    const rendered = await (extname(target) === '.webp'
      ? pipeline.webp({ quality: 92, effort: 6 })
      : pipeline.png({ compressionLevel: 9, adaptiveFiltering: true })
    ).toBuffer();
    await writeFile(target, rendered);
    manifest.sources[source] = digest(sourceContent);
    manifest.targets[targetName(target)] = {
      source,
      sha256: digest(rendered),
      width,
      height,
    };
  }
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
}

process.stdout.write(checking ? 'brand assets are synchronized\n' : 'brand assets generated\n');
