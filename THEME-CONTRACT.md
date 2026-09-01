# The theme contract

What a Presence theme may decide, what it may not, and what CSS the engine
actually understands.

A theme is a directory. Presence generates a stylesheet from its `theme.json`
and then appends its `theme.css` verbatim, so a theme can restyle anything the
generated sheet sets — and, because the append comes last, wins every tie.
That is deliberate: **the theme owns how a slide looks.**

What it does not own is how a slide is *classified*. `slides/layout.py` decides
from the slide's own content whether it is text, a full-bleed picture, one
picture beside words, two portraits flanking them, or a gallery — because those
decisions need measurements CSS cannot take: a word count, an image's aspect
ratio, and how many lone pictures came before this slide in the deck.

So the engine says what it measured and the stylesheet decides what that means.
The rule throughout: **a classification is a data attribute, a measurement is a
custom property, and nothing that a theme might want to change is written as an
inline style** — an inline style outranks every selector, so anything set that
way would not be the theme's to decide. Section 3 lists what is published;
overriding the rules that read it is how a theme arranges a slide differently
from the way Presence would have.

Everything below was measured against WeasyPrint 68.1, the engine that renders
every deck, thumbnail, live preview and export.

---

## 1. What a theme is

```
<name>/
├── theme.json      required — palette, fonts, callouts
├── theme.css       optional — anything else
└── fonts/          optional — .ttf / .otf / .woff / .woff2
```

Installed under `~/.local/share/presence/themes/` (or the system data dir).
Font files found in `fonts/` are turned into `@font-face` rules automatically.
The family name is the file stem with a trailing style suffix split off and
hyphens turned into spaces, so `IBM-Plex-Sans-Bold.ttf` becomes the family
`IBM Plex Sans` at weight bold. Refer to it from `body_font`, `heading_font`
or `mono_font`.

### `theme.json` keys

| Key | Default | Paints |
|---|---|---|
| `name` `slug` `author` `version` `description` | dir name / `Unknown` / `1.0` | identity |
| `bg` | `#ffffff` | slide background |
| `fg` | `#1a1a2e` | body text |
| `accent` | `#ba5d00` | `strong`, `a`, h1–h3 fallback, table header, progress bar |
| `accent2` | *(= accent)* | declared as `--p-accent2`; **used by nothing in the generated CSS** — it is live only where your own `theme.css` reads it |
| `heading_color` | *(= accent)* | h1–h3 |
| `code_bg` | `#f0f4f8` | `pre`, `code` |
| `title_bg` `title_fg` | `#1a1a2e` / `#ffffff` | cover slide |
| `title_accent` | *(= accent)* | cover h1 |
| `body_font` `heading_font` `mono_font` | IBM Plex Sans / *(= body)* / Fira Code | font stacks |
| `base_size` | `34` | body px at 720p, scaled with the slide |
| `pygments_style` | `friendly` | code highlighting |
| `callout_tip` `callout_info` `callout_warning` `callout_danger` | — | `[bg, fg, border]` triples |
| `callout_icon_*` | `✓` `i` `!` `✕` | inserted into `content:`, so no single quotes |

Colour values are validated against a CSS colour allowlist before they are
interpolated (`css.py::_safe_colour`); a value that is not a colour literal is
replaced with black and logged. Your `theme.css` is not validated this way —
only `@import` is stripped from it, to stop a stylesheet making network
requests.

### Contrast

Fields are graded against the text they actually paint, which is why there is
no single number. `accent` reaches body-size text (`strong`, `a`) and so is held
to 4.5:1; `heading_color` paints only h1–h3 and `title_accent` only the cover
h1, both large text at 3:1. Because `accent2` is live only in your own
stylesheet, it is graded only where that stylesheet sets a `color:` from it.

---

## 2. Where your CSS lands

```
generated from theme.json  →  your theme.css  →  (end of sheet)
```

Later wins on equal specificity, so a bare `.slide h1 { … }` in your file beats
the generated `h1 { … }` without `!important`.

Nothing else outranks you. The only inline styles in the document are custom
properties carrying a measurement, and each has a rule in the generated sheet
that reads it — which is the rule you override:

| Custom property | Set on | Read by | Carries |
|---|---|---|---|
| `--p-img-size` | `.slide-image`, `.slide-image-a/-b` | the panel's `width` (or `height`) | the width `auto_size()` chose from the word count, or `PAIR_SIZE` |
| `--p-img-pad` | `.slide-text` | the padding that clears the picture | the same figure, in `%` beside the text and `px` above or below it |
| `--p-img-pad-a` / `-b` | `.slide-text` | the same, for the flanking pair | |
| `--p-gallery-columns` | `.gallery` | `grid-template-columns` | e.g. `repeat(3, 1fr)` |
| `--p-gallery-row` | `.gallery` | `grid-auto-rows` | the measured row height in `px` |
| `--p-progress` | `.progress-bar-fill` | `width` | how far into the deck this slide is |

To rearrange, override the property that reads one — `.slide[data-layout="single"]
.slide-image { width: 33% }` — not the custom property, which is a measurement
and inline. **One caution:** `grid-auto-rows` must stay a definite length. An
image at `height:100%` in an indefinite row collapses, and WeasyPrint then
renders the whole grid empty.

### Custom properties

Every palette field is exposed on `:root`, and these are the intended handle
for a stylesheet that wants to stay in step with the theme's own colours:

```
--p-bg  --p-fg  --p-accent  --p-accent2  --p-heading  --p-code-bg
--p-title-bg  --p-title-fg  --p-title-accent
--p-body-font  --p-heading-font  --p-mono-font
```

---

## 3. The DOM you are styling

One slide is one `div.slide`, one PDF page, `1280×720` at 16:9 (the box scales
with the aspect ratio; `base_size` and paddings scale with it).

```
div.slide[data-slide-index]              ← every slide, always
        [data-layout][data-text][data-images][data-shapes]
        [data-step][data-steps]          ← only a slide that reveals in steps
    …the slide's blocks, each with data-src-line…
        .not-yet                         ← a block this step has not reached
    div.slide-number
    div.progress-bar-track > div.progress-bar-fill
```

### What the engine measured

Every slide div carries these, stamped at the one dispatch site so a new
layout cannot ship without them. They are the facts the arrangement was chosen
from, published so a stylesheet can reach a different conclusion.

| Attribute | Values | Means |
|---|---|---|
| `data-layout` | `title` `text` `bleed` `single` `pair` `gallery` | the arrangement `choose_layout()` picked |
| `data-text` | `none` `short` `long` | how much text shares the slide; the boundary between `short` and `long` is 40 words, the same one that decides whether a picture gets half the slide or 40% |
| `data-images` | an integer | how many pictures the slide holds |
| `data-step` / `data-steps` | integers | which reveal step this copy of the slide is, of how many; absent on a slide that shows all at once |
| `data-shapes` | space-separated `portrait` `landscape` `square` `unknown` | one per picture, in order; `unknown` where the file could not be read |

`data-shapes` is a whole-slide list, so use `~=` to ask about any one picture:
`.slide[data-shapes~="portrait"]`. A gallery cell also carries its own
`data-shape`, plus `data-fit` (`cover` or `contain`) — the crop-or-letterbox
call `cell_fit()` made — because the slide-level list cannot address a single
cell.

These are worth knowing for what they let a theme do that Presence would not:
a lone picture goes beside the words and never behind them, but
`.slide[data-layout="single"][data-text="short"] .slide-image { width: 100% }`
is a theme deciding otherwise.

The cover is `div.slide.title-slide` and carries neither a slide number nor a
progress bar; it may hold `p.title-meta` (author · date, when the frontmatter
names them).

The five layouts, as emitted:

| Layout | Slide element | Inside |
|---|---|---|
| text | `.slide` | the blocks |
| two-column | `.slide` | `.two-col > .col` ×2 |
| bleed | `.slide.has-image[data-img-pos="background"]` | `.slide-image > img`, empty `.slide-text` |
| single | `.slide.has-image[data-img-pos="left"\|"right"]` | `.slide-image > img`, `.slide-text` |
| pair | `.slide.has-two-images[data-split="h"]` | `.slide-image-a`, `.slide-image-b`, `.slide-text` |
| gallery | `.slide.has-gallery` | `.gallery-text`, `.gallery > .gallery-cell[data-span] > img` |

Also emitted on the slide div, conditionally: `data-img-fit` when the fit is not `cover`,
`data-img-focal` when the focal point is not centred, `data-span="2"` on the
gallery cell that spans two columns (the third of three images), and
`data-p-theme="light"` or `"dark"` on a slide pinned to one of those.

Blocks carry `data-src-line` — the line in the source document they came from.
It is how a layout measurement names the line that overflowed. Do not rely on
it in selectors; do not strip it.

---

## 4. What a theme may not break

Five things are read back off the laid-out page (`slides/pagination.py`). They
are the reason Presence can tell a writer that a slide overflowed, put the
right thumbnail against the right slide, and drop continuation pages from the
exported PDF. A stylesheet that breaks them does not fail loudly — it makes the
app quietly wrong.

1. **`div.slide` must survive, one per slide, with its class.** The fold marker
   is measured against the content box of the first box whose class list
   contains `slide`. Do not rename it, do not unwrap it, do not make it
   `display:contents`.

2. **`data-slide-index` must survive.** It is how each slide is matched to the
   page it starts on, which the thumbnail strip, image export, handout and
   slideshow all index by.

3. **`.not-yet` must not change what a block occupies.** A slide that reveals
   in steps is laid out *once* and written out once per step, so the engine can
   measure the fold and the fragmentation on any of them. The generated rule is
   `visibility: hidden`, which keeps the box. A theme may restyle it — `opacity`
   or `color: transparent` are both fine, and dimming what is coming rather than
   hiding it is a legitimate house style — but `display: none`, a height of 0 or
   anything else that takes the block out of flow makes the steps of one slide
   different shapes, and the text will jump as the reveal lands.

4. **Slide content should stay in normal flow.** Overflow is detected two ways —
   a block crossing the slide's text area, and a slide needing a second page —
   and absolutely positioned or transformed content trips neither. A theme that
   positions its body copy absolutely will silently lose the overflow warning
   for every slide that uses it. Position decoration freely; leave the text in
   flow.

5. **The bottom padding is the warning line.** `.slide` is `padding: 57px 102px`
   with `padding-bottom: 79px`, and the fold fires when content reaches the
   content box's bottom edge — 641px on a 720px slide, not 720px — because the
   reserved strip belongs to the slide number and the progress bar. Enlarge that
   padding and you warn earlier; remove it and text will collide with the
   furniture before anything says so.

---

## 5. The CSS the engine understands

WeasyPrint is not a browser. It is very good at layout and print typography and
has almost no interest in effects. Nothing animates: a deck is a PDF.

### Supported

**Layout** — `display: flex | grid | inline-block | table`, `float`,
`position: static | relative | absolute | fixed`, `z-index`, the whole of
`gap` / `grid-template-*` / `grid-auto-rows` / `grid-column` / `flex-*` /
`order` / `align-*` / `justify-*`.

**Box** — margins, padding, borders, `border-radius`, `outline`, `box-sizing`,
`overflow`, `min-*` / `max-*`.

**Typography** — the `font-*` family including `font-feature-settings` and
`font-variation-settings`, `line-height`, `letter-spacing`, `word-spacing`,
`text-transform`, `text-align`, `text-align-last`, `text-decoration` with
`-thickness` and `text-underline-offset`, `text-indent`, `white-space`,
`hyphens`, `font-variant`, `columns`, `column-gap`, `widows`, `orphans`,
`tab-size`.

**Paint** — colours in every modern notation (`rgb()` with slash alpha, `hsl()`,
`oklch()`, `#rgba`), `background-color`, `background-image` with
`linear-gradient`, `radial-gradient` and the `repeating-` forms,
`background-size` / `-position` / `-repeat` / `-clip`, `opacity`,
`currentColor`, `visibility: hidden` — which keeps the box, paints nothing of
it including list markers and images, and leaves the text out of the PDF
altogether, and is what a reveal step is built on.

**Transforms** — `transform` with `rotate()`, `scale()`, `translate()`,
`matrix()`, plus `transform-origin`.

**Images** — `object-fit`, `object-position`, `image-rendering`.

**Paged** — `@page`, `break-inside`, `break-after`, the `page-break-*` aliases.

**Selectors — effectively all of them.** `::before` / `::after` with `content`,
`:first-child`, `:last-child`, `:nth-child()` including `an+b`,
`:nth-of-type()`, `:not()`, `:is()`, `:where()`, **`:has()`**, attribute
selectors with `=` `^=` `*=`, and the `>` `+` `~` combinators. This is the part
of CSS a theme leans on hardest, and it is complete.

**Values** — `var()` with fallbacks, `calc()`, `min()`, `max()`, `clamp()`,
and `px em rem % ch ex`.

### Not supported

| | |
|---|---|
| `box-shadow`, `text-shadow` | no shadows of any kind |
| `filter`, `backdrop-filter` | no blur, no saturate |
| `mix-blend-mode`, `mask-image`, `clip-path` | no compositing |
| `aspect-ratio` | set a height, or a padding-box ratio |
| `vw`, `vh` | use `%`, or px against the known slide size |
| `writing-mode`, `text-wrap: balance` | |
| `display: contents`, `position: sticky` | |
| `place-items`, `place-content` | the longhands work: `align-items`, `justify-items`, `align-content`, `justify-content` |
| logical properties — `inset`, `padding-block`, `margin-inline`, `border-block` | use the physical longhands |
| `rotate` / `scale` / `translate` as standalone properties | use the `transform` shorthand |
| `transform: translateZ()` and other 3-D functions | 2-D only |
| `@supports`, `@layer`, `@container` | the block is dropped entirely — everything inside is lost |
| `@media` with a feature query, e.g. `(min-width: …)` | only media *types* work; `@media print` matches, `@media screen` does not |
| `@keyframes` / `animation`, and `transition` | parse without complaint and do nothing |

The two rows to read twice are `@supports` and feature-query `@media`: a rule
guarded by either never applies, so a stylesheet that wraps a fallback in
`@supports` ships only the fallback's absence.

---

## 6. Checking a theme

WeasyPrint names every declaration it drops. With no logging configured,
Python's last-resort handler puts them on stderr, so the CLI shows them:

```bash
presence-cli deck.md /tmp/out.pdf
# WARNING:weasyprint:Ignored `box-shadow: 0 0 10px red` at 412:5, unknown property.
```

The line number is into the *generated* sheet, not your file, but the
declaration is quoted verbatim — grep your `theme.css` for it.

A silent render is not proof the theme is right: an `@supports` block, a
feature-query `@media` block, or a `transition` all vanish without a word. So
does a rule aimed at an element whose inline style outranks it.

Two habits worth having. Render your theme against a deck that touches
everything — a cover, headings, a blockquote, a code block, a table, a
two-column split, and one slide of each image layout — because a theme is
usually written against the two slides its author had open. And check the
overflow warning still fires, by writing a slide with far too much text on it:
if the app stays quiet, something in the stylesheet has taken the content out
of normal flow.

---

## History

On 2026-09-01 the arrangement moved out of inline styles and into the
stylesheet. Panel geometry, gallery tracks, cell `object-fit` and the progress
bar's width were all written onto the element in Python, where no selector
could reach them; each is now a rule in the generated sheet reading a published
measurement. The same change began stamping `data-layout`, `data-text`,
`data-images` and `data-shapes`. Rendering was unchanged — 166 renders across
every layout and all 29 installed themes came back pixel-identical.

Until 2026-09-01 `build_css()` ended by appending a block that blanked
`.slide::before` and `::after`, every heading border and padding, link
underlines, the rules around code and `pre`, a blockquote's left border, table
borders and zebra striping, and the divider between two columns — after the
theme's own stylesheet, specifically so it would win. Twenty-five of the
twenty-seven installed themes wrote rules that block silently discarded. It is
gone; this document is what replaced it.
