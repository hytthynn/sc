# 🎵 SoundCloud Telegram Bot

Telegram-бот на **aiogram 3.x**, который скачивает лайкнутые треки с SoundCloud и отправляет их прямо в чат.

---

## 📋 Что умеет бот

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и список команд |
| `/help` | Подробная инструкция |
| `/settoken` | Сохранить OAuth-токен SoundCloud |
| `/likes` | Получить последний лайкнутый трек |
| `/track <url>` | Скачать трек по ссылке |
| *(просто вставить ссылку)* | Автоматически скачает трек |

---

## 🚀 Установка

### 1. Клонируй или скачай проект

```bash
git clone <url-репозитория>
cd soundcloud_bot
```

### 2. Создай виртуальное окружение и установи зависимости

```bash
python -m venv venv
source venv/bin/activate        # Linux / macOS
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### 3. Создай бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather)
2. Отправь `/newbot`
3. Придумай имя и username для бота
4. Скопируй полученный **токен**

### 4. Настрой переменные окружения

```bash
cp .env.example .env
# Открой .env и вставь свой BOT_TOKEN
```

Либо передай токен напрямую в переменную окружения:

```bash
export BOT_TOKEN="1234567890:AAxxxxxxxxxxxxxx"
```

### 5. Запусти бота

```bash
python bot.py
```

---

## 🔑 Как получить OAuth-токен SoundCloud

Токен нужен, чтобы бот знал, **чьи лайки** скачивать.

### Способ 1 — через Network (рекомендуется)

1. Открой [soundcloud.com](https://soundcloud.com) (войди в аккаунт)
2. Нажми **F12** → вкладка **Network**
3. В строке фильтра введи `api-v2`
4. Кликни на любой запрос к `api-v2.soundcloud.com`
5. Перейди в **Request Headers**
6. Найди заголовок `Authorization: OAuth XXXXX...`
7. Скопируй всё **после** `OAuth ` (сам токен, без слова OAuth)

### Способ 2 — через Cookies

1. Открой [soundcloud.com](https://soundcloud.com) (войди в аккаунт)
2. Нажми **F12** → вкладка **Application / Storage**
3. Слева: **Cookies → https://soundcloud.com**
4. Найди строку `oauth_token`
5. Скопируй значение из колонки **Value**

---

## 🔒 Безопасность

- OAuth-токен **удаляется из чата** сразу после получения
- Токены хранятся **только в памяти** (сбрасываются при перезапуске)
- Для продакшена используй Redis или базу данных для хранения токенов

---

## ⚠️ Ограничения

- Треки крупных лейблов (Sony/Universal/Warner) **нельзя скачать** — они защищены DRM
- Приватные треки скачиваются только если твой аккаунт имеет доступ
- Размер файла в Telegram ограничен **50 МБ** (через Bot API)
- SoundCloud иногда меняет API — если что-то сломалось, обнови `scdl`: `pip install -U scdl`

---

## 📦 Зависимости

- [aiogram](https://docs.aiogram.dev/) 3.x — фреймворк для Telegram-ботов
- [scdl](https://github.com/scdl-org/scdl) — загрузчик треков с SoundCloud

---

## 🐳 Docker (опционально)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
```

```bash
docker build -t sc-bot .
docker run -e BOT_TOKEN=ваш_токен sc-bot
```
