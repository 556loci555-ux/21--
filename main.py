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
RAISE_AMOUNTS = {10, 20, 50}
SEARCH_SECONDS = 21

app = FastAPI(title="Игра 21")

# Прототип для одного процесса сервера.
lock = threading.Lock()
waiting = {}
games = {}
player_games = {}


class LoginRequest(BaseModel):
    init_data: str


class SearchRequest(LoginRequest):
    stake: int


class PlayRequest(LoginRequest):
    action: str


class RaiseRequest(LoginRequest):
    action: str
    amount: int = 0


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
        or any(
            char not in "0123456789abcdefABCDEF"
            for char in received_hash
        )
    ):
        raise HTTPException(401, "Нет подписи Telegram")

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
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

    if not hmac.compare_digest(
        expected,
        received_hash.lower(),
    ):
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


def reserve(db, player_id, amount):
    result = db.execute(
        """
        UPDATE players
        SET balance = balance - ?
        WHERE telegram_id = ? AND balance >= ?
        """,
        (amount, player_id, amount),
    )

    if result.rowcount != 1:
        raise HTTPException(
            400,
            "Недостаточно фишек для ставки",
        )


def credit(db, player_id, amount):
    db.execute(
        """
        UPDATE players
        SET balance = balance + ?
        WHERE telegram_id = ?
        """,
        (amount, player_id),
    )


def make_deck():
    suits = ["♠", "♥", "♦", "♣"]

    ranks = [
        ("6", 6),
        ("7", 7),
        ("8", 8),
        ("9", 9),
        ("10", 10),
        ("В", 2),
        ("Д", 3),
        ("К", 4),
        ("Т", 11),
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

    first_points = score(game["hands"][first])
    second_points = score(game["hands"][second])

    if first_points > 21 and second_points > 21:
        return None

    if first_points > 21:
        return second

    if second_points > 21:
        return first

    if first_points > second_points:
        return first

    if second_points > first_points:
        return second

    return None


def finish_game(db, game):
    # Все изменения партии выполняются под lock.
    # Повторно выплачивать результат нельзя.
    if game["result"]:
        return

    # Неотвеченное предложение больше не действует.
    game["raise_offer"] = None

    game["winner"] = winner_of(game)
    game["result"] = True

    first, second = game["players"]
    stake = game["stake"]

    if game["winner"] is None:
        for player_id in (first, second):
            if player_id != "bot":
                credit(db, player_id, stake)

    elif game["winner"] != "bot":
        # Победитель получает обе ставки.
        # Для игры с ботом его виртуальная ставка
        # выплачивается по тем же правилам.
        credit(db, game["winner"], stake * 2)


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
            player_id: [deck.pop(), deck.pop()]
            for player_id in player_ids
        },
        "finished": {
            player_id: False
            for player_id in player_ids
        },
        "raise_offer": None,
        "result": False,
        "winner": None,
    }

    games[game_id] = game

    for player_id in player_ids:
        if player_id != "bot":
            player_games[player_id] = game_id
            waiting.pop(player_id, None)

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
        participant
        for participant in game["players"]
        if participant != player_id
    )

    mine = game["hands"][player_id]
    theirs = game["hands"][opponent_id]

    offer = game["raise_offer"]
    visible_offer = None

    if offer and not game["result"]:
        visible_offer = {
            "amount": offer["amount"],
            "from_me": offer["from"] == player_id,
        }

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
        "raise_offer": visible_offer,
    }

    if game["result"]:
        state.update({
            "opponent_cards": [
                card["label"]
                for card in theirs
            ],
            "opponent_points": score(theirs),
            "winner": (
                "draw"
                if game["winner"] is None
                else "me"
                if game["winner"] == player_id
                else "opponent"
            ),
        })

    return state


def waiting_state(db, player_id, now):
    entry = waiting[player_id]

    left = max(
        0,
        math.ceil(
            SEARCH_SECONDS - (now - entry["started"])
        ),
    )

    return {
        "status": "waiting",
        "seconds_left": left,
        "stake": entry["stake"],
        "balance": balance_of(db, player_id),
    }


def clean_inactive_waiters(db, now, except_id=None):
    for player_id, entry in list(waiting.items()):
        if (
            player_id != except_id
            and now - entry["seen"] > 15
        ):
            credit(db, player_id, entry["stake"])
            waiting.pop(player_id, None)


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
    player_id = user["id"]

    if (
        type(body.stake) is not int
        or body.stake not in ALLOWED_STAKES
    ):
        raise HTTPException(
            400,
            "Допустимые ставки: 10, 20 или 50",
        )

    now = time.monotonic()

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        clean_inactive_waiters(
            db,
            now,
            except_id=player_id,
        )

        old_id = player_games.get(player_id)
        old_game = games.get(old_id) if old_id else None

        if old_game and not old_game["result"]:
            return game_state(db, player_id)

        if player_id in waiting:
            waiting[player_id]["seen"] = now
            return waiting_state(db, player_id, now)

        if old_game:
            player_games.pop(player_id, None)

        if balance_of(db, player_id) < body.stake:
            raise HTTPException(
                400,
                "Недостаточно фишек для ставки",
            )

        opponent_id = None

        for other_id, entry in waiting.items():
            if (
                other_id != player_id
                and entry["stake"] == body.stake
                and now - entry["started"] < SEARCH_SECONDS
            ):
                opponent_id = other_id
                break

        reserve(db, player_id, body.stake)

        if opponent_id is not None:
            create_game(
                [opponent_id, player_id],
                {
                    opponent_id: waiting[opponent_id]["name"],
                    player_id: user["first_name"],
                },
                body.stake,
            )

            return game_state(db, player_id)

        waiting[player_id] = {
            "started": now,
            "seen": now,
            "stake": body.stake,
            "name": user["first_name"],
        }

        return waiting_state(db, player_id, now)


@app.post("/api/state")
def state(body: LoginRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]
    now = time.monotonic()

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        clean_inactive_waiters(
            db,
            now,
            except_id=player_id,
        )

        if player_id in player_games:
            return game_state(db, player_id)

        entry = waiting.get(player_id)

        if entry is None:
            return {
                "status": "idle",
                "balance": balance_of(db, player_id),
            }

        entry["seen"] = now

        if now - entry["started"] >= SEARCH_SECONDS:
            create_game(
                [player_id, "bot"],
                {
                    player_id: user["first_name"],
                    "bot": "Бот",
                },
                entry["stake"],
                bot=True,
            )

            return game_state(db, player_id)

        return waiting_state(db, player_id, now)


@app.post("/api/cancel")
def cancel(body: LoginRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)
        entry = waiting.pop(player_id, None)

        if entry:
            credit(db, player_id, entry["stake"])

        if player_id in player_games:
            return game_state(db, player_id)

        return {
            "status": "idle",
            "balance": balance_of(db, player_id),
        }


@app.post("/api/play")
def play(body: PlayRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]

    if body.action not in ("draw", "stand"):
        raise HTTPException(
            400,
            "Неизвестное действие",
        )

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)

        game_id = player_games.get(player_id)
        game = games.get(game_id) if game_id else None

        if not game or game["result"]:
            raise HTTPException(
                400,
                "Активная партия не найдена",
            )

        if game["finished"][player_id]:
            raise HTTPException(
                400,
                "Ты уже закончил ход",
            )

        if body.action == "draw":
            game["hands"][player_id].append(
                game["deck"].pop()
            )

            if score(game["hands"][player_id]) >= 21:
                game["finished"][player_id] = True
        else:
            game["finished"][player_id] = True

        if game["bot"] and game["finished"][player_id]:
            while score(game["hands"]["bot"]) < 17:
                game["hands"]["bot"].append(
                    game["deck"].pop()
                )

            game["finished"]["bot"] = True

        if all(game["finished"].values()):
            finish_game(db, game)

        return game_state(db, player_id)


@app.post("/api/raise")
def raise_stake(body: RaiseRequest):
    user = telegram_user(body.init_data)
    player_id = user["id"]

    if body.action not in (
        "propose",
        "accept",
        "decline",
    ):
        raise HTTPException(
            400,
            "Неизвестное действие со ставкой",
        )

    with lock, sqlite3.connect(DB_PATH) as db:
        ensure_player(db, user)

        game_id = player_games.get(player_id)
        game = games.get(game_id) if game_id else None

        if not game or game["result"]:
            raise HTTPException(
                400,
                "Раунд уже завершён",
            )

        opponent_id = next(
            participant
            for participant in game["players"]
            if participant != player_id
        )

        offer = game["raise_offer"]

        if body.action == "propose":
            if (
                type(body.amount) is not int
                or body.amount not in RAISE_AMOUNTS
            ):
                raise HTTPException(
                    400,
                    "Можно добавить 10, 20 или 50 фишек",
                )

            if offer is not None:
                raise HTTPException(
                    400,
                    "Сначала дождись ответа "
                    "на предыдущее предложение",
                )

            if balance_of(db, player_id) < body.amount:
                raise HTTPException(
                    400,
                    "Недостаточно фишек для повышения",
                )

            if game["bot"]:
                # Бот принимает предложение сразу.
                reserve(db, player_id, body.amount)
                game["stake"] += body.amount

            else:
                game["raise_offer"] = {
                    "from": player_id,
                    "amount": body.amount,
                }

            return game_state(db, player_id)

        if offer is None:
            raise HTTPException(
                400,
                "Нет предложения о повышении",
            )

        if offer["from"] == player_id:
            raise HTTPException(
                400,
                "На своё предложение ответить нельзя",
            )

        if body.action == "decline":
            game["raise_offer"] = None
            return game_state(db, player_id)

        amount = offer["amount"]

        # Проверяем обоих ДО списания. После проверки
        # lock не позволяет другому запросу вмешаться
        # между списаниями внутри этого процесса.
        if (
            balance_of(db, player_id) < amount
            or balance_of(db, opponent_id) < amount
        ):
            game["raise_offer"] = None

            raise HTTPException(
                400,
                "Повышение отменено: у одного "
                "из игроков недостаточно фишек",
            )

        reserve(db, player_id, amount)
        reserve(db, opponent_id, amount)

        game["stake"] += amount
        game["raise_offer"] = None

        return game_state(db, player_id)
