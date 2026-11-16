Add a static page `web/audit.html` that shows the audit log from `audit.json` (fetched relative to
the page, an array of rows with `ts`, `actor`, `action`, `target`) as a table with those four
columns. Add a labelled text input with `id="filter"` that hides rows whose text does not contain
the typed text (case-insensitive) as the user types, and show the message `No matching rows` when
nothing matches. Link the page from `web/index.html`. Keep it accessible (labelled input, table
header cells, a `lang` attribute) and working at a 390 px viewport with no horizontal scroll.
