---
title: Brand and visual identity
description: The canonical aizk mark, palette, typography, and asset workflow.
---

The aizk identity comes from one idea. A single connected path forms a `Z` for Zettelkasten. Its
four nodes represent sources that become useful memory without losing their links.

Use the lowercase `aizk` wordmark in navigation, titles, and visual branding. Uppercase `AIZK` is
still acceptable in prose when it improves readability.

## Canonical assets

The editable sources live in `docs/src/assets/`.

| Asset             | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `icon.svg`        | Square product mark, favicon, and compact navigation identity |
| `logo.svg`        | Horizontal mark and wordmark                                  |
| `banner.svg`      | Repository and documentation banner                           |
| `social-card.svg` | Social previews                                                |
| `thumbnail.svg`   | Hackathon thumbnail in the required 3 to 2 ratio               |

Never edit a generated PNG or a copied favicon directly. Run the generator from the monorepo root.

```sh
chefe run aizk-brand
```

Documentation checks fail when a generated asset has drifted from its vector source.

## Color

| Role   | Value     | Use                                        |
| ------ | --------- | ------------------------------------------ |
| Ink    | `#17223b` | Text, outlines, and deep surfaces          |
| Cobalt | `#315dff` | The product mark, actions, and links       |
| Coral  | `#ff6b4a` | The starting node and focused details      |
| Paper  | `#fffaf1` | Quiet surfaces and the path through memory |
| Mist   | `#dfe8ff` | Supporting highlights                      |

Cobalt identifies the product and interactive actions. Coral marks a starting point or one focused
detail. Documentation diagrams stay neutral unless color carries meaning.

## Type

Space Grotesk in weights 500 through 700 is the display face for the wordmark, headings, and major
navigation labels. Body copy uses the local system sans serif stack. Code, evidence labels, and
measurements use the local monospace stack.

## Usage

- Keep the mark square and preserve its proportions.
- Give the icon at least one eighth of its width as clear space.
- Use the full icon at 24 pixels or larger. Use the generated favicon at smaller sizes.
- Keep the supplied colors on light or dark neutral surfaces.
- Do not add cards, shadows, patterns, or decorative layers to the mark.
- Do not stretch, rotate, or recolor individual parts of the mark.
- Give decorative copies empty alternative text. Use `aizk` when the mark is the only identifying
  content in a link or image.

The repository banner, documentation, marketing site, web app, browser icons, social cards, and
hackathon thumbnail all derive from the same specification in `docs/scripts/brand-spec.mjs`.

The generator records the source and output hashes plus dimensions in `brand-manifest.json`.
Checks validate that committed manifest instead of rerasterizing SVG text with the runner's local
fonts. Regeneration remains explicit, so a fresh checkout verifies the same release assets on every
platform.

```text
brand specification
      |
      v
brand generator
      |
      +---- canonical SVGs
      +---- repository banner
      +---- docs and marketing
      +---- web app icons
      +---- social preview
      +---- hackathon thumbnail
```
