@echo off
title Polymarket Arb Bot
chcp 65001 >nul
cd /d "%~dp0"

:: ── Проверка Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python не найден!
    echo  Установи Python с https://python.org
    echo  При установке отметь галочку "Add to PATH"
    echo.
    pause
    exit /b 1
)

:: ── Установка зависимостей ───────────────────────────────────────────────────
echo  Проверка зависимостей...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo  ERROR: Не удалось установить зависимости.
    echo  Попробуй запустить от имени администратора.
    echo.
    pause
    exit /b 1
)

:: ── Запуск ───────────────────────────────────────────────────────────────────
echo  Открываем меню...
echo.
python launcher.py

:: ── После выхода ─────────────────────────────────────────────────────────────
echo.
pause
