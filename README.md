# Resume Optimizer AI

ATS-focused resume tailoring app with a Python/Flask backend and a React/Vite frontend.

## Local Development

### Backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

## Free Deployment Setup

Recommended split:

1. Frontend on [Vercel](https://vercel.com)
2. Backend on [Render](https://render.com)
3. Database on a free hosted Postgres service such as [Supabase](https://supabase.com)

### Why not local SQLite on Render?

Render free web services use ephemeral disk, so local SQLite files can be lost when the service restarts or redeploys. For saved history and generated resume records, use a hosted database instead.

### Backend on Render

Use:

- Root directory: `backend`
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`

Set this environment variable in Render:

- `DATABASE_URL` = your hosted Postgres connection string

### Frontend on Vercel

Use:

- Root directory: `frontend`
- Build command: `npm run build`
- Output directory: `dist`

Set this environment variable in Vercel:

- `VITE_API_BASE_URL` = your Render backend URL

## Environment Examples

- `backend/.env.example`
- `frontend/.env.example`

## Notes

- The backend now returns PDF output only.
- The app keeps runtime-generated resume history in the database instead of local generated files.
- If `DATABASE_URL` is not set, the backend falls back to local SQLite for development only.
