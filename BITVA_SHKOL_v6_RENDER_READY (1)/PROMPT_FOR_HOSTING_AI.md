# ТЗ ДЛЯ ДРУГОЙ ИИ — подготовка и хостинг «БИТВА ШКОЛ»

Ты получаешь готовый проект «БИТВА ШКОЛ». Твоя задача — не переписывать проект с нуля, а безопасно подготовить текущий код к размещению на VPS/Beget и помочь владельцу пройти деплой по шагам.

## 1. Главное правило

Сначала изучи весь проект и его конфигурацию. Не удаляй и не пересоздавай production database. Не меняй схему голосов и не обнуляй реальные клики без явного разрешения владельца.

Перед изменениями:
1. Сделай backup БД.
2. Зафиксируй текущий Git commit.
3. Проверь переменные окружения.
4. Запусти все локальные тесты.
5. После каждой серьёзной правки снова запусти тесты.

## 2. Архитектура

Проект использует:
- FastAPI + Uvicorn
- SQLAlchemy
- PostgreSQL в production
- SQLite только для локального теста
- Redis для общего rate limiting при нескольких workers
- WebSocket для live leaderboard
- Cloudflare Turnstile для входа на сайт
- Nginx/reverse proxy перед приложением

Публичный интерфейс должен оставаться свободным после прохождения Cloudflare Turnstile. Админская зона отдельная. Прямой URL `/app` не должен позволять обойти gate: без валидной подписанной сессии должен быть редирект на `/`.

## 3. Данные

В проекте уже есть seed `data/institutions.csv`. Не удаляй его. При первом запуске пустой БД проект сам импортирует учреждения.

Публично сейчас активна только Россия через:
`PUBLIC_COUNTRIES=RU`

Архитектуру новых стран не ломай: позже владелец должен иметь возможность добавить страны CSV-импортом.

## 4. Что нужно проверить перед хостингом

Проверь:
- старт FastAPI;
- `GET /`;
- `POST /api/enter`;
- `GET /api/stats`;
- города и autocomplete;
- выбор типа учреждения;
- поиск учреждения;
- live WebSocket;
- автоматическое изменение рейтинга без перезагрузки;
- клики и clicks/sec;
- фото submit/moderation;
- admin event +N/-N + audio loop;
- удаление накрученных голосов;
- admin login;
- поддержка Donation Alerts;
- русский/английский интерфейс;
- мобильную верстку.

## 5. PostgreSQL

Подними отдельную БД для nationwide версии. Подключи `DATABASE_URL` в формате PostgreSQL, а SQLAlchemy должен использовать psycopg3 через `postgresql+psycopg://`.

Проверь `Base.metadata.create_all()` и импорт seed на чистую БД.

## 6. Redis

Подними Redis и передай:
`REDIS_URL=redis://...`

Убедись, что rate limiter использует Redis atomically и один клик нельзя одновременно принять двумя process workers.

Если Redis временно недоступен, приложение не должно внезапно принять неограниченный поток голосов. Должен применяться строгий fallback или временное отклонение запросов.

## 7. Защита голосов

Нельзя строить защиту на cookie. Проверка должна происходить на сервере.

Сохрани и проверь уровни:
- 800 кликов/сек на одну точку;
- 1500 кликов/сек суммарно на IP;
- подпись пользовательской сессии + привязка к IP/User-Agent;
- max batch 60;
- `MAX_BATCH_CLICKS` должен оставаться 60 и не должен быть поднят до 1500 (1500 — это лимит количества принятых кликов в секунду, а не размер одного HTTP batch);
- Cloudflare Turnstile перед выдачей сессии;
- Cloudflare WAF/rate limiting перед FastAPI.

Важно: не обещай владельцу «абсолютно невозможную накрутку». Для распределённых бот-сетей нужен edge уровень Cloudflare.

## 8. Cloudflare

Подключи домен через Cloudflare и включи proxy.

Используй реальные production Turnstile Site Key + Secret Key.

Создай rate limiting/WAF правила для:
- `/api/clicks`
- `/api/photo-submissions`
- hidden admin login path (`ADMIN_LOGIN_PATH`)
- при необходимости `/api/enter`

После подключения Cloudflare проверь, что приложение корректно получает реальный клиентский IP через `CF-Connecting-IP` только когда `TRUST_PROXY=true`. На Render/Beget не включай доверие к произвольному пользовательскому заголовку: `CF-Connecting-IP` должен доверяться только потому, что трафик приходит от настроенного reverse proxy/Cloudflare.

## 9. Домен

После того как VPS отвечает по IP:
1. Привяжи A/AAAA запись домена к VPS.
2. В Cloudflare поставь proxy.
3. Настрой HTTPS.
4. Проверь WebSocket по WSS.
5. Проверь upload фото и audio через HTTPS.

## 10. Nginx

Nginx должен:
- проксировать HTTP к Uvicorn;
- поддерживать WebSocket Upgrade;
- ограничивать размер upload;
- отдавать статические файлы с кэшированием;
- не кэшировать `/api/clicks`, `/api/enter`, `/api/stats` и live endpoints;
- передавать `Host`, `X-Forwarded-For` и `X-Forwarded-Proto`;
- не принимать бесконечные upload/request body.

## 11. Workers

Сначала запусти один worker.

Когда Redis rate limiting проверен, можно увеличить workers. Не запускай много workers без Redis, иначе лимиты будут раздельными и накрутчик сможет умножить допустимую скорость на число процессов.

## 12. Фотографии

Проверь, что конечные изображения не смешиваются между школами.
Структура должна быть привязана к `institution_id`, например:
`media/institutions/<institution_id>/desktop.jpg`
`media/institutions/<institution_id>/mobile.jpg`

В production filesystem media должен быть на persistent volume или в object storage.

## 13. Админка

Админка доступна через скрытый переход `Anton Ljungberg Production → Ljungberg`.

Не добавляй обратно заметную кнопку «Админка» на публичную страницу.

Проверь:
- Cloudflare Turnstile на admin login;
- rate limit попыток входа;
- environment variables вместо пароля в Git;
- moderation queue;
- download source photo;
- desktop/mobile upload;
- reject;
- admin event;
- remove suspicious real votes;
- audit log.

## 14. Что сделать владельцу перед public launch

Попроси владельца самостоятельно задать реальные:
- `SECRET_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `TURNSTILE_SITE_KEY`
- `TURNSTILE_SECRET_KEY`
- `DONATION_URL`
- `DATABASE_URL`
- `REDIS_URL`

Не вставляй эти значения в GitHub.

## 15. Производительность

Не создавай отдельный SQL-запрос статистики для каждого WebSocket клиента: текущий код группирует подписки и считает агрегированную статистику один раз на broadcast. Не добавляй таблицу с каждой отдельной записью клика без явного разрешения владельца — это ухудшит производительность.

## 16. Результат

К концу работы должен быть:
1. работающий VPS;
2. PostgreSQL + Redis;
3. HTTPS и Cloudflare;
4. WebSocket live;
5. persistent uploads;
6. backup/restore procedure;
7. documented `.env` values;
8. проверенный mobile + desktop UI;
9. smoke-test после деплоя;
10. инструкция, как безопасно обновлять проект без потери БД.

При любой неоднозначности сначала проверяй текущий код и конфигурацию проекта, а не придумывай новую архитектуру.
