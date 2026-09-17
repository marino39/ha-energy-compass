# Energy Compass branding

`icon.svg` preserves the supplied Energy Compass artwork. The SVG contains an
embedded raster image; the light PNG assets are size exports of that image without
redesigning, cropping, or changing its background.

- `icon.png` and `logo.png`: 256 × 256 pixels.
- `icon@2x.png` and `logo@2x.png`: 512 × 512 pixels.
- `dark_icon.png` and `dark_logo.png`: 256 × 256 pixels.
- `dark_icon@2x.png` and `dark_logo@2x.png`: 512 × 512 pixels.

The dark companion uses a charcoal background with the same blue-and-gold
compass design. It was adapted from the supplied image with built-in imagegen.

Home Assistant serves these assets from the integration's local `brand/`
directory and selects the `dark_` variants for dark themes. The repository
README uses a theme-aware picture with the light image as its fallback.
