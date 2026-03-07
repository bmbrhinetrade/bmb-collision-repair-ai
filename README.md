# BMB Collision Repair AI (VIN + Vision + PDF)

This version is a full-stack app with:

- VIN decode (NHTSA + optional extra provider for paint/build data)
- Door-jamb label photo extraction (VIN + paint code)
- AI photo analysis for automatic preliminary estimate write-up
- OEM parts lookup integration (dealership part numbers + list pricing via provider)
- Driver's license extraction for customer supplement fields (name/address/phone/email)
- Real-time voice edit button for verbal estimate adjustments
- Default shop labor rates + automated labor-hour/total estimate calculation
- 5 required deliverables (repair/replace, missing ops, notes, red flags, system notes)
- Email-ready PDF export

## 1) Install

```bash
cd /Users/michaelsaad/Documents/Playground
npm install
```

## 2) Configure environment

```bash
cp .env.example .env
```

Set at minimum:

- `OPENAI_API_KEY=...` (required for true AI vision mode)

Optional:

- `OPENAI_VISION_MODEL=gpt-4.1`
- `OPENAI_LICENSE_MODEL=gpt-4.1-mini` (low-cost model for driver's license OCR)
- `OPENAI_DOOR_LABEL_MODEL=gpt-4.1-mini` (low-cost model for door-jamb OCR)
- `OPENAI_LICENSE_MAX_OUTPUT_TOKENS=220`
- `OPENAI_DOOR_LABEL_MAX_OUTPUT_TOKENS=260`
- `EXTRACTION_CACHE_TTL_SECONDS=86400`
- `EXTRACTION_CACHE_MAX_ITEMS=300`
- `VIN_EXTRA_PROVIDER_URL=...` (for paint code / richer VIN data)
- `VIN_EXTRA_PROVIDER_TOKEN=...`
- `OEM_PARTS_PROVIDER_URL=...` (POST endpoint for OEM part # + list pricing by component)
- `OEM_PARTS_PROVIDER_TOKEN=...`
- `PORT=3000`

## 3) Run

```bash
npm start
```

Open:

- `http://localhost:3000`

## Deploy to cloud

This repo is now pre-configured for both Railway and Render.
Nixpacks is explicitly pinned to Node via `nixpacks.toml`.

### Option A: Railway (recommended fastest path)

1. Push this folder to a GitHub repo.
2. In Railway, create a new project from that GitHub repo.
3. Railway will auto-detect `railway.json`.
4. Add environment variables in Railway:
   - `OPENAI_API_KEY` (required)
   - `OPENAI_VISION_MODEL` (optional, default `gpt-4.1`)
   - `OPENAI_LICENSE_MODEL` (optional, default `gpt-4.1-mini`)
   - `OPENAI_DOOR_LABEL_MODEL` (optional, default `gpt-4.1-mini`)
   - `OPENAI_LICENSE_MAX_OUTPUT_TOKENS` (optional, default `220`)
   - `OPENAI_DOOR_LABEL_MAX_OUTPUT_TOKENS` (optional, default `260`)
   - `EXTRACTION_CACHE_TTL_SECONDS` (optional, default `86400`)
   - `EXTRACTION_CACHE_MAX_ITEMS` (optional, default `300`)
   - `VIN_EXTRA_PROVIDER_URL` (optional)
   - `VIN_EXTRA_PROVIDER_TOKEN` (optional)
   - `OEM_PARTS_PROVIDER_URL` (optional for live OEM parts/pricing)
   - `OEM_PARTS_PROVIDER_TOKEN` (optional)
   - `BMB_LOGO_URL` (optional override)
5. Deploy and open:
   - `https://<your-app>.up.railway.app/api/health`
6. If health returns `{ \"ok\": true }`, open the root URL and use the app.

### Option B: Render

1. Push this folder to a GitHub repo.
2. In Render, choose **New > Blueprint** and select your repo.
3. Render will use `render.yaml`.
4. Set required secret in Render dashboard:
   - `OPENAI_API_KEY`
5. Optional variables:
   - `OPENAI_VISION_MODEL`
   - `OPENAI_LICENSE_MODEL`
   - `OPENAI_DOOR_LABEL_MODEL`
   - `OPENAI_LICENSE_MAX_OUTPUT_TOKENS`
   - `OPENAI_DOOR_LABEL_MAX_OUTPUT_TOKENS`
   - `EXTRACTION_CACHE_TTL_SECONDS`
   - `EXTRACTION_CACHE_MAX_ITEMS`
   - `VIN_EXTRA_PROVIDER_URL`
   - `VIN_EXTRA_PROVIDER_TOKEN`
   - `OEM_PARTS_PROVIDER_URL`
   - `OEM_PARTS_PROVIDER_TOKEN`
   - `BMB_LOGO_URL`
6. Deploy and verify:
   - `https://<your-render-service>/api/health`

## Usage flow

1. Enter VIN and click `Decode VIN`.
2. Upload door-jamb label and click `Extract Label` to pull VIN/paint code.
3. Upload driver's license photo and click `Extract License` (or include it during generate).
4. Upload collision photos.
5. Click `Generate Estimate From Photos`.
6. Review all 5 deliverables + customer supplement header + OEM parts/pricing + totals.
7. Click `Download PDF` for email-ready output.
8. Optional: click `Start Talk Edit` and speak updates (rates, charges, line changes) to update estimate in real time.

## Default shop rates (preloaded)

- Paint & Body Labor: `$83/hr`
- Structural Labor: `$83/hr`
- Frame Straightening: `$135/hr`
- Mechanical Labor: `$175/hr`
- Electrical Labor: `$150/hr`
- Paint Materials: `$46 per paint hour`
- Inside Storage: `$180/day`
- Outside Storage: `$180/day`
- Towing: `$12/mile`

These are editable in the app before generating each estimate.

## Paint code behavior

- Base VIN decode (NHTSA) usually does **not** include paint code.
- If `VIN_EXTRA_PROVIDER_URL` is configured and returns paint fields, the app will auto-fill paint code.
- If unavailable, paint code is marked `Unknown` and flagged in assumptions.

## AI fallback behavior

- If OpenAI key is not configured or AI call fails, server falls back to a rule-based preliminary estimate so the app still runs.

## Low-cost extraction mode

- Driver's license and door-jamb extraction now use dedicated low-cost models (`OPENAI_LICENSE_MODEL`, `OPENAI_DOOR_LABEL_MODEL`).
- These extraction calls are cached by image hash in-memory, so re-uploading the same image does not call OpenAI again until cache expiry.
- If OpenAI quota is exceeded (`429`), extraction returns a safe fallback response instead of crashing the flow.

## OEM parts behavior

- The app derives damaged components from the estimate and sends them to `OEM_PARTS_PROVIDER_URL` (if configured).
- Provider should return `items[]` with at least `component`, `partNumber`, `description`, `quantity`, `listPrice`.
- If no provider is configured (or provider fails), the app inserts placeholder OEM rows with `$0.00` pricing and flags assumptions.

## API endpoints

- `POST /api/vin/decode`
- `POST /api/estimate/generate` (multipart form with `photos[]`, optional `license`, optional `vehicleLabel`, + JSON `payload`)
- `POST /api/license/extract` (multipart form with `license`)
- `POST /api/vehicle-label/extract` (multipart form with `vehicleLabel`)
- `POST /api/estimate/recalculate` (JSON report + rates/charges for live recalculation, used by voice edits)
- `POST /api/report/pdf` (multipart form with `report` JSON string and optional `photos[]` for damage photo pages)
- `GET /api/health`

## Logo on supplement PDF

- By default, PDF generation uses the BMB logo asset from `bmbrhinetradeinc.com`.
- You can override with:
  - `BMB_LOGO_URL` for remote image URL
  - `BMB_LOGO_FILE` for local image path
