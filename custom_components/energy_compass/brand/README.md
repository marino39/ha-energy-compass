# Energy Compass branding

The Energy Compass mark is derived from the supplied blue-and-gold artwork.
Its canvas background and the negative space inside the compass are transparent;
the colored sectors and white battery details remain part of the artwork.
`icon.svg` contains an embedded transparent PNG, rather than vector paths.

- `icon.png` and `logo.png`: 256 × 256 pixels.
- `icon@2x.png` and `logo@2x.png`: 512 × 512 pixels.
- `dark_icon.png` and `dark_logo.png`: 256 × 256 pixels.
- `dark_icon@2x.png` and `dark_logo@2x.png`: 512 × 512 pixels.

Both light and dark variants have transparent backgrounds. The `dark_` assets
retain the dark companion's brighter blue tones for Home Assistant's theme
selection. PNG exports retain an RGBA alpha channel.

Background extraction used the built-in imagegen tool, with this edit brief:
remove the white/charcoal background and internal negative space, preserve the
existing compass shapes and colors, retain opaque white battery details, and
leave clean antialiased edges without a matte or halo. Assets are resized from
the transparent master to the dimensions above.

Home Assistant serves these assets from the integration's local `brand/`
directory and selects the `dark_` variants for dark themes. The repository
README uses a theme-aware picture with the light image as its fallback. The
512-pixel documentation copies in `docs/assets/` must match these brand assets.
