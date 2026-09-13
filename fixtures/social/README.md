# Manual EOD preview files

Social Studio accepts JSON and `.xlsx` files up to 5 MB for preview-only EOD drafts.
These inputs never populate market-data collections and can never be published to X.

## JSON

The top-level object must contain exactly `mode`, `metadata`, `options`, `analysis`,
`strike_gex`, and `narrative`. The two JSON files in this directory are complete
metrics-mode examples.

In `raw` mode, leave `analysis` as `{}` and `strike_gex` as `[]`. Each `options`
row requires `type`, `strike`, `expiry`, `gamma`, `iv`, and `open_interest`.
Optional fields are `delta`, `theta`, `vega`, and `volume`.

In `metrics` mode, leave `options` as `[]`. Exposure values in `analysis` and
`strike_gex` use billions. Supplied walls, hot zone, exposures, and net GEX must
agree with the per-strike rows.

## Excel

The workbook must contain exactly these sheets:

- `Metadata`: two columns headed `Field`, `Value`; include `mode`, `symbol`,
  `trade_date`, `snapshot_at`, and `spot_price`.
- `Options`: a header row followed by raw contracts. Keep only the header row in
  metrics mode.
- `Analysis`: two columns headed `Field`, `Value`. Keep only the header row in
  raw mode.
- `StrikeGEX`: columns `strike`, `call_gex_b`, and `put_gex_b`. Keep only the
  header row in raw mode.
- `Narrative`: two columns headed `Field`, `Value` containing the six bounded
  narrative fields when file narrative is selected.

Dates use ISO format. `snapshot_at` must include an offset and resolve to the
trade date in Central Time before the 3:00 PM CT expiry.
