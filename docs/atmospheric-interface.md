# Atmospheric interface

The interface adapts the whole-page atmosphere of [in here](https://blog.shin86.dev/)
and the [personal cluster](https://shin86.dev/). The reference is a stable reading
surface surrounded by scenery. The visual identity belongs to Pokémon scouting.
A two-tone Poké Ball replaces the reference star mark. The new vector habitat artwork is original to Indigo Circuit.

Tall grass, ghost woods, and surf change the scenery. Original vector illustrations
of Pikachu, Gengar, and Lapras anchor each habitat. Tall grass, route markers, and
coastal ripples replace the botanical flowers and celestial orbits. The saved scene
IDs remain `garden`, `night`, and `tide` so existing preferences continue to work.
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

`dashboard/static/atmosphere.css` owns the shared visual rules.
`dashboard/static/atmosphere.js` stores scene and motion preferences.
`dashboard/templates/base.html` contains the decorative SVG and shared controls.
Page templates retain their data requests and calculations. The ingest and
production validation pipeline are unchanged.

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
