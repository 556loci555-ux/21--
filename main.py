import hashlib
import hmac
import json
import math
import os
import secrets
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

START_BALANCE = 100
ALLOWED_STAKES = {10, 20, 50}
SEARCH_SECONDS = 21

app = FastAPI(title="Игра 21")

# Прототип для одного процесса Render.
lock = threading.Lock()
waiting = {}       # id -> {started, seen, stake, name}
games = {}         # game_id -> игра
player_games = {}  # id -> game_id


class LoginRequest(BaseModel):
    init_data: str


class SearchRequest(LoginRequest):
    stake: int


class PlayRequest(LoginRequest):
    action: str


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                username TEXT,
                created_at INTEGER NOT NULL,
                balance INTEGER NOT NULL DEFAULT 100
            )
        """)

        # Обновление базы, если игроки уже были зарегистрированы
        # предыдущей версией приложения.
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(players)")
        }
        if "balance" not in columns:
            db.execute(
                "ALTER TABLE players "
                "ADD COLUMN balance INTEGER NOT NULL DEFAULT 100"
            )


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
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    expected = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash.lower()):
        raise HTTPException(401, "Неверная подпись Telegram")

    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Неверные данные пользователя")

    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > 3600:
        raise HTTPException(
            401,
            "Данные входа устарели. Закрой и открой игру заново",
        )

    if (
        not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or not isinstance(user.get("first_name"), str)
        or not user["first_name"]
    ):
        raise HTTPException(401, "Неверный профиль Telegram")

    return user


def ensure_player(db, user):
    username = user.get("username")
    if not isinstance(username, (str, type(None))):
        username = None

    db.execute(
        """
        INSERT INTO players
            (telegram_id, first_name, username, created_at, balance)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            first_name = excluded.first_name,
            username = excluded.username
        """,
        (
            user["id"],
            user["first_name"],
            username,
            int(time.time()),
            START_BALANCE,
        ),
    )


def balance_of(db, player_id):
    row = db.execute(
        "SELECT balance FROM players WHERE telegram_id = ?",
        (player_id,),
    ).fetchone()
    return row[0] if row else 0


def reserve(db, player_id, stake):
    result = db.execute(
        """
        UPDATE players
        SET balance = balance - ?
        WHERE telegram_id = ? AND balance >= ?
        """,
        (stake, player_id, stake),
    )
    if result.rowcount != 1:
        raise HTTPException(400, "Недостаточно фишек для ставки")


def refund(db, player_id, stake):
    db.execute(
        "UPDATE players SET balance = balance + ? "
        "WHERE telegram_id = ?",
        (stake, player_id),
    )


def make_deck():
    suits = ["♠", "♥", "♦", "♣"]
    ranks = [
        ("6", 6), ("7", 7), ("8", 8), ("9", 9),
        ("10", 10), ("В", 2), ("Д", 3),
        ("К", 4), ("Т", 11),
    ]
    deck = [
        {"label": rank + suit, "points": value}
        for suit in suits
        for rank, value in ranks
    ]
    secrets.SystemRandom().shuffle(deck)
    return deck


def score(cards):
    return sum(card["points"] for card in cards)


def winner_of(game):
    first, second = game["players"]
    a = score(game["hands"][first])
    b = score(game["hands"][second])

    if a > 21 and b > 21:
        return None
    if a > 21:
        return second
    if b > 21:
        return first
    if a > b:
        return first
    if b > a:
        return second
    return None


def finish_game(db, game):
    # Вызывать только после завершения ходов, под lock.
    if game["result"]:
        return

    game["winner"] = winner_of(game)
    game["result"] = True
    first, second = game["players"]
    stake = game["stake"]

    if game["winner"] is None:
        for pid in (first, second):
            if pid != "bot":
                refund(db, pid, stake)
    elif game["winner"] != "bot":
        # Победитель получает свою ставку обратно
        # и столько же фишек выигрыша.
        refund(db, game["winner"], stake * 2)


def create_game(player_ids, names, stake, bot=False):
    deck = make_deck()
    game_id = uuid.uuid4().hex

    game = {
        "players": player_ids,
        "names": names,
        "stake": stake,
        "bot": bot,
        "deck": deck,
        "hands": {
            pid: [deck.pop(), deck.pop()] for pid in player_ids
        },
        "finished": {pid: False for pid in player_ids},
        "result": False,
        "winner": None,
    }

    games[game_id] = game
    for pid in player_ids:
        if pid != "bot":
            player_games[pid] = game_id
            waiting.pop(pid, None)

    return game


def game_state(db, player_id):
    game_id = player_games.get(player_id)
    game = games.get(game_id) if game_id else None

    if not game:
        return {
            "status": "idle",
            "balance": balance_of(db, player_id),
        }

    opponent_id = next(
        pid for pid in game["players"] if pid != player_id
    )
    mine = game["hands"][player_id]
    theirs = game["hands"][opponent_id]

    state = {
        "status": "result" if game["result"] else "playing",
        "balance": balance_of(db, player_id),
        "stake": game["stake"],
        "opponent_name": game["names"][opponent_id],
        "is_bot": game["bot"],
        "my_cards": [card["label"] for card in mine],
        "my_points": score(mine),
        "my_finished": game["finished"][player_id],
        "opponent_finished": game["finished"][opponent_id],
    }

    if game["result"]:
        state.update({
            "opponent_cards": [card["label"] for card in theirs],
            "opponent_points": score(theirs),
            "winner": (
                "draw" if game["winner"] is None
                else "me" if game["winner"] == player_id
                else "opponent"
            ),
        })

    return state


def waiting_state(db, player_id, now):
    entry = waiting[player_id]
    left = max(0, math.ceil(
        SEARCH_SECONDS - (now - entry["started"])
    ))
    return {
        "status": "waiting",
        "seconds_left": left,
        "stake": entry["stake"],
        "balance": balance_of(db, player_id),
    }


def clean_inactive_waiters(db, now, except_id=None):
    # Игрок, закрывший приложение, не должен вечно занимать очередь
    # или терять зарезервированную ставку.
    for pid, entry in list(waiting.items()):
        if pid != except_id and now - entry["seen"] > 15:
            refund(db, pid, entry["stake"])
            waiting.pop(pid, None)


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/login")
def login(body: LoginRequest):
    user = telegram_user(body.init_data)
    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        return {
            "telegram_id": user["id"],
            "first_name": user["first_name"],
            "balance": balance_of(db, user["id"]),
        }


@app.post("/api/search")
def search(body: SearchRequest):
    user = telegram_user(body.init_data)
    pid = user["id"]

    if type(body.stake) is not int or body.stake not in ALLOWED_STAKES:
        raise HTTPException(400, "Допустимые ставки: 10, 20 или 50")

    now = time.monotonic()

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        clean_inactive_waiters(db, now, except_id=pid)

        old_id = player_games.get(pid)
        old = games.get(old_id) if old_id else None

        if old and not old["result"]:
            return game_state(db, pid)

        if pid in waiting:
            waiting[pid]["seen"] = now
            return waiting_state(db, pid, now)

        # Сыгранный раунд остаётся доступен второму игроку,
        # но этот игрок может начать искать новую партию.
        if old:
            player_games.pop(pid, None)

        if balance_of(db, pid) < body.stake:
            raise HTTPException(400, "Недостаточно фишек для ставки")

        opponent_id = None
        for other_id, entry in waiting.items():
            if (
                other_id != pid
                and entry["stake"] == body.stake
                and now - entry["started"] < SEARCH_SECONDS
            ):
                opponent_id = other_id
                break

        reserve(db, pid, body.stake)

        if opponent_id is not None:
            create_game(
                [opponent_id, pid],
                {
                    opponent_id: waiting[opponent_id]["name"],
                    pid: user["first_name"],
                },
                body.stake,
            )
            return game_state(db, pid)

        waiting[pid] = {
            "started": now,
            "seen": now,
            "stake": body.stake,
            "name": user["first_name"],
        }
        return waiting_state(db, pid, now)


@app.post("/api/state")
def state(body: LoginRequest):
    user = telegram_user(body.init_data)
    pid = user["id"]
    now = time.monotonic()

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        clean_inactive_waiters(db, now, except_id=pid)

        if pid in player_games:
            return game_state(db, pid)

        entry = waiting.get(pid)
        if entry is None:
            return {
                "status": "idle",
                "balance": balance_of(db, pid),
            }

        entry["seen"] = now

        if now - entry["started"] >= SEARCH_SECONDS:
            create_game(
                [pid, "bot"],
                {pid: user["first_name"], "bot": "Бот"},
                entry["stake"],
                bot=True,
            )
            return game_state(db, pid)

        return waiting_state(db, pid, now)


@app.post("/api/cancel")
def cancel(body: LoginRequest):
    user = telegram_user(body.init_data)
    pid = user["id"]

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        entry = waiting.pop(pid, None)

        if entry:
            refund(db, pid, entry["stake"])

        if pid in player_games:
            return game_state(db, pid)

        return {
            "status": "idle",
            "balance": balance_of(db, pid),
        }


@app.post("/api/play")
def play(body: PlayRequest):
    user = telegram_user(body.init_data)
    pid = user["id"]

    if body.action not in ("draw", "stand"):
        raise HTTPException(400, "Неизвестное действие")

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)

        game_id = player_games.get(pid)
        game = games.get(game_id) if game_id else None

        if not game or game["result"]:
            raise HTTPException(400, "Активная партия не найдена")

        if game["finished"][pid]:
            raise HTTPException(400, "Ты уже закончил ход")

        if body.action == "draw":
            game["hands"][pid].append(game["deck"].pop())
            if score(game["hands"][pid]) >= 21:
                game["finished"][pid] = True
        else:
            game["finished"][pid] = True

        if game["bot"] and game["finished"][pid]:
            while score(game["hands"]["bot"]) < 17:
                game["hands"]["bot"].append(game["deck"].pop())
            game["finished"]["bot"] = True

        if all(game["finished"].values()):
            finish_game(db, game)

        return game_state(db, pid)
