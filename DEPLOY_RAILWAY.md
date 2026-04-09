# Deploy to Railway (Multi-User Dashboard)

This project runs a Flask dashboard with multi-user login and per-user settings.

## 1) Create Railway project

- Push this folder to GitHub (recommended).
- In Railway: **New Project -> Deploy from GitHub Repo**

Railway will detect the `Procfile` and run:
- `web: python railway_app.py`

## 2) Add a MySQL database

This bot uses **MySQL** via `pymysql` (not PostgreSQL).

In Railway:
- **Add -> Database -> MySQL / MariaDB** (whichever Railway offers)

Then set these environment variables for the service:
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`

Use the values from the Railway database plugin connection panel.

## 3) Required environment variables

Set these in Railway -> Service -> Variables:

- `DASHBOARD_SECRET` (required; use a long random string)

Optional (only if you want defaults for the first admin user):
- `DELTA_API_KEY`
- `DELTA_API_SECRET`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

Notes:
- The **first registered user** becomes `admin` and may be auto-seeded from global settings.
- **Other users must add their own keys** in Settings.

## 4) Port binding

Railway sets `PORT` automatically.

`railway_app.py` binds to `0.0.0.0:$PORT` using `waitress` (production WSGI).

## 5) IP whitelist warning (Delta)

If you enabled IP whitelisting on Delta, you must whitelist the **server public IP** shown in:
- Settings -> Delta Exchange API -> "Server Public IP"

Important:
- On Railway/Cloud hosting, egress IP can change. If Delta requires a static whitelist, use a provider with static egress IP or disable IP whitelist.

## 6) Bot on Railway (important)

By default Railway runs the **dashboard only**.

To trade:
- Login -> Settings -> add your API key/secret
- Back to dashboard -> click **Start Bot**

Notes:
- The bot runs as a **subprocess** inside the same Railway service.
- If Railway restarts the service, the bot stops too.

## 7) First run

- Open the Railway public URL
- Go to `/register`
- Create your first user (admin)
- Open `/settings` and save keys
