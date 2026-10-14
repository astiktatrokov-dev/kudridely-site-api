# Kudridely Website API

Small API for the Kudridely studio website. It is separate from the Telegram bot polling process and uses the same Neon PostgreSQL database.

## Render settings

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set these environment variables in Render (never commit values to GitHub):

- `DATABASE_URL` — Neon PostgreSQL connection string.
- `BOT_TOKEN` — Telegram bot token.
- `ADMIN_IDS` — comma-separated numeric Telegram admin IDs.

## Endpoints

- `GET /health`
- `POST /api/inquiries`

POST body example:

```json
{
  "client_name": "Анна",
  "contact": "+7 900 000-00-00",
  "service_name": "Кудрявая стрижка",
  "desired_date": "2026-10-20",
  "desired_time": "14:00",
  "comment": "Хочу уточнить стоимость"
}
```

The endpoint saves the request in the `website_inquiries` table and then sends Telegram notifications to the numeric IDs in `ADMIN_IDS`.

CORS is open for initial testing from a local HTML file. After the website is published, restrict allowed origins to its real HTTPS domain.
