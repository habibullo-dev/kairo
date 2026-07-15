# Kira brand asset provenance

The Kira geometric monogram and three theme preloader concepts were supplied by the project
owner in the `logo_preloader_three_themes` asset pack on 2026-07-15. The pack did not include
separate author, source, attribution, or third-party license metadata. Distribution therefore
relies on the project owner's authorization for these supplied assets.

## Imported source fingerprints

| Source file | SHA-256 |
| --- | --- |
| `logo/rebuilt_logo_transparent.svg` | `257360cecc2e766ffbf6be2c7ab04527285588d274178e9a0679a0f76fbfe8f3` |
| `logo/rebuilt_logo_transparent_black.svg` | `204cf11b8c52d152632afb817f6066b3763a6dfb678cc1d8c01d455741241608` |

## Production adaptations

The source geometry is unchanged. Production assets crop the sparse `0 0 900 900` canvas to
`206 193 500 500`, add accessible Kira titles and descriptions, and provide fixed white, black,
and teal surface variants. The preloader defines the geometry once and reuses it for its four
clipped letter reveals and final mark.

The three workspace photographs were already part of this repository and are brand-neutral.
They were renamed byte-for-byte for the Kira light, noir, and neon themes; their visual data was
not recompressed or altered.

The supplied standalone demo HTML, CSS, and JavaScript are reference material only. Production
uses namespaced styles, a reduced-motion bypass, once-per-tab display, route-readiness dismissal,
and an independent three-second fail-open boundary.
