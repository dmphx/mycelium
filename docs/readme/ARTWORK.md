# README artwork

The README uses exported product artwork with separate desktop and phone compositions.

- `poster.html` is the editable cover composition.
- `hero.png` is exported at 900 CSS pixels wide; `hero-mobile.png` at 380 CSS pixels wide.
- Where present, `workflow.html` is the editable workflow panel, exported at the same two widths.
- Exports use a 1.5 device scale. Wait for document fonts and images to finish loading before capture.
- Use Chromium to capture the `.poster` or `.workflow` element. Only the saved images are embedded in the README.

The composition retains product-specific colors, artwork, and existing attribution. Clearly identified illustrations are separate from real product captures. TheaterHelper’s production capture uses only its temporary synthetic demo data.

Fonts are loaded from Google Fonts in the editable source; the exports contain no runtime font or network dependency.
