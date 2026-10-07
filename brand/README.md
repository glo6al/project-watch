# Project W.A.T.C.H. brand assets

The approved identity is three empty outlined monitors facing inward, as seen from inside the room. No center square, eye, filled screens, glow or texture. The production artwork is a geometric vector reconstruction of the approved concept; the concept image and the design studies are kept outside the repository.

## Assets

- `svg/agent-watch-icon-*.svg`: mark only, 512 × 256.
- `svg/agent-watch-logo-*.svg`: horizontal wordmark, 1280 × 360.
- `svg/agent-watch-logo-stacked-*.svg`: approved stacked composition, 1000 × 720.
- `svg/agent-watch-app-icon.svg`: square dark app tile, 512 × 512.
- `png/`: matching raster versions at native SVG dimensions.
- `favicon/`: 16, 32 and 48 px PNGs, a three-size ICO, 180 px Apple touch icon, and 192/512 px app icons. Artwork retains its proportions on the square canvas.
- `preview/contact-sheet.png`: rendered inventory, including favicons at actual size.

Each mark and wordmark comes in five variants: transparent signal green (`#36D879`), signal green on near-black (`#090D13`), transparent graphite (`#17212B`), graphite on soft light (`#F4F7F5`), and transparent white. Use graphite on light surfaces and signal green or white on dark surfaces.

All production SVG lettering is outlined; no runtime font dependencies. The wordmark uses the approved Squared Heavy (option B) display lettering: heavy strokes, clipped corners and square punctuation, drawn to echo the monitor frames and the app’s terminal aesthetic. Keep the screens empty and do not stretch the mark.

## Build and integration

Run `npm ci --prefix brand/tools` and `node brand/tools/build-assets.js` from the project root. The script builds the pack and synchronizes the two README logos in `docs/media/` and four application files in `src/web/static/`. `tools/build-wordmark.js` expands the approved heavy lettering into filled outlines and generates `tools/wordmark-paths.json`. The small PROJECT label uses the outlined geometry in `tools/project-label-path.json`, matching the selected study. Rebuilding the production assets requires no installed fonts.

The README uses the stacked composition. The header and lock screen use the mark. Application URLs include a version query so cached old assets are refreshed.

`tools/capture-demo.js` captures the real synthetic demo in Chrome through Playwright, including the three documentation screenshots and temporary frames for the demo GIF. It expects `src/web` served on localhost port 18794. No real session data is used.

Superseded production artwork has been removed. The approved concept image and the design studies are process material and are not published.

## Stable filenames

Original `dark-green` filenames are preserved for compatibility and now contain the approved graphite artwork (#17212B). The `graphite` filenames are identical copies. Both sets are generated on every build. The README uses its original `docs/media/logo-dark-green.svg` path. Do not remove or rename public asset paths when changing the design or color.
