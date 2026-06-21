# Smart PDF — Trial & Privacy Server

FastAPI service for trial management + privacy policy.

## Endpoints
- `POST /trial/check` — body: {"deviceId": "..."} → trial status
- `GET /privacy` — privacy policy page
- `GET /` — health check

## Config (Render environment variables)
- `TRIAL_DAYS` — trial length in days (default 7)

## Run locally
pip install -r requirements.txt
uvicorn main:app --reload
