#!/bin/bash

# Пути к локальной и серверной версиям
LOCAL_DIR="~/projects/telegram_bot"
SERVER_DIR="/var/www/telegram_bot"

# Функция для синхронизации с сервера на локальную машину
sync_from_server() {
    echo "Синхронизация с сервера на локальную машину..."
    rsync -avz --exclude '.DS_Store' selectel:$SERVER_DIR/ $LOCAL_DIR/
}

# Функция для синхронизации с локальной машины на сервер
sync_to_server() {
    echo "Синхронизация с локальной машины на сервер..."
    rsync -avz --exclude '.DS_Store' $LOCAL_DIR/ selectel:$SERVER_DIR/
}

# Функция для перезапуска бота на сервере
restart_bot() {
    echo "Перезапуск бота на сервере..."
    ssh selectel "cd $SERVER_DIR && \
        if [ -f bot.pid ]; then \
            kill \$(cat bot.pid) 2>/dev/null || true; \
            rm bot.pid; \
        fi; \
        nohup python3 bot.py > bot.log 2>&1 & \
        echo \$! > bot.pid"
    echo "Бот перезапущен. Проверьте логи: ssh selectel 'tail -f $SERVER_DIR/bot.log'"
}

# Проверка аргументов
if [ "$1" == "from" ]; then
    sync_from_server
elif [ "$1" == "to" ]; then
    sync_to_server
elif [ "$1" == "restart" ]; then
    restart_bot
elif [ "$1" == "deploy" ]; then
    sync_to_server
    restart_bot
else
    echo "Использование: ./sync.sh [from|to|restart|deploy]"
    echo "  from    - синхронизировать с сервера на локальную машину"
    echo "  to      - синхронизировать с локальной машины на сервер"
    echo "  restart - перезапустить бота на сервере"
    echo "  deploy  - синхронизировать и перезапустить бота"
fi 