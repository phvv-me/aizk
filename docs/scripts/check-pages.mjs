// Documentation gate. Five rules, checked on every build.
//
//   1. No page takes more than ten minutes to read.
//   2. Every page carries at least one diagram.
//   3. Every internal link resolves to a page that was actually built.
//   4. No em dash, colon or semicolon in prose, which is the house punctuation rule.
//   5. Public material carries no known private identities, hosts, source titles or identifiers.
//
// File extensions are also semantic. Plain Markdown uses `.md`, while component imports and JSX
// use `.mdx`.
//
// Words come from the Markdown source with the frontmatter, code and diagrams removed, since a
// reader spends different time on those. Links are checked against `dist/`, which is the only
// authority on what the site really serves.

import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';

const docs = resolve(import.meta.dirname, '..');
const project = resolve(docs, '..');
const content = join(docs, 'src/content/docs');
const dist = join(docs, 'dist');

const WORD_BUDGET = 980;
const WORDS_PER_MINUTE = 200;

async function walk(dir, match) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await walk(path, match)));
    else if (match(entry.name)) found.push(path);
  }
  return found;
}

function prose(markdown) {
  return markdown
    .replace(/^---\n[\s\S]*?\n---\n/, '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/^import .*$/gm, '')
    // Strip JSX component blocks, both paired (`<CardGrid>...</CardGrid>`) and self-closing
    // (`<Diagram ... />`), which span many lines. Their node and edge specs are markup, not prose,
    // so counting them would penalize a page for carrying a rich diagram.
    .replace(/<([A-Z][A-Za-z]*)\b[\s\S]*?<\/\1>/g, ' ')
    .replace(/<[A-Z][A-Za-z]*\b[\s\S]*?\/>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\|[^\n]*\|/g, ' ')
    .replace(/[#*_`>[\]()]/g, ' ');
}

function hasDiagram(markdown) {
  return (
    /```(d2|text)\b/.test(markdown) ||
    /<(Diagram|CostEstimator|svg|figure)\b/.test(markdown)
  );
}

// mermaid is retired. It renders client-side, which is exactly what broke in Safari, so every
// diagram is now a server-rendered `<Diagram>`, a D2 block, or ASCII in a ```text fence.
function usesMermaid(markdown) {
  return /```mermaid\b/.test(markdown);
}

function usesMdx(markdown) {
  const source = markdown
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`]*`/g, '');
  return /^import\s.+$/m.test(source) || /<[A-Z][A-Za-z]*(?:\s|\/>|>)/.test(source);
}

// The house rule is prose without em dashes, colons or semicolons. Colons stay legal inside code,
// tables, frontmatter, links and component imports, which are syntax rather than punctuation.
function punctuation(markdown) {
  // Strip frontmatter, code, and whole JSX component blocks first, so a `<Diagram>` node spec like
  // `{ id: 'x', label: 'y' }` never reaches the prose check no matter how it is wrapped. Its
  // `key:` colons are object-literal syntax, not prose.
  const body = markdown
    .replace(/^---\n[\s\S]*?\n---\n/, '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/<([A-Z][A-Za-z]*)\b[\s\S]*?<\/\1>/g, ' ')
    .replace(/<[A-Z][A-Za-z]*\b[\s\S]*?\/>/g, ' ');
  const offenders = [];
  for (const line of body.split('\n')) {
    const lead = line.trimStart();
    // Table rows and component imports carry colons that are syntax, not prose.
    if (lead.startsWith('|') || lead.startsWith('import ') || lead.startsWith('{') || lead.startsWith('<')) continue;
    if (line.includes('—')) offenders.push(['em dash', line]);
    if (line.includes(';')) offenders.push(['semicolon', line]);
    const bare = line
      .replace(/`[^`]*`/g, '')
      .replace(/https?:\/\/\S*/g, '')
      .replace(/\[[^\]]*\]\([^)]*\)/g, '');
    if (/\w:(\s|$)/.test(bare)) offenders.push(['colon', line]);
  }
  return offenders;
}

const failures = [];

const privacyRules = [
  ["personal identity", /\bPedro(?: Valois)?\b/i],
  ["private deployment name", /\bpgAIZK\b/i],
  ["private machine name", /\bCrimson\b/i],
  ["private project name", /\b(?:Meteng|Crivo|Archy|GE4M)\b/i],
  ["private study title", /\b(?:JLPT N2|Japanese Area)\b/i],
  ["private project title", /\b(?:Personal Brand and Career Website|My Personal Computer)\b/i],
  ["private institution", /\bC-AIR\b/i],
  ["sensitive personal record", /\b(?:passport|visa status|residence status)\b/i],
  ["local user path", /\/(?:home|Users)\/pedro\b/i],
  ["personal deployment domain", /\b(?:[a-z0-9-]+\.)*phvv\.me\b/i],
  ["personal email address", /\b[A-Z0-9._%+-]+@(?!example\.(?:com|org)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b/i],
  ["production document or operator identifier", /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i],
];

const candidateFiles = [
  join(project, 'README.md'),
  join(project, 'SECURITY.md'),
  join(project, 'ROADMAP.md'),
  join(project, 'CHANGELOG.md'),
  join(project, 'pyproject.toml'),
  join(project, 'infra/aws/README.md'),
  join(project, 'src/web/.env.example'),
  join(project, 'src/deploy/.env.example'),
  join(project, 'src/deploy/Dockerfile'),
  join(project, 'src/deploy/docker-compose.yml'),
  join(project, 'tests/eval/data/certainty_audit_cases.jsonl'),
  ...(await walk(join(project, '.agents'), (name) => /\.(?:json|md)$/.test(name))),
  ...(await walk(join(project, '.claude-plugin'), (name) => /\.(?:json|md)$/.test(name))),
  ...(await walk(join(project, 'plugins'), (name) => /\.(?:json|md)$/.test(name))),
  ...(await walk(join(docs, 'src'), (name) => /\.(?:astro|md|mdx|svelte)$/.test(name))),
  ...(await walk(join(project, 'hackathon'), (name) => /\.(?:json|md|py|sh|txt)$/.test(name))),
];
const publicFiles = [];
for (const path of candidateFiles) {
  try {
    if ((await stat(path)).isFile()) publicFiles.push(path);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
}

for (const path of publicFiles) {
  const source = await readFile(path, 'utf8');
  for (const [label, pattern] of privacyRules) {
    if (pattern.test(source)) failures.push(`${relative(project, path)} exposes a ${label}.`);
  }
}

for (const path of await walk(content, (name) => name.endsWith('.md') || name.endsWith('.mdx'))) {
  const source = await readFile(path, 'utf8');
  const where = relative(docs, path);
  const words = prose(source).split(/\s+/).filter(Boolean).length;
  const minutes = Math.ceil(words / WORDS_PER_MINUTE);

  if (words > WORD_BUDGET) {
    failures.push(`${where} runs ${words} words, about ${minutes} minutes. Split it. Budget is ${WORD_BUDGET}.`);
  }
  if (!hasDiagram(source)) {
    failures.push(`${where} carries no diagram. Add a <Diagram>, a D2 block, or ASCII art in a text fence.`);
  }
  if (usesMermaid(source)) {
    failures.push(`${where} still uses mermaid. Replace it with a <Diagram> component so it renders in Safari.`);
  }
  if (path.endsWith('.md') && usesMdx(source)) {
    failures.push(`${where} uses component syntax but has a .md extension. Rename it to .mdx.`);
  }
  if (path.endsWith('.mdx') && !usesMdx(source)) {
    failures.push(`${where} uses no MDX features. Rename it to .md.`);
  }
  for (const [kind, line] of punctuation(source)) {
    failures.push(`${where} uses a ${kind} in prose. ${line.trim().slice(0, 80)}`);
  }
}

// Every href the built site emits has to land on something the built site serves.
const pages = await walk(dist, (name) => name.endsWith('.html'));
const links = new Set();
for (const path of pages) {
  const html = await readFile(path, 'utf8');
  for (const [, href] of html.matchAll(/href="(\/[^"#?]*)/g)) links.add(`${href}\x1f${relative(dist, path)}`);

  // A D2 block that uses a reserved keyword as a container id makes astro-d2 drop the entire page
  // body without failing the build, so a page that renders almost nothing is a real failure.
  const body = (html.split('<main')[1] ?? '').replace(/<[^>]+>/g, ' ').trim();
  const clientApplication = relative(dist, path).replaceAll('\\', '/').startsWith('app/');
  if (body.length < 400 && !path.endsWith('404.html') && !clientApplication) {
    failures.push(`${relative(dist, path)} rendered an almost empty body, so a diagram probably failed silently.`);
  }
}

const homepage = await readFile(join(dist, 'index.html'), 'utf8');
const homepageContracts = [
  ['the rendered brain cube', 'src="/brain-box.webp"'],
  ['the rendered navigation mark', 'src="/brain-box-icon.webp"'],
  ['the agent setup', 'Agent setup'],
  ['the Claude Code choice', 'Claude Code'],
  ['the Codex choice', 'Codex'],
  ['the command copy controls', 'data-copy-command='],
  ['the Claude marketplace installer', 'claude plugin marketplace add phvv-me/aizk'],
  ['the Claude plugin installer', 'claude plugin install aizk@aizk'],
  ['the Codex marketplace installer', 'codex plugin marketplace add phvv-me/aizk'],
  ['the Codex plugin installer', 'codex plugin add aizk@aizk'],
  ['the Codex sign in command', 'mcp_oauth_callback_port=8912'],
  ['the install copy label', 'Copy install'],
  ['the second command copy label', 'Copy command'],
];
for (const [label, snippet] of homepageContracts) {
  if (!homepage.includes(snippet)) failures.push(`index.html omits ${label}.`);
}

const setupInstructions = await readFile(join(dist, 'setup.md'), 'utf8');
const readme = await readFile(join(project, 'README.md'), 'utf8');
const homepageCommands = homepage.replaceAll('&amp;', '&');
const canonicalSetupCommands = [
  'claude plugin marketplace add phvv-me/aizk && claude plugin install aizk@aizk',
  'claude',
  'codex plugin marketplace add phvv-me/aizk && codex plugin add aizk@aizk',
  'codex -c mcp_oauth_callback_port=8912 mcp login aizk',
];
for (const command of canonicalSetupCommands) {
  if (!homepageCommands.includes(command)) failures.push(`index.html omits the canonical ${command} command.`);
  if (!setupInstructions.includes(command)) failures.push(`setup.md omits the canonical ${command} command.`);
  if (!readme.includes(command)) failures.push(`README.md omits the canonical ${command} command.`);
}

const codexMarketplace = JSON.parse(
  await readFile(join(project, '.agents/plugins/marketplace.json'), 'utf8'),
);
const claudeMarketplace = JSON.parse(
  await readFile(join(project, '.claude-plugin/marketplace.json'), 'utf8'),
);
const codexPlugin = JSON.parse(
  await readFile(join(project, 'plugins/aizk/.codex-plugin/plugin.json'), 'utf8'),
);
const claudePlugin = JSON.parse(
  await readFile(join(project, 'plugins/aizk/.claude-plugin/plugin.json'), 'utf8'),
);
const claudeMcp = JSON.parse(
  await readFile(join(project, 'plugins/aizk/claude.mcp.json'), 'utf8'),
);
const pluginContracts = [
  ['the Codex marketplace name', codexMarketplace.name, 'aizk'],
  ['the Codex marketplace plugin', codexMarketplace.plugins?.[0]?.name, 'aizk'],
  [
    'the Codex marketplace source',
    codexMarketplace.plugins?.[0]?.source?.path,
    './plugins/aizk',
  ],
  ['the Claude marketplace name', claudeMarketplace.name, 'aizk'],
  ['the Claude marketplace plugin', claudeMarketplace.plugins?.[0]?.name, 'aizk'],
  ['the Claude marketplace source', claudeMarketplace.plugins?.[0]?.source, './plugins/aizk'],
  ['the Codex plugin name', codexPlugin.name, 'aizk'],
  ['the Claude plugin name', claudePlugin.name, 'aizk'],
  ['the shared plugin version', codexPlugin.version, claudePlugin.version],
  [
    'the shared MCP endpoint',
    codexPlugin.mcpServers?.aizk?.url,
    claudeMcp.mcpServers?.aizk?.url,
  ],
];
for (const [label, actual, expected] of pluginContracts) {
  if (actual !== expected) failures.push(`Plugin manifests disagree on ${label}.`);
}

const external = /^\/(app|mcp|api|auth|events)(\/|$)/;
for (const entry of links) {
  const [href, from] = entry.split('\x1f');
  if (external.test(href)) continue;
  const target = join(dist, href);
  const candidates = [target, join(target, 'index.html'), `${target}.html`];
  const resolved = await Promise.all(
    candidates.map((candidate) =>
      stat(candidate).then(
        (info) => info.isFile(),
        () => false,
      ),
    ),
  );
  if (!resolved.some(Boolean)) failures.push(`${from} links to ${href}, which the build does not serve.`);
}

if (failures.length) {
  console.error(`\ncheck-pages found ${failures.length} problems\n`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log(`check-pages passed over ${pages.length} built pages`);
