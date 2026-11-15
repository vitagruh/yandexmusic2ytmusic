# Яндекс.Музыка → YouTube Music

Локальный CLI-переносчик: читает плейлист Яндекс.Музыки, ищет каждый трек в
YouTube Music, создаёт новый плейлист и складывает отчёт о спорных совпадениях
в CSV.

## Установка

Нужен Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 1. Токен Яндекс.Музыки

1. Откройте <https://music.yandex.ru> и войдите в аккаунт.
2. Откройте DevTools → Network, выберите фильтр Fetch/XHR и обновите страницу.
3. Откройте любой запрос к `api.music.yandex.*` → Request Headers.
4. Скопируйте значение заголовка `Authorization` и уберите префикс `OAuth `.

Токен передаётся через переменную окружения (предпочтительно) или флаг
`--yandex-token`:

```bash
export YANDEX_MUSIC_TOKEN="ваш_токен"
```

Токен нужен для приватных плейлистов и большинства запросов. Не коммитьте его
в репозиторий.

## 2. Авторизация YouTube Music

```bash
ytmusicapi browser
```

Утилита попросит скопировать заголовки запроса из DevTools на
<https://music.youtube.com> и сохранит их в `browser.json`. Альтернатива —
`ytmusicapi oauth` (потребуется свой OAuth-клиент Google); результат также
подходит для `--ytm-auth`.

Подробнее: <https://ytmusicapi.readthedocs.io/en/stable/setup/authentication.html>

## Запуск

Пробный прогон без создания плейлиста (только поиск и отчёт):

```bash
python3 yandex_to_ytmusic.py \
  --yandex-playlist "https://music.yandex.ru/users/username/playlists/3" \
  --max-tracks 5 \
  --dry-run
```

Полный перенос:

```bash
python3 yandex_to_ytmusic.py \
  --yandex-playlist "https://music.yandex.ru/users/username/playlists/3" \
  --ytm-auth browser.json \
  --name "Мой плейлист" \
  --privacy PRIVATE
```

Поддерживаются ссылки:

- `https://music.yandex.ru/users/<user>/playlists/<kind>`
- `https://music.yandex.ru/playlists/<uuid>`
- `owner:kind` (например, `ivan:3`)
- просто `kind` — для плейлиста своего аккаунта

## Флаги

| Флаг | Значение |
| --- | --- |
| `--yandex-playlist` | ссылка/идентификатор плейлиста (обязательно) |
| `--yandex-token` | токен Яндекса, если не задан `YANDEX_MUSIC_TOKEN` |
| `--ytm-auth` | `browser.json` или `oauth.json`; обязателен без `--dry-run` |
| `--name`, `--description` | название и описание нового плейлиста |
| `--privacy` | `PRIVATE` (по умолчанию), `UNLISTED`, `PUBLIC` |
| `--min-score` | порог совпадения, по умолчанию `0.72` |
| `--search-limit` | сколько результатов запрашивать, по умолчанию `5` |
| `--allow-videos` | искать в видео, если среди песен нет уверенного совпадения |
| `--skip-ambiguous` | не добавлять неоднозначные совпадения |
| `--max-tracks N` | обработать только первые N треков |
| `--dry-run` | не создавать плейлист, только отчёт |
| `--out DIR` | каталог отчёта, по умолчанию `out` |
| `--delay` | пауза между запросами, по умолчанию `0.3` с |
| `--quiet` | не печатать каждый трек |
| `--debug` | показывать traceback |

## Отчёт

`out/report.csv` записывается в UTF-8 с BOM (открывается в Excel) и содержит
все треки исходного плейлиста со статусами:

| Статус | Что значит |
| --- | --- |
| `matched` | уверенное совпадение, трек добавлен |
| `ambiguous` | похожие результаты; по умолчанию добавлен лучший, проверьте вручную |
| `not_found` | подходящий трек не найден или оценка ниже порога |
| `unavailable` | трек недоступен в Яндекс.Музыке |
| `error` | ошибка поиска/добавления, причина в колонке `note` |
| `duplicate` | тот же `video_id` уже занят другой позицией |

Колонки: `position, status, yandex_title, yandex_artists, yandex_duration,
youtube_title, youtube_artists, youtube_duration, video_id, score, note`.

## Как работает сопоставление

- Из названий убираются служебные пометки: `feat.`, `prod.`, `Remastered`,
  `Live`, `Radio Edit`, `Official Video` и т. п.
- Сравниваются нормализованные название и исполнители, дополнительно —
  длительность (если YouTube её отдал).
- Итоговая оценка: 0.55 × название + 0.30 × исполнитель + 0.15 × длительность
  (без длительности веса перераспределяются).
- Повторы `video_id` добавляются в плейлист один раз.

## Ограничения

- Обе библиотеки используют неофициальные API и могут ломаться при изменениях
  на стороне сервисов. Соблюдайте условия использования сервисов.
- Заголовки `browser.json` со временем истекают; тогда выполните
  `ytmusicapi browser` заново.
- Часть треков недоступна в регионе или отсутствует в YouTube Music — они
  останутся в отчёте со статусом `not_found`/`error`.
- Автоматическое сопоставление не идеально: проверяйте `ambiguous` и
  `not_found`, особенно для каверов, ремиксов и одноимённых песен.
- При большом плейлисте возможны ограничения частоты запросов; увеличьте
  `--delay` и перезапустите.
