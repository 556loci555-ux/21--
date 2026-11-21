import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "players.db"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

MAX_INIT_DATA_AGE = 3600  # Данные входа действуют 1 час

app = FastAPI(title="Игра 21")


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                username TEXT,
                created_at INTEGER NOT NULL
            )
        """)


init_db()


class LoginRequest(BaseModel):
    init_data: str


def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data or len(init_data) > 16_384:
        raise HTTPException(
            status_code=401,
            detail="Отсутствуют или некорректны данные Telegram",
        )

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="Некорректный формат данных Telegram",
        )

    data = dict(pairs)

    # Не принимаем повторяющиеся параметры.
    if len(data) != len(pairs):
        raise HTTPException(
            status_code=401,
            detail="Повторяющиеся параметры Telegram",
        )

    received_hash = data.pop("hash", None)

    if (
        not received_hash
        or len(received_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in received_hash)
    ):
        raise HTTPException(
            status_code=401,
            detail="Отсутствует подпись Telegram",
        )

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash.lower()):
        raise HTTPException(
            status_code=401,
            detail="Неверная подпись Telegram",
        )

    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=401,
            detail="Некорректные данные пользователя Telegram",
        )

    now = int(time.time())

    if auth_date > now + 60 or now - auth_date > MAX_INIT_DATA_AGE:
        raise HTTPException(
            status_code=401,
            detail="Данные входа устарели",
        )

    if (
        not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or not isinstance(user.get("first_name"), str)
        or not user["first_name"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Некорректный профиль Telegram",
        )

    return user


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/login")
def login(body: LoginRequest):
    user = verify_telegram_init_data(body.init_data)

    telegram_id = user["id"]
    first_name = user["first_name"]
    username = user.get("username")

    if not isinstance(username, (str, type(None))):
        username = None

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO players (
                telegram_id,
                first_name,
                username,
                created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username
            """,
            (
                telegram_id,
                first_name,
                username,
                int(time.time()),
            ),
        )

        player = db.execute(
            """
            SELECT telegram_id, first_name, username, created_at
            FROM players
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

    return {
        "telegram_id": player[0],
        "first_name": player[1],
        "username": player[2],
        "created_at": player[3],
    }
