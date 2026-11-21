import hashlib
import hmac
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "players.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

app = FastAPI(title="Игра 21")

# Первая версия: очередь и партии хранятся в памяти одного процесса.
lock = threading.Lock()
waiting = {}       # telegram_id -> время начала поиска
last_seen = {}     # telegram_id -> время последнего запроса
games = {}         # game_id -> состояние партии
player_games = {}  # telegram_id -> game_id


class LoginRequest(BaseModel):
    init_data: str


class PlayRequest(BaseModel):
    init_data: str
    action: str


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


def telegram_user(init_data: str) -> dict:
    if not init_data or len(init_data) > 16384:
        raise HTTPException(401, "Нет данных Telegram")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        raise HTTPException(401, "Неверный формат данных Telegram")

    data = dict(pairs)

    if len(data) != len(pairs):
        raise HTTPException(401, "Повторяющиеся параметры Telegram")

    received_hash = data.pop("hash", None)

    if (
        not received_hash
        or len(received_hash) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in received_hash)
    ):
        raise HTTPException(401, "Нет подписи Telegram")

    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(data.items())
    )

    secret = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    expected_hash = hmac.new(
        secret,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise HTTPException(401, "Неверная подпись Telegram")

    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Неверные данные пользователя")

    now = int(time.time())

    if auth_date > now + 60 or now - auth_date > 3600:
        raise HTTPException(401, "Данные входа устарели. Открой игру заново")

    if (
        not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or not isinstance(user.get("first_name"), str)
        or not user["first_name"]
    ):
        raise HTTPException(401, "Неверный профиль Telegram")

    return user


def make_deck():
    suits = ["♠", "♥", "♦", "♣"]
    ranks = [
        ("6", 6), ("7", 7), ("8", 8), ("9", 9),
        ("10", 10), ("В", 2), ("Д", 3),
        ("К", 4), ("Т", 11),
    ]

    deck = [
        {"label": f"{rank}{suit}", "points": points}
        for suit in suits
        for rank, points in ranks
    ]
    random.SystemRandom().shuffle(deck)
    return deck


def points(cards):
    return sum(card["points"] for card in cards)


def new_game(player_ids, names, bot=False):
    game_id = uuid.uuid4().hex
    deck = make_deck()

    game = {
        "players": player_ids,
        "names": names,
        "bot": bot,
        "deck": deck,
        "hands": {pid: [deck.pop(), deck.pop()] for pid in player_ids},
        "finished": {pid: False for pid in player_ids},
        "result": False,
    }

    games[game_id] = game

    for pid in player_ids:
        player_games[pid] = game_id
        waiting.pop(pid, None)

    return game


def game_state(player_id):
    game_id = player_games.get(player_id)

    if not game_id or game_id not in games:
        return {"status": "idle"}

    game = games[game_id]
    opponent_id = next(
        pid for pid in game["players"] if pid != player_id
    )

    my_cards = game["hands"][player_id]
    opponent_cards = game["hands"][opponent_id]

    response = {
        "status": "result" if game["result"] else "playing",
        "opponent_name": game["names"][opponent_id],
        "is_bot": game["bot"],
        "my_cards": [card["label"] for card in my_cards],
        "my_points": points(my_cards),
        "my_finished": game["finished"][player_id],
        "opponent_finished": game["finished"][opponent_id],
    }

    if game["result"]:
        my_score = points(my_cards)
        opponent_score = points(opponent_cards)

        if my_score > 21:
            winner = "opponent"
        elif opponent_score > 21:
            winner = "me"
        elif my_score > opponent_score:
            winner = "me"
        elif opponent_score > my_score:
            winner = "opponent"
        else:
            winner = "draw"

        response.update({
            "opponent_cards": [
                card["label"] for card in opponent_cards
            ],
            "opponent_points": opponent_score,
            "winner": winner,
        })

    return response


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/login")
def login(body: LoginRequest):
    user = telegram_user(body.init_data)

    username = user.get("username")
    if not isinstance(username, (str, type(None))):
        username = None

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO players
                (telegram_id, first_name, username, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username
            """,
            (
                user["id"],
                user["first_name"],
                username,
                int(time.time()),
            ),
        )

    return {
        "telegram_id": user["id"],
        "first_name": user["first_name"],
    }


@app.post("/api/search")
def search(body: LoginRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]
    now = time.monotonic()

    with lock:
        last_seen[player_id] = now

        # Если старая партия закончилась — разрешаем искать новую.
        old_game_id = player_games.get(player_id)
        if old_game_id:
            old_game = games.get(old_game_id)
            if old_game and not old_game["result"]:
                return game_state(player_id)
            player_games.pop(player_id, None)

        if player_id in waiting:
            return {
                "status": "waiting",
                "seconds_left": max(
                    0, 30 - int(now - waiting[player_id])
                ),
            }

        # Не подбираем игрока, который давно закрыл приложение.
        for other_id in list(waiting):
            if now - last_seen.get(other_id, 0) > 10:
                waiting.pop(other_id, None)
                last_seen.pop(other_id, None)

        if waiting:
            other_id = next(iter(waiting))

            with sqlite3.connect(DB_PATH) as db:
                row = db.execute(
                    "SELECT first_name FROM players WHERE telegram_id = ?",
                    (other_id,),
                ).fetchone()

            if row:
                new_game(
                    [other_id, player_id],
                    {
                        other_id: row[0],
                        player_id: user["first_name"],
                    },
                )
                return game_state(player_id)

            waiting.pop(other_id, None)

        waiting[player_id] = now
        return {"status": "waiting", "seconds_left": 30}


@app.post("/api/state")
def state(body: LoginRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]
    now = time.monotonic()

    with lock:
        last_seen[player_id] = now

        if player_id in player_games:
            return game_state(player_id)

        started = waiting.get(player_id)

        if started is None:
            return {"status": "idle"}

        elapsed = now - started

        if elapsed >= 30:
            new_game(
                [player_id, "bot"],
                {
                    player_id: user["first_name"],
                    "bot": "Бот",
                },
                bot=True,
            )
            return game_state(player_id)

        return {
            "status": "waiting",
            "seconds_left": max(0, 30 - int(elapsed)),
        }


@app.post("/api/cancel")
def cancel(body: LoginRequest):
    user = telegram_user(body.init_data)

    with lock:
        waiting.pop(user["id"], None)
        last_seen.pop(user["id"], None)

    return {"status": "idle"}


@app.post("/api/play")
def play(body: PlayRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]

    if body.action not in ("draw", "stand"):
        raise HTTPException(400, "Неизвестное действие")

    with lock:
        game_id = player_games.get(player_id)
        game = games.get(game_id) if game_id else None

        if not game or game["result"]:
            raise HTTPException(400, "Активная партия не найдена")

        if game["finished"][player_id]:
            raise HTTPException(400, "Ты уже закончил ход")

        if body.action == "draw":
            game["hands"][player_id].append(game["deck"].pop())

            if points(game["hands"][player_id]) >= 21:
                game["finished"][player_id] = True
        else:
            game["finished"][player_id] = True

        if game["bot"] and game["finished"][player_id]:
            # Бот добирает карты до 17 очков.
            while points(game["hands"]["bot"]) < 17:
                game["hands"]["bot"].append(game["deck"].pop())

            game["finished"]["bot"] = True

        if all(game["finished"].values()):
            game["result"] = True

        return game_state(player_id)
