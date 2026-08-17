const colors = {
  ink: '#17223b',
  cobalt: '#315dff',
  coral: '#ff6b4a',
  paper: '#fffaf1',
  mist: '#dfe8ff',
};

const font = "'Space Grotesk', 'Noto Sans', sans-serif";
const tagline = 'Shared memory for people, teams, and AI agents';

function mark(x, y, size) {
  const scale = size / 96;
  return `<g transform="translate(${x} ${y}) scale(${scale})">
    <defs>
      <clipPath id="brain-box-clip"><rect width="96" height="96" rx="24"/></clipPath>
    </defs>
    <rect width="96" height="96" rx="24" fill="${colors.cobalt}"/>
    <g clip-path="url(#brain-box-clip)" fill="none" stroke="${colors.paper}" stroke-width="11" stroke-linecap="round" stroke-linejoin="round">
      <path d="M25 -5V15C25 26 33 29 33 39C33 49 26 54 15 54H-5"/>
      <path d="M67 -5V14C67 24 74 29 84 29H101"/>
      <path d="M101 58H83C73 58 68 64 68 74V101"/>
      <path d="M-5 76H14C24 76 29 83 29 101"/>
      <path d="M47 -5V15C47 25 51 30 58 36C65 42 65 49 59 56C53 62 49 67 49 76V101"/>
    </g>
    <circle cx="59" cy="56" r="5" fill="${colors.coral}" stroke="${colors.paper}" stroke-width="2"/>
  </g>`;
}

function memoryLine(points, width = 6) {
  const path = points.map(([x, y]) => `${x},${y}`).join(' ');
  const nodes = points
    .map(([x, y], index) => {
      const fill = index === 0 ? colors.coral : index === points.length - 1 ? colors.cobalt : colors.paper;
      return `<circle cx="${x}" cy="${y}" r="11" fill="${fill}" stroke="${colors.ink}" stroke-width="${width}"/>`;
    })
    .join('\n    ');
  return `<polyline points="${path}" fill="none" stroke="${colors.ink}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round"/>
    ${nodes}`;
}

function wordmark(x, y, size, label = 'aizk', fill = colors.ink) {
  const spacing = label === 'aizk' ? size * -0.035 : size * -0.01;
  return `<text x="${x}" y="${y}" fill="${fill}" font-family="${font}" font-size="${size}" font-weight="700" letter-spacing="${spacing}">${label}</text>`;
}

export const vectors = {
  'icon.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96" role="img" aria-labelledby="title desc">
  <title id="title">aizk</title>
  <desc id="desc">A memory block with a brain-fold texture</desc>
  ${mark(0, 0, 96)}
</svg>
`,
  'logo.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" viewBox="0 0 600 200" role="img" aria-labelledby="title desc">
  <title id="title">aizk</title>
  <desc id="desc">The aizk memory-block mark and wordmark</desc>
  ${mark(28, 28, 144)}
  ${wordmark(204, 139, 128)}
</svg>
`,
  'banner.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="320" viewBox="0 0 1280 320" role="img" aria-labelledby="title desc">
  <title id="title">aizk</title>
  <desc id="desc">${tagline}</desc>
  <rect width="1280" height="320" rx="28" fill="${colors.paper}"/>
  ${mark(72, 72, 176)}
  ${wordmark(304, 178, 142)}
  <text x="312" y="234" fill="${colors.ink}" opacity=".72" font-family="${font}" font-size="31" font-weight="500">${tagline}</text>
  <rect x="1248" y="0" width="32" height="320" fill="${colors.cobalt}"/>
</svg>
`,
  'social-card.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="title desc">
  <title id="title">aizk</title>
  <desc id="desc">${tagline}</desc>
  <rect width="1200" height="630" fill="${colors.paper}"/>
  <rect x="0" y="0" width="32" height="630" fill="${colors.cobalt}"/>
  ${mark(88, 154, 220)}
  ${wordmark(366, 305, 170)}
  <text x="376" y="374" fill="${colors.ink}" opacity=".72" font-family="${font}" font-size="34" font-weight="500">${tagline}</text>
  <g opacity=".9">
    ${memoryLine([
      [378, 494],
      [586, 452],
      [760, 500],
      [944, 430],
      [1100, 482],
    ])}
  </g>
</svg>
`,
  'thumbnail.svg': `<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1200" viewBox="0 0 1800 1200" role="img" aria-labelledby="title desc">
  <title id="title">crAIZK</title>
  <desc id="desc">${tagline}</desc>
  <rect width="1800" height="1200" fill="${colors.paper}"/>
  <rect x="0" y="0" width="48" height="1200" fill="${colors.cobalt}"/>
  ${mark(136, 300, 360)}
  ${wordmark(574, 548, 236, 'crAIZK')}
  <text x="590" y="640" fill="${colors.ink}" opacity=".72" font-family="${font}" font-size="44" font-weight="500">${tagline}</text>
  <g opacity=".9">
    ${memoryLine(
      [
        [590, 850],
        [832, 782],
        [1082, 868],
        [1330, 752],
        [1604, 834],
      ],
      9,
    )}
  </g>
</svg>
`,
};
