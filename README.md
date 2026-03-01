# BMB Collision Repair AI (VIN + Vision + PDF)

This version is a full-stack app with:

- VIN decode (NHTSA + optional extra provider for paint/build data)
- AI photo analysis for automatic preliminary estimate write-up
- Driver's license extraction for customer supplement fields (name/address/phone/email)
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
- `VIN_EXTRA_PROVIDER_URL=...` (for paint code / richer VIN data)
- `VIN_EXTRA_PROVIDER_TOKEN=...`
- `PORT=3000`

## 3) Run

```bash
npm start
```

Open:

- `http://localhost:3000`

## Deploy to cloud

This repo is now pre-configured for both Railway and Render.

### Option A: Railway (recommended fastest path)

1. Push this folder to a GitHub repo.
2. In Railway, create a new project from that GitHub repo.
3. Railway will auto-detect `railway.json`.
4. Add environment variables in Railway:
   - `OPENAI_API_KEY` (required)
   - `OPENAI_VISION_MODEL` (optional, default `gpt-4.1`)
   - `VIN_EXTRA_PROVIDER_URL` (optional)
   - `VIN_EXTRA_PROVIDER_TOKEN` (optional)
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
   - `VIN_EXTRA_PROVIDER_URL`
   - `VIN_EXTRA_PROVIDER_TOKEN`
   - `BMB_LOGO_URL`
6. Deploy and verify:
   - `https://<your-render-service>/api/health`

## Usage flow

1. Enter VIN and click `Decode VIN`.
2. Upload driver's license photo and click `Extract License` (or include it during generate).
3. Upload collision photos.
4. Click `Generate Estimate From Photos`.
5. Review all 5 deliverables + customer supplement header.
6. Click `Download PDF` for email-ready output.

## Paint code behavior

- Base VIN decode (NHTSA) usually does **not** include paint code.
- If `VIN_EXTRA_PROVIDER_URL` is configured and returns paint fields, the app will auto-fill paint code.
- If unavailable, paint code is marked `Unknown` and flagged in assumptions.

## AI fallback behavior

- If OpenAI key is not configured or AI call fails, server falls back to a rule-based preliminary estimate so the app still runs.

## API endpoints

- `POST /api/vin/decode`
- `POST /api/estimate/generate` (multipart form with `photos[]` + JSON `payload`)
- `POST /api/license/extract` (multipart form with `license`)
- `POST /api/report/pdf`
- `GET /api/health`

## Logo on supplement PDF

- By default, PDF generation uses the BMB logo asset from `bmbrhinetradeinc.com`.
- You can override with:
  - `BMB_LOGO_URL` for remote image URL
  - `BMB_LOGO_FILE` for local image path
