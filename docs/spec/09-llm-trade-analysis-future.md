# Module 09 — LLM-Powered Trade Analysis (Future / Post-v1)

> **Status: future module, not part of v1 launch.** This is carried over conceptually from your existing personal-use app and documented now so the schema/architecture decisions in earlier modules don't accidentally block it later. Build this only after the core platform (Modules 01–08) is live and stable.

## Purpose
Generate LLM-written analysis and predictive commentary on top of the GEX snapshots already being collected (Module 02) — e.g. a daily written summary of dealer positioning, and/or a short-term "likely price landing zone" prediction — as a premium content feature, most likely paid-tier-only or a separate add-on.

## What carries over from your existing app
- **LLM providers:** Ollama (local) or LM Studio (local, OpenAI-compatible API) — both run models locally on your home Mac, no per-token API cost, but add real CPU/RAM load to the same machine running your scheduler and web app (capacity-plan this before enabling).
- **Config pattern:** `LLM_PROVIDER=ollama|lmstudio`, then provider-specific `OLLAMA_BASE_URL`/`OLLAMA_MODEL`/`OLLAMA_TIMEOUT` or `LM_STUDIO_BASE_URL`/`LM_STUDIO_MODEL`/`LM_STUDIO_TIMEOUT`.
- **Context window constraint:** your existing prompts run ~4,000–5,000 tokens (multiple past GEX analyses + option data) — your LM Studio default of 4,096 tokens was too small and had to be raised to 8,192+. Factor this into model choice; smaller context models will need `LLM_MAX_ANALYSIS_CHARS`-style truncation of historical context (you used 900 chars/analysis as your working value).
- **Two content types from the old app, worth deciding which (if any) to bring forward:**
  1. **`daily_gex_analysis`** — a written narrative analysis tied to a specific GEX snapshot (`gex_file_name`, `analysis_date`, `analysis_text`, `model`, plus a small metadata block of total_gex/spot/close).
  2. **`spx_predictions`** — a more structured "landing zone" prediction (probability-weighted price ranges with labels like "support"/"resistance"), generated async via a job-tracking pattern (`spx_prediction_jobs`: `status: running → done/error`).

## Open decisions for when you pick this up
- Is this paid-tier-only, or a separate paid add-on on top of the regular subscription?
- Narrative analysis only, structured predictions only, or both?
- Run on every snapshot, or only EOD/daily — running an LLM pass on every 5-minute 0DTE snapshot is almost certainly overkill and a resource risk on a home Mac.
- Local LLM (Ollama/LM Studio, free but resource-constrained) vs. a hosted API (costs per call, but doesn't compete with your scheduler/web app for CPU) — worth revisiting this tradeoff once you have real paid-user load on the home Mac.

## Suggested collections (when this is built)
Adapted from the old schema, scoped to the new platform's multi-tenant shape:
```json
// daily_gex_analysis
{
  "_id": ObjectId,
  "symbol": "string",
  "snapshot_ref": "ObjectId (FK into gex_intraday/gex_weekly/etc.)",
  "analysis_date": "date",
  "analysis_text": "string (markdown)",
  "model": "string",
  "created_at": "datetime"
}

// gex_predictions (renamed from spx_predictions to support any symbol, not just SPX)
{
  "_id": ObjectId,
  "symbol": "string",
  "prediction_id": "uuid",
  "generated_at": "datetime",
  "timeframe": "daily | weekly",
  "landing_zones": [{"zone_min": "float", "zone_max": "float", "probability": "float", "label": "string"}],
  "model": "string"
}

// gex_prediction_jobs
{
  "_id": ObjectId,
  "job_id": "uuid",
  "status": "running | done | error",
  "started_at": "datetime",
  "finished_at": "datetime"
}
```

## Why this is deferred
- Not core to the v1 value proposition (raw GEX data access) — it's a richer/premium layer on top.
- Adds real infra load (local LLM inference) to a home-Mac deployment that's already running the web app + scheduler + MongoDB (see Module 07's reliability notes).
- Worth validating paid-user demand for raw GEX data first before investing in this layer.
