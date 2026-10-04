# БИТВА ШКОЛ — GitHub / Render

## GitHub

Загружай содержимое проекта в корень репозитория. Файл `.env` в GitHub не добавляй: он уже исключён через `.gitignore`. Для локальной работы используй `.env.example` как шаблон.

## Render

Blueprint уже подготовлен в `render.yaml`:

- Python web service;
- `/healthz` как health check;
- существующий Postgres `kaliningrad-school-leaderboard-db` через `DATABASE_URL`;
- Render Key Value для `REDIS_URL`;
- `ADMIN_USERNAME` и `ADMIN_PASSWORD` запрашиваются отдельно (`sync: false`);
- `SECRET_KEY` генерируется Render;
- Cloudflare Turnstile обязателен для входа обычных пользователей;
- дополнительная Turnstile-проверка админ-входа по умолчанию отключена, чтобы парольный вход не ломался из-за недоступности Cloudflare.

Не удаляй существующую БД и не создавай новую при обновлении. `data/leaderboard.db` нужен только для локального SQLite-теста и в Render не используется.

## Важное про Free Render

Free Web Service и Free Postgres подходят для теста. Free Postgres имеет лимит 1 GB и срок жизни 30 дней, после чего БД нужно перевести на платный план, иначе она будет удалена после grace period. Для постоянного публичного проекта используй платную БД/хостинг.
