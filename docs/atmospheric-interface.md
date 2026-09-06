# Atmospheric interface

The interface adapts the whole-page atmosphere of [in here](https://blog.shin86.dev/)
and the [personal cluster](https://shin86.dev/). The reference is a stable reading
surface surrounded by scenery. The new vector artwork is original to Indigo
Circuit.

Garden, night, and tide change the scenery. They do not change a data source,
format, ranking, or calculation. Their small swatches preview the scene. The
selection persists between routes. Motion has a separate control. A reduced-motion
preference starts the page still. Hidden documents pause the scenery.

Heavy lowercase headings, small mono labels, muted type colors, and dark table
surfaces connect the pages. Player names retain their source spelling. The league
and ranking formulas remain available in native disclosure elements. Tables scroll
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
