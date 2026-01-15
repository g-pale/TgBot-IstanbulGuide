#!/bin/bash

# Пути к локальной и серверной версиям
# ВАЖНО: используем абсолютный путь, т.к. rsync не расширяет ~ сам
LOCAL_DIR="/Users/sergejsaburkin/projects/telegram_bot"
SERVER_DIR="/var/www/telegram_bot"

# Функция для синхронизации с сервера на локальную машину
sync_from_server() {
    echo "Синхронизация с сервера на локальную машину..."
    rsync -avz --exclude '.DS_Store' --exclude '.git' --exclude 'venv' --exclude '.env' --exclude '*.log' --exclude 'bot.pid' selectel:$SERVER_DIR/ $LOCAL_DIR/
}

# Функция для синхронизации с локальной машины на сервер
sync_to_server() {
    echo "Синхронизация с локальной машины на сервер..."
    rsync -avz --exclude '.DS_Store' --exclude '.git' --exclude 'venv' --exclude '.env' --exclude '*.log' --exclude 'bot.pid' $LOCAL_DIR/ selectel:$SERVER_DIR/
    
    # Автоматическая установка/обновление зависимостей на сервере
    echo "Проверка и установка зависимостей на сервере..."
    ssh selectel "cd $SERVER_DIR && \
        if [ -d venv ]; then \
            venv/bin/pip install -q --upgrade pip && \
            venv/bin/pip install -q -r requirements.txt; \
            echo 'Зависимости обновлены.'; \
        else \
            echo 'ВНИМАНИЕ: venv не найден на сервере. Создайте его вручную.'; \
        fi"
}

# Функция для очистки ненужных файлов на сервере
cleanup_server() {
    echo "Очистка ненужных файлов на сервере..."
    echo "Работаем только в директории: $SERVER_DIR"
    ssh selectel "cd $SERVER_DIR && \
        pwd && \
        if [ \$(pwd) != '$SERVER_DIR' ]; then \
            echo 'ОШИБКА: Не удалось перейти в нужную директорию!'; \
            exit 1; \
        fi && \
        if [ -d .git ]; then \
            echo 'Удаление .git директории из $SERVER_DIR...'; \
            rm -rf .git; \
            echo '.git удалена.'; \
        else \
            echo '.git не найдена (уже удалена или не была скопирована).'; \
        fi && \
        echo 'Очистка завершена. Другие проекты не затронуты.'"
}

# Функция для перезапуска бота на сервере
restart_bot() {
    echo "Перезапуск бота на сервере..."
    ssh selectel "cd $SERVER_DIR && \
        if [ -f bot.pid ]; then \
            kill \$(cat bot.pid) 2>/dev/null || true; \
            rm bot.pid; \
        fi; \
        # Определяем путь к Python в venv (python3 или python)
        PYTHON_BIN=\$(ls venv/bin/python* 2>/dev/null | head -1); \
        if [ -z \"\$PYTHON_BIN\" ]; then \
            echo 'ОШИБКА: Python не найден в venv/bin/'; \
            exit 1; \
        fi; \
        nohup \$PYTHON_BIN bot.py > bot.log 2>&1 & \
        echo \$! > bot.pid && \
        echo \"Бот запущен с \$PYTHON_BIN (PID: \$(cat bot.pid))\""
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
elif [ "$1" == "cleanup" ]; then
    cleanup_server
else
    echo "Использование: ./sync.sh [from|to|restart|deploy|cleanup]"
    echo "  from    - синхронизировать с сервера на локальную машину"
    echo "  to      - синхронизировать с локальной машины на сервер (автоматически обновляет зависимости)"
    echo "  restart - перезапустить бота на сервере"
    echo "  deploy  - синхронизировать, обновить зависимости и перезапустить бота"
    echo "  cleanup - удалить ненужные файлы (.git и т.д.) с сервера"
fi 