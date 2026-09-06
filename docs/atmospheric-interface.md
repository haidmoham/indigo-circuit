# Atmospheric interface

The interface adapts the whole-page atmosphere of [in here](https://blog.shin86.dev/)
and the [personal cluster](https://shin86.dev/). The reference is a stable reading
surface surrounded by scenery. The visual identity belongs to Pokémon scouting.
A two-tone Poké Ball replaces the reference star mark. The new vector habitat artwork is original to Indigo Circuit.

Tall grass, ghost woods, and surf change the scenery. Original vector illustrations
of Pikachu, Gengar, and Lapras sit quietly behind the League cards. Animated
translation stays separate from SVG placement so motion preserves their position.
Tall grass, route markers, and
coastal ripples replace the botanical flowers and celestial orbits. The saved scene
IDs remain `garden`, `night`, and `tide` so existing preferences continue to work.
Ghost woods is the default when no valid preference exists. An explicit saved
choice still wins.
The visible names describe habitats. They do not change a data source,
format, ranking, or calculation. Their small swatches preview the scene. The
selection persists between routes. Motion has a separate control. A reduced-motion
preference starts the page still. Hidden documents pause the scenery.

Heavy lowercase headings, small mono labels, muted type colors, and dark table
surfaces connect the pages. Warm cream and muted Poké Ball red define navigation.
Inset card borders and colored tier edges give player panels a trainer-card finish.
The Elite Four use one row of four below the champion on desktop. The grid changes
to two columns on tablets and one column on small screens. Player names retain
their source spelling. The league and ranking formulas remain available in native disclosure elements. Tables scroll
inside their containers on narrow screens. Charts use resolved CSS colors and
resize with the viewport.

## Champion honors

The League champion is the first player in the live seasonal ranking. The world
title is a separate, dated honor in `dashboard/static/championship-honors.json`.
Its 2026 TCG Masters entry cites the official Pokémon event results. Update that
record from an official result when a new championship is complete. Do not infer
the world title from League rank, a Worlds top-eight count, or a best placing.

When both honors belong to one person, the page combines them in one hero. When
the holders differ, the page honors both people separately. The world-title year
and source remain visible. A missing honors file must not prevent the live League
ranking from rendering. Season statistics and the season archetype retain their
original meaning; they are not labeled as the world-final statistics or deck.

`dashboard/static/atmosphere.css` owns the shared visual rules.
`dashboard/static/atmosphere.js` stores scene and motion preferences.
`dashboard/templates/base.html` contains the decorative SVG and shared controls.
Page templates retain their data requests and calculations. The ingest and
production validation pipeline are unchanged.

## Social preview

The Open Graph image is a separate server-rendered PNG. Keep its moonlit palette,
cream type, and gold champion emphasis consistent with the League page. Round
numeric labels before drawing. Fit text to measured bounds so long player names
and database float precision cannot overlap nearby content. Missing data must
produce a readable brand card. A world badge requires a matching dated honor.

Social metadata uses the public HTTPS image URL. Do not derive its scheme from
the application request behind the deployment proxy. Change the image version
when its visual grammar changes. External services can retain previews of links
that were already shared; changing the origin cannot force those messages to
refresh.

## Verification

- Render all ten Jinja templates and check each resulting inline script with
  `node --check`.
- Run `node --check dashboard/static/atmosphere.js`.
- Run `python scripts/test_pipeline_gate.py`. Its eight fixtures exercise the
  existing acceptance and rejection rules. They do not report the current
  production database's health.
- Inspect populated league, ranking, player, meta, tech, search, and EV views at
  desktop and mobile widths. Check scene persistence, motion control, keyboard
  focus, table scrolling, and chart resizing.

Local visual inspection can render these same templates against the public API.
The full dashboard process uses Linux `fcntl` for its pipeline lock. Keep the
scheduler disabled during local application checks.
