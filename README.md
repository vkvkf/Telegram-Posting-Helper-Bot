# Telegram Posting Helper Bot

Telegram-бот для подготовки и постинга сообщений в каналы: текст (HTML), фото, веб-кнопки, шаблоны, роли OWNER/Админы, экспорт/импорт шаблонов. Построен на **Aiogram 3**.

## ✨ Возможности
- Создание постов: текст, фото, **многорядные веб-кнопки**.
- Отправка в привязанный канал (бот должен быть админом канала).
- Шаблоны (готовые посты): игра → чит → название.
- Управление шаблонами: добавление, удаление, список.
- Экспорт/импорт шаблонов (JSON).
- Роли: **OWNER** и админы. Панель владельца.
- Подключение канала: переслать пост или указать **@username**.
- `storage.json` создаётся автоматически (можно задать `DATA_DIR`).

## 📦 Требования
- Python **3.12+**
- `aiogram==3.13.1`, `python-dotenv`

## 🗂 Структура
├─ bot.py
├─ requirements.txt
├─ .env.example
├─ .gitignore
└─ data/ # создаётся при запуске (если указан DATA_DIR)

## ⚙️ Установка локально
```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# отредактируй .env:
# BOT_TOKEN=токен от BotFather
# OWNER_ID=твой user_id (цифры)
# ADMIN_IDS= (опционально через запятую)
# DATA_DIR=./data

python bot.py

🔐 Переменные окружения (.env)
BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
OWNER_ID=000000000
ADMIN_IDS=
DATA_DIR=./data

🚀 Деплой на Ubuntu (systemd)
sudo apt update && sudo apt install -y python3.12 python3.12-venv git
git clone https://github.com/vkvkf/Telegram-Posting-Helper-Bot.git /root/telegrambot
cd /root/telegrambot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
mkdir -p data

Создай сервис /etc/systemd/system/telegrambot.service:
[Unit]
Description=Telegram Channel Poster Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/telegrambot
ExecStart=/root/telegrambot/.venv/bin/python /root/telegrambot/bot.py
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target

Запуск и логи:
sudo systemctl daemon-reload
sudo systemctl enable telegrambot
sudo systemctl start telegrambot
sudo systemctl status telegrambot
journalctl -u telegrambot -f   # выйти: Ctrl+C

🔁 Обновления на сервере
cd /root/telegrambot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegrambot

🧰 Частые проблемы
Бот не постит в канал — добавь бота админом канала.
Нет storage.json — создастся автоматически при первом запуске (или в папке DATA_DIR).
message is not modified — учтено в коде через безопасный edit_text.
Windows PowerShell ругается на скрипты — Set-ExecutionPolicy -Scope CurrentUser RemoteSigned.

📝 Лицензия
Этот проект распространяется под лицензией MIT License.
