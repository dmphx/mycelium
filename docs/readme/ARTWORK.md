# README artwork

The README uses exported product artwork with separate desktop and phone compositions.

- `poster.html` is the editable cover composition.
- `hero.png` is exported at 900 CSS pixels wide; `hero-mobile.png` at 380 CSS pixels wide.
- Where present, `workflow.html` is the editable workflow panel, exported at the same two widths.
- Exports use a 1.5 device scale. Wait for document fonts and images to finish loading before capture.
- Use Chromium to capture the `.poster` or `.workflow` element. Only the saved images are embedded in the README.

The composition retains product-specific colors, artwork, and existing attribution. Clearly identified illustrations are separate from real product captures. TheaterHelper’s production capture uses only its temporary synthetic demo data.

Fonts are loaded from Google Fonts in the editable source; the exports contain no runtime font or network dependency.

## Visual chapters

The README presents its product story with a cover, a product showcase, three illustrated feature chapters, a workflow, and an ending panel. Existing screenshots or project photographs remain in the story where relevant.

- `showcase.html`, `feature-1.html`, `feature-2.html`, `feature-3.html`, and `next.html` are editable compositions; the matching PNGs are exported at 900 CSS pixels wide and a 1.5 device scale.
- Wait for document fonts and embedded images, then capture the `.chapter` element with Chromium. The exported PNGs have no network or font dependency.
- Illustrations describe the documented experience. They are editorial compositions, not independent evidence of runtime acceptance or release availability.
- The discovery image is the existing project asset. Upstream attribution, licensing, and the original screenshot remain in place.
- Embed saved PNGs using native Markdown image syntax and relative repository paths. GitHub Mobile has limitations with HTML `picture` / `source` markup; desktop viewport tests do not verify the native mobile app.

Existing artwork and photography retain their original provenance and licensing. Product status is carried in each visual chapter; current behavior and availability remain in the maintained source and linked product guides.
