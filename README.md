# Lead Finder API (Live + Free Fallback)

This app can run in 3 modes:
- `demo`: sample data only
- `premium`: Google Places + Hunter APIs (best quality)
- `free_fallback`: no paid keys, uses public search + website extraction

You can use two workflows:
- `Search mode`: niche + city
- `Direct website mode`: paste one business website and enrich that company directly

Direct website mode returns:
- likely owner/decision-maker name
- owner title (Founder/Owner/etc)
- best-confidence business email
- business phone (if found)

## 1. Run

```powershell
cd "C:\Users\USER\Desktop\email finder"
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env -Force
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

UI:
- http://127.0.0.1:8000/

Docs:
- http://127.0.0.1:8000/docs

## 2. No Paid API Keys (Your Case)

In `.env`:
- `DEMO_MODE=false`
- leave API keys blank

The app will use `free_fallback` mode:
- OSM Nominatim for business discovery
- Website crawling for owner/email/phone extraction
- OpenCorporates fallback for officer names

Quality is lower than premium, but it returns real data where available.

## 2.1 Email Accuracy Mode

For stricter email quality, keep these in `.env`:
- `EMAIL_MIN_CONFIDENCE=0.8`
- `EMAIL_REQUIRE_SAME_DOMAIN=true`
- `EMAIL_REQUIRE_MX=true`
- `EMAIL_REJECT_GENERIC_LOCALS=true`

This favors precision over volume and may return fewer emails.

## 3. Optional Premium Upgrade

If later you add keys, quality improves:
- `GOOGLE_PLACES_API_KEY`
- `HUNTER_API_KEY`

Optional:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `OPENCORPORATES_API_TOKEN`

## 4. Check Mode

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness
```

Look at `discovery_mode`:
- `premium`
- `free_fallback`
- `demo`

## 5. Endpoints

- `GET /health`
- `GET /api/v1/readiness`
- `POST /api/v1/discover`
- `GET /api/v1/leads`
- `GET /api/v1/leads/export.csv`

`POST /api/v1/discover` accepts either:
- `website` (optional `business_name`, `city`)
- or `niche` + `city`

## 6. Compliance

Use legally:
- honor opt-outs/suppression lists
- follow CAN-SPAM and TSR/TCPA rules

## 7. Database

SQLite file: `leadfinder.db`.

## 8. Android APK

An Android WebView app scaffold is available in:
- `mobile-android/`

Setup and build instructions:
- `mobile-android/README.md`

## 9. GitHub Automation (CI/CD)

This repo includes GitHub Actions workflow:
- file: `.github/workflows/ci-cd.yml`
- CI runs on every push/PR:
  - install dependencies
  - `python -m compileall app`
  - smoke test (`GET /health`) using FastAPI TestClient
- CD runs on pushes to `main` only when deploy secret exists.

### Enable Auto-Deploy

In GitHub repo settings, add this secret:
- `DEPLOY_WEBHOOK_URL`

Use your hosting provider deploy hook URL (for example Render/Railway).

Behavior after setup:
- push to `main` -> CI runs, then deploy webhook is triggered automatically.
