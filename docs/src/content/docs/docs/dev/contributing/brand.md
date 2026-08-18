---
title: Brand and visual identity
description: The canonical aizk mark, palette, typography, and asset workflow.
---

The aizk identity starts with a memory block. Broad interlocking folds suggest a brain without
turning the mark into an anatomical illustration. One coral node marks a focused source or memory.

The identity has two levels. The three-dimensional master gives large editorial surfaces depth.
The simplified vector keeps the same silhouette and folds legible in navigation and browser icons.

Use the lowercase `aizk` wordmark in navigation, titles, and visual branding. Uppercase `AIZK` is
still acceptable in prose when it improves readability.

## Canonical assets

The editable sources live in `docs/src/assets/`.

| Asset                 | Purpose                                                       |
| --------------------- | ------------------------------------------------------------- |
| `brain-box-master.png` | Source render for large editorial surfaces                   |
| `icon.svg`            | Square product mark, favicon, and compact navigation identity |
| `logo.svg`            | Horizontal mark and wordmark                                  |
| `banner.svg`          | Repository and documentation banner                           |
| `social-card.svg`     | Social previews                                                |
| `thumbnail.svg`       | Hackathon thumbnail in the required 3 to 2 ratio               |

The generator writes the optimized homepage render to `docs/public/brain-box.webp`.

Never edit a generated PNG or a copied favicon directly. Run the generator from the monorepo root.

```sh
pnpm --dir docs brand
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

Manrope is the product face. It keeps navigation, controls, and longer reading clear while giving
the interface a precise geometric rhythm. Instrument Serif is reserved for the largest editorial
statements. Its warmer shape gives the landing page a human counterpoint without entering the
working interface. IBM Plex Mono is used for commands, evidence labels, and measurements.

The logo is a fixed generated drawing rather than live text. Never recreate the wordmark with a
locally installed font. This keeps every exported asset identical across build systems.

## Usage

- Keep the memory block square and preserve its proportions.
- Give the icon at least one eighth of its width as clear space.
- Use the full icon at 24 pixels or larger. Use the generated favicon at smaller sizes.
- Keep the supplied colors on light or dark neutral surfaces.
- Use the supplied three-dimensional render only on large editorial surfaces.
- Do not add new shadows, textures, patterns, or decorative layers to either mark.
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
