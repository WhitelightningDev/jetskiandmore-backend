# jetskiandmore-backend

FastAPI backend for Jet Ski & More bookings and payments.

## Local development

1. Create a virtualenv and install dependencies:
   - `python -m venv .venv`
   - `source .venv/bin/activate` (or `.\.venv\Scripts\activate` on Windows)
   - `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - MongoDB connection (`MONGODB_URI` or `JSM_MONGODB_URI`)
   - Email settings (`JSM_GMAIL_*`, `JSM_EMAIL_*`)
   - Yoco keys (`JSM_YOCO_*`)
3. Run the API:
   - `uvicorn app.main:app --reload`
4. Health check:
   - `GET http://localhost:8000/health`

## Deploying to Render

This repo includes a `render.yaml` blueprint for Render.

### One-click setup

1. Push this repo to GitHub or GitLab.
2. In Render, create a new **Blueprint** and point it at this repo.
3. Render will detect `render.yaml` and create a **Web Service**:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -k uvicorn.workers.UvicornWorker app.main:app -b 0.0.0.0:$PORT`
4. Set environment variables on the service:
   - `JSM_MONGODB_URI` – MongoDB connection string (e.g. MongoDB Atlas)
   - `JSM_MONGODB_DB` – database name (default: `jetskiandmore`)
   - `JSM_GMAIL_USER`, `JSM_GMAIL_APP_PASSWORD`, `JSM_EMAIL_TO`, `JSM_EMAIL_FROM_NAME`
   - `JSM_YOCO_PUBLIC_KEY`, `JSM_YOCO_SECRET_KEY`, `JSM_YOCO_CHECKOUT_TOKEN`
   - `JSM_YOCO_CLIENT_ID`, `JSM_YOCO_CLIENT_SECRET`
   - `JSM_SITE_BASE_URL` – your frontend base URL

Render automatically sets `PORT` and the service listens on it via the start command.

Once deployed, the health endpoint will be:

- `GET https://<your-render-service>.onrender.com/health`
