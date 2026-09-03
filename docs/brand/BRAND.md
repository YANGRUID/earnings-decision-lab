# EDL Logo Asset Pack

**In this repository:** the white-background mark is the default everywhere (browser tab, README,
GitHub). The app sidebar shows the navy-tile mark on the light theme and the white-background
mark on the dark theme (`frontend/src/components/Layout.tsx`, `frontend/src/index.css`). Files:
`frontend/public/brand/` (served) and `docs/brand/` (documentation). Replacing the logo means
replacing those files; no code change is needed.

This package contains the production master artwork for the selected first EDL logo: a deep ascending peak, a warm-gold opportunity facet, and a sweeping market arc. The artwork has no lettering.

## Master colors

| Role | HEX | RGB | Approx. CMYK | Use |
|---|---:|---:|---:|---|
| Deep Navy | `#0A1118` | 10, 17, 24 | 58, 29, 0, 91 | Primary peak and market arc; dark background |
| Opportunity Gold | `#CDB086` | 205, 176, 134 | 0, 14, 35, 20 | Middle facet / accent |
| Warm White | `#F7F4EF` | 247, 244, 239 | 0, 1, 3, 3 | Reversed mark on dark backgrounds |
| Pure Black | `#000000` | 0, 0, 0 | 0, 0, 0, 100 | One-color production |
| Pure White | `#FFFFFF` | 255, 255, 255 | 0, 0, 0, 0 | One-color reversed production |

CMYK values are starting points only. Confirm output with the printer's ICC profile and a physical proof for color-critical work.

## Clear space / safe area

Keep clear space on every side equal to at least **12.5% of the visible mark width**. Do not place text, borders, UI controls, or other graphics inside this area. The supplied square canvas already exceeds that minimum clear space.

For app icons and favicons, use the supplied icon files rather than cropping the master. The icon artwork is centered within a square deep-navy field and retains the intended safe area.

## Minimum size

- Standard digital use: **32 px wide or larger**.
- Absolute digital minimum: **24 px wide**; below this, use the supplied favicon/icon exports.
- Print: **8 mm wide or larger** for the full-color mark.
- Embroidery, engraving, or other coarse processes: test at final size and prefer the monochrome artwork.

## Variant selection

- **Primary / transparent:** use on white, cream, or other very light backgrounds.
- **Light:** primary colors on a fixed white background.
- **Dark:** warm-white and gold mark on the deep-navy background.
- **Monochrome black:** stamps, fax, one-color print, laser engraving, and light surfaces.
- **Monochrome white:** reversed one-color use; place only on a dark or sufficiently contrasting background.

Do not rotate, stretch, skew, outline, add shadows, recolor individual pieces, change the negative-space wedge, or alter the relationship among the three paths.

## File guide

- `SVG/`: editable vector masters. Every visible shape is an independent named `<path>`; there are no text elements.
- `PDF/`: vector PDFs for print and general handoff.
- `EPS/`: vector EPS exports for legacy print/vendor workflows.
- `PNG/4096/`: 4096 × 4096 production rasters, including transparent, white-background, dark-background, and monochrome versions.
- `Icons/`: app icons at 1024, 512, 256, 128, 64, and 32 px, plus a multi-resolution `favicon.ico`.
- `Reference/selected-original.png`: the low-resolution selected image used as the reconstruction reference.
- `MANIFEST-SHA256.txt`: integrity hashes for every packaged asset.

## Reconstruction note

The selected reference supplied in the conversation was a 191 × 139 raster with an off-white background and a narrow dark edge at the far right. The production masters remove those capture artifacts and rebuild the approved silhouette as clean vector paths. The original three-part proportions, angle, negative space, and visual balance are preserved; low-resolution anti-aliasing and compression noise are intentionally not reproduced.

## Export settings

- Master canvas: 1024 × 1024 SVG viewBox.
- High-resolution PNG: 4096 × 4096, lossless.
- Transparent masters: straight alpha preserved.
- PDF/EPS: generated directly from the SVG path masters; no embedded bitmap logo artwork.
