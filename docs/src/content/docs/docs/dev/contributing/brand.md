---
title: Brand and visual identity
description: The canonical aizk mark, palette, typography, and asset workflow.
---

The aizk identity comes from one idea. A slip-box holds source cards while a small graph on the
front shows the knowledge connected through them. The gold spark signals a useful finding, not a
generic AI effect.

Use the lowercase `aizk` wordmark in navigation, titles, and visual branding. Uppercase `AIZK` is
still acceptable in prose when it improves readability.

## Canonical assets

The editable sources live in `docs/src/assets/`.

| Asset             | Purpose                                                       |
| ----------------- | ------------------------------------------------------------- |
| `icon.svg`        | Square product mark, favicon, and compact navigation identity |
| `logo.svg`        | Horizontal mark and wordmark                                  |
| `banner.svg`      | Repository and documentation banner                           |
| `social-card.svg` | Social previews and hackathon thumbnail                       |

Never edit a generated PNG or a copied favicon directly. Run the generator from the monorepo root.

```sh
chefe run aizk-brand
```

Documentation checks fail when a generated asset has drifted from its vector source.

## Color

| Role           | Value     | Use                                  |
| -------------- | --------- | ------------------------------------ |
| Ink            | `#1e1b4b` | Dark text and deep surfaces          |
| Primary indigo | `#4f46e5` | Actions, links, and the product mark |
| Bright indigo  | `#6366f1` | Highlights and gradients             |
| Paper          | `#eef2ff` | Light detail and quiet surfaces      |
| Gold           | `#fbbf24` | The small finding spark only         |

Indigo identifies the product and interactive actions. Gold is intentionally scarce. Documentation
diagrams stay neutral unless color carries actual information.

## Type

Space Grotesk in weights 500 through 700 is the display face for the wordmark, headings, and major
navigation labels. Body copy uses the local system sans serif stack. Code, evidence labels, and
measurements use the local monospace stack.

## Usage

- Keep the mark square and preserve its proportions.
- Give the icon at least one eighth of its width as clear space.
- Use the full icon at 24 pixels or larger. Use the generated favicon at smaller sizes.
- Keep the supplied colors on light or dark neutral surfaces.
- Do not replace the mark with a generic sparkle, stretch it, rotate it, or recolor individual parts.
- Give decorative copies empty alternative text. Use `aizk` when the mark is the only identifying
  content in a link or image.

The repository banner, documentation, marketing site, web app, browser icons, social cards, and
hackathon thumbnail all derive from this system.

```text
canonical SVGs
      |
      v
brand generator
      |
      +---- repository banner
      +---- docs and marketing
      +---- web app icons
      +---- social preview
      +---- hackathon thumbnail
```
