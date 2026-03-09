#!/usr/bin/env python3
"""
TAP RUSH — Unified API Server
Combines Tournament + Vote backends into one FastAPI app.
Designed for deployment on Railway / Render / any container host.

Tournament endpoints: /tournament/...
Vote endpoints:       /vote/...
Play.fun endpoints:   /playfun/...
"""

import json
import os
import sqlite3
import urllib.request
import time
import hashlib
import hmac
import logging
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

logger = logging.getLogger("taprush.main")

from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import crons

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
RUSH_MINT = "ZZdUjmm6stModTGwB7yQk9RphzbV6WYHMD5Wz7oPLAY"
DEPOSIT_ADDRESS = "7rH4WYQ9Y7UjmizQxvHmpgLyvsBZfArweE48DWrcyoXu"
DEPOSIT_ATA = "p7dB4kZFt1q7VxNd9wtNTFt7q39kiQBQTYYc4KbXXNg"  # Hardcoded — RPC rate limits getTokenAccountsByOwner

RPC_URLS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]

# Database paths — use /data/ on Railway (persistent volume), fallback to local
DATA_DIR = os.environ.get("DATA_DIR", ".")
TOURNAMENT_DB = os.path.join(DATA_DIR, "tournament.db")
VOTES_DB = os.path.join(DATA_DIR, "votes.db")

BUY_IN = 1_000_000  # 1M RUSH tokens
PAYOUT_SPLIT = {"1st": 0.70, "2nd": 0.20, "3rd": 0.10}
ADMIN_KEY_HASH = hashlib.sha256(b"taprush2026admin").hexdigest()
SESSION_LOCK_TIMEOUT = 30  # seconds
MIN_VOTE_DEPOSIT = 1  # 1 RUSH to vote

PORT = int(os.environ.get("PORT", 8000))


# ══════════════════════════════════════════════
#  SOLANA RPC HELPERS
# ══════════════════════════════════════════════
def solana_rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data_bytes = json.dumps(payload).encode("utf-8")
    for rpc_url in RPC_URLS:
        req = urllib.request.Request(
            rpc_url, data=data_bytes,
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            result = json.loads(resp.read().decode("utf-8"))
            if result and "error" not in result:
                return result
        except Exception:
            continue
    return None


def get_rush_balance(wallet_address):
    data = solana_rpc("getTokenAccountsByOwner", [
        wallet_address,
        {"mint": RUSH_MINT},
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    if data and data.get("result", {}).get("value"):
        total = 0.0
        for ta in data["result"]["value"]:
            bi = ta["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(bi.get("uiAmount") or 0)
        return total
    return 0.0


def get_deposit_ata():
    """Get the RUSH Associated Token Account for the deposit wallet."""
    data = solana_rpc("getTokenAccountsByOwner", [
        DEPOSIT_ADDRESS,
        {"mint": RUSH_MINT},
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    if data and data.get("result", {}).get("value"):
        return data["result"]["value"][0]["pubkey"]
    return None


# ══════════════════════════════════════════════
#  DATABASE HELPERS
# ══════════════════════════════════════════════
def get_tournament_db():
    db = sqlite3.connect(TOURNAMENT_DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    db.execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id TEXT PRIMARY KEY,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            total_pool INTEGER DEFAULT 0,
            entries INTEGER DEFAULT 0,
            winner_1st TEXT, winner_2nd TEXT, winner_3rd TEXT,
            payout_1st INTEGER DEFAULT 0, payout_2nd INTEGER DEFAULT 0, payout_3rd INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id TEXT NOT NULL,
            address TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            tx_signature TEXT,
            best_score INTEGER DEFAULT 0,
            best_grade TEXT DEFAULT '',
            total_games INTEGER DEFAULT 0,
            deposits INTEGER DEFAULT 1,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tournament_id, address),
            FOREIGN KEY(tournament_id) REFERENCES tournaments(id)
        )
    """)
    try:
        db.execute("ALTER TABLE registrations ADD COLUMN deposits INTEGER DEFAULT 1")
        db.commit()
    except Exception:
        pass

    db.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id TEXT NOT NULL,
            address TEXT NOT NULL,
            score INTEGER NOT NULL,
            grade TEXT DEFAULT '',
            game_mode TEXT DEFAULT 'normal',
            session_time INTEGER DEFAULT 0,
            device_type TEXT DEFAULT 'desktop',
            is_bust INTEGER DEFAULT 0,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(tournament_id) REFERENCES tournaments(id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS session_locks (
            address TEXT PRIMARY KEY,
            tournament_id TEXT NOT NULL,
            locked_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL
        )
    """)
    db.commit()
    return db


def get_votes_db():
    db = sqlite3.connect(VOTES_DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id TEXT NOT NULL,
            address TEXT NOT NULL,
            vote TEXT NOT NULL,
            balance REAL DEFAULT 0,
            vote_weight REAL DEFAULT 0,
            tx_signature TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(poll_id, address)
        )
    """)
    db.commit()
    return db


def get_current_tournament_id():
    now = datetime.now(timezone.utc)
    if now.hour < 23:
        start_date = now.date() - timedelta(days=1)
    else:
        start_date = now.date()
    return f"RUSH-{start_date.strftime('%Y-%m-%d')}"


def ensure_tournament(db):
    tid = get_current_tournament_id()
    now = datetime.now(timezone.utc)
    existing = db.execute("SELECT id, status, end_time FROM tournaments WHERE id = ?", [tid]).fetchone()
    if not existing:
        if now.hour < 23:
            start = (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
        else:
            start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        db.execute(
            "INSERT INTO tournaments (id, start_time, end_time, status) VALUES (?, ?, ?, 'active')",
            [tid, start.isoformat(), end.isoformat()]
        )
        db.commit()
    elif existing["status"] == "finalized":
        # Tournament was finalized by cron but we're still in its time window.
        # Check if end_time hasn't passed yet — if so, reactivate it.
        end_time = datetime.fromisoformat(existing["end_time"]).replace(tzinfo=timezone.utc)
        if now < end_time:
            db.execute("UPDATE tournaments SET status = 'active' WHERE id = ?", [tid])
            db.commit()
            logger.info(f"Reactivated tournament {tid} (finalized early, still within window)")
    return tid


# ══════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app):
    os.makedirs(DATA_DIR, exist_ok=True)
    # Start cron scheduler
    crons.start_scheduler()
    yield
    # Stop cron scheduler on shutdown
    crons.stop_scheduler()

app = FastAPI(title="TAP RUSH API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════
@app.get("/")
def health():
    return {"status": "ok", "service": "TAP RUSH API"}


# ══════════════════════════════════════════════
#  TOURNAMENT ENDPOINTS
# ══════════════════════════════════════════════
@app.get("/tournament/tournament")
def tournament_info():
    db = get_tournament_db()
    tid = ensure_tournament(db)
    t = db.execute("SELECT * FROM tournaments WHERE id = ?", [tid]).fetchone()
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(t["end_time"]).replace(tzinfo=timezone.utc)
    remaining = max(0, int((end - now).total_seconds()))

    leaders = db.execute("""
        SELECT address, display_name, best_score, best_grade, total_games
        FROM registrations WHERE tournament_id = ? AND best_score > 0
        ORDER BY best_score DESC LIMIT 10
    """, [tid]).fetchall()

    leaderboard = []
    for i, r in enumerate(leaders):
        addr = r["address"]
        short_addr = addr[:4] + "..." + addr[-4:] if len(addr) > 8 else addr
        leaderboard.append({
            "rank": i + 1, "address": addr, "short_address": short_addr,
            "display_name": r["display_name"] or short_addr,
            "best_score": r["best_score"], "best_grade": r["best_grade"],
            "total_games": r["total_games"]
        })

    prize_pool = t["entries"] * BUY_IN
    db.close()
    return {
        "tournament_id": tid, "status": t["status"],
        "start_time": t["start_time"], "end_time": t["end_time"],
        "time_remaining_seconds": remaining, "entries": t["entries"],
        "prize_pool": prize_pool, "prize_pool_display": f"{prize_pool:,.0f} RUSH",
        "buy_in": BUY_IN, "buy_in_display": f"{BUY_IN:,.0f} RUSH",
        "payouts": {
            "1st": f"{int(prize_pool * 0.70):,.0f} RUSH",
            "2nd": f"{int(prize_pool * 0.20):,.0f} RUSH",
            "3rd": f"{int(prize_pool * 0.10):,.0f} RUSH",
        },
        "deposit_address": DEPOSIT_ADDRESS, "leaderboard": leaderboard
    }


@app.post("/tournament/register")
async def tournament_register(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    display_name = body.get("display_name", "").strip()[:20]
    tx_signature = body.get("tx_signature", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Wallet address required"})
    if not tx_signature:
        return JSONResponse(status_code=400, content={"error": "Deposit transaction signature required. Send 1M RUSH first."})

    db = get_tournament_db()
    tid = ensure_tournament(db)
    t = db.execute("SELECT status FROM tournaments WHERE id = ?", [tid]).fetchone()
    if t and t["status"] != "active":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Tournament is no longer accepting entries"})

    existing = db.execute(
        "SELECT id, deposits, total_games FROM registrations WHERE tournament_id = ? AND address = ?",
        [tid, address]
    ).fetchone()

    if existing:
        new_deposits = (existing["deposits"] or 1) + 1
        db.execute(
            "UPDATE registrations SET deposits = ?, tx_signature = ? WHERE tournament_id = ? AND address = ?",
            [new_deposits, tx_signature, tid, address]
        )
        db.execute("UPDATE tournaments SET total_pool = total_pool + ? WHERE id = ?", [BUY_IN, tid])
        db.commit()
        games_remaining = new_deposits - (existing["total_games"] or 0)
        db.close()

        # Email notification: re-deposit
        try:
            t_info = get_tournament_db()
            t_row = t_info.execute("SELECT entries FROM tournaments WHERE id = ?", [tid]).fetchone()
            pool = (t_row["entries"] if t_row else 0) * BUY_IN
            t_info.close()
            crons.send_email(
                f"TAP RUSH — Re-deposit! ({tid})",
                f"PLAYER RE-DEPOSITED\n"
                f"{'=' * 40}\n\n"
                f"Address: {address}\n"
                f"Tournament: {tid}\n"
                f"Deposit #{new_deposits}\n"
                f"TX Signature: {tx_signature}\n\n"
                f"Games Played: {existing['total_games'] or 0}\n"
                f"Games Remaining: {games_remaining}\n"
                f"Current Prize Pool: {pool:,.0f} RUSH"
            )
        except Exception as e:
            logger.warning(f"Failed to send re-deposit email: {e}")

        return {"success": True, "tournament_id": tid, "deposits": new_deposits,
                "total_games": existing["total_games"] or 0, "games_remaining": games_remaining,
                "message": f"Re-deposit accepted! You have {games_remaining} game(s) remaining."}

    try:
        db.execute(
            "INSERT INTO registrations (tournament_id, address, display_name, tx_signature, deposits) VALUES (?, ?, ?, ?, 1)",
            [tid, address, display_name, tx_signature]
        )
        db.execute("UPDATE tournaments SET entries = entries + 1, total_pool = total_pool + ? WHERE id = ?", [BUY_IN, tid])
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        return JSONResponse(status_code=400, content={"error": "Registration error"})

    t_updated = db.execute("SELECT entries FROM tournaments WHERE id = ?", [tid]).fetchone()
    pool = t_updated["entries"] * BUY_IN
    db.close()

    # Email notification: new tournament buy-in
    try:
        short_addr = address[:6] + "..." + address[-4:] if len(address) > 10 else address
        crons.send_email(
            f"TAP RUSH — New Tournament Entry! ({tid})",
            f"NEW PLAYER REGISTERED\n"
            f"{'=' * 40}\n\n"
            f"Address: {address}\n"
            f"Display Name: {display_name or 'N/A'}\n"
            f"Tournament: {tid}\n"
            f"TX Signature: {tx_signature}\n\n"
            f"Total Entries: {t_updated['entries']}\n"
            f"Prize Pool: {pool:,.0f} RUSH\n\n"
            f"Payouts if tournament ended now:\n"
            f"  1st: {int(pool * 0.70):,.0f} RUSH\n"
            f"  2nd: {int(pool * 0.20):,.0f} RUSH\n"
            f"  3rd: {int(pool * 0.10):,.0f} RUSH"
        )
    except Exception as e:
        logger.warning(f"Failed to send registration email: {e}")

    return {"success": True, "tournament_id": tid, "entries": t_updated["entries"],
            "deposits": 1, "total_games": 0, "games_remaining": 1,
            "prize_pool": f"{pool:,.0f} RUSH", "message": "You're in! You have 1 game. Make it count!"}


@app.post("/tournament/submit-score")
async def tournament_submit_score(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    score = body.get("score", 0)
    grade = body.get("grade", "").strip()
    game_mode = body.get("game_mode", "normal").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address required"})
    if not isinstance(score, (int, float)) or score <= 0:
        return JSONResponse(status_code=400, content={"error": "Valid positive score required"})
    score = int(score)

    db = get_tournament_db()
    tid = get_current_tournament_id()
    reg = db.execute(
        "SELECT id, best_score, total_games, deposits FROM registrations WHERE tournament_id = ? AND address = ?",
        [tid, address]
    ).fetchone()
    if not reg:
        db.close()
        return JSONResponse(status_code=400, content={"error": "Not registered for today's tournament."})

    deposits = reg["deposits"] or 1
    games_played = reg["total_games"] or 0
    if games_played >= deposits:
        db.close()
        return JSONResponse(status_code=403, content={"error": "No games remaining. Deposit another 1M RUSH to play again.",
                                   "code": "NO_GAMES_LEFT", "deposits": deposits,
                                   "total_games": games_played, "games_remaining": 0})

    t = db.execute("SELECT status, end_time FROM tournaments WHERE id = ?", [tid]).fetchone()
    if t["status"] != "active":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Tournament has ended"})
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(t["end_time"]).replace(tzinfo=timezone.utc)
    if now > end:
        db.close()
        return JSONResponse(status_code=400, content={"error": "Tournament time has expired"})

    session_time = body.get("session_time", 0)
    device_type = body.get("device_type", "desktop")
    is_bust = 1 if body.get("is_bust", False) else 0
    db.execute(
        "INSERT INTO scores (tournament_id, address, score, grade, game_mode, session_time, device_type, is_bust) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tid, address, score, grade, game_mode, session_time, device_type, is_bust]
    )

    new_best = False
    if score > (reg["best_score"] or 0):
        db.execute("UPDATE registrations SET best_score = ?, best_grade = ?, total_games = total_games + 1 WHERE tournament_id = ? AND address = ?",
                   [score, grade, tid, address])
        new_best = True
    else:
        db.execute("UPDATE registrations SET total_games = total_games + 1 WHERE tournament_id = ? AND address = ?", [tid, address])
    db.commit()

    # Safety net: release session lock
    try:
        db.execute("DELETE FROM session_locks WHERE address = ?", [address])
        db.commit()
    except Exception:
        pass

    rank_row = db.execute("SELECT COUNT(*) + 1 as rank FROM registrations WHERE tournament_id = ? AND best_score > ?",
                          [tid, score if new_best else reg["best_score"]]).fetchone()
    entries = db.execute("SELECT entries FROM tournaments WHERE id = ?", [tid]).fetchone()
    db.close()
    return {"success": True, "score": score, "grade": grade, "new_best": new_best,
            "current_rank": rank_row["rank"], "total_entries": entries["entries"],
            "tournament_id": tid, "deposits": deposits,
            "total_games": games_played + 1, "games_remaining": deposits - (games_played + 1)}


@app.get("/tournament/leaderboard")
def tournament_leaderboard(tournament_id: str = None):
    db = get_tournament_db()
    tid = tournament_id
    if not tid:
        tid = get_current_tournament_id()
        ensure_tournament(db)

    leaders = db.execute("""
        SELECT address, display_name, best_score, best_grade, total_games
        FROM registrations WHERE tournament_id = ? ORDER BY best_score DESC LIMIT 50
    """, [tid]).fetchall()
    t = db.execute("SELECT * FROM tournaments WHERE id = ?", [tid]).fetchone()
    if not t:
        db.close()
        return JSONResponse(status_code=404, content={"error": "Tournament not found"})

    result = []
    for i, r in enumerate(leaders):
        addr = r["address"]
        short_addr = addr[:4] + "..." + addr[-4:] if len(addr) > 8 else addr
        result.append({"rank": i + 1, "address": addr, "short_address": short_addr,
                       "display_name": r["display_name"] or short_addr,
                       "best_score": r["best_score"], "best_grade": r["best_grade"],
                       "total_games": r["total_games"]})
    prize_pool = t["entries"] * BUY_IN
    db.close()
    return {"tournament_id": tid, "status": t["status"], "entries": t["entries"],
            "prize_pool": f"{prize_pool:,.0f} RUSH", "leaderboard": result}


@app.get("/tournament/history")
def tournament_history(limit: int = 10):
    limit = min(limit, 30)
    db = get_tournament_db()
    tournaments = db.execute("SELECT * FROM tournaments WHERE status = 'finalized' ORDER BY start_time DESC LIMIT ?", [limit]).fetchall()
    results = []
    for t in tournaments:
        results.append({
            "tournament_id": t["id"], "start_time": t["start_time"], "end_time": t["end_time"],
            "entries": t["entries"], "prize_pool": f"{t['entries'] * BUY_IN:,.0f} RUSH",
            "winner_1st": t["winner_1st"], "winner_2nd": t["winner_2nd"], "winner_3rd": t["winner_3rd"],
            "payout_1st": f"{t['payout_1st']:,.0f} RUSH", "payout_2nd": f"{t['payout_2nd']:,.0f} RUSH",
            "payout_3rd": f"{t['payout_3rd']:,.0f} RUSH",
        })
    db.close()
    return {"history": results}


@app.post("/tournament/check-deposit")
async def tournament_check_deposit(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address required"})

    balance = get_rush_balance(address)
    query_address = DEPOSIT_ATA  # Use hardcoded ATA — RPC rate limits the dynamic lookup

    sigs_data = solana_rpc("getSignaturesForAddress", [query_address, {"limit": 20}])
    recent_sigs = []
    if sigs_data and sigs_data.get("result"):
        now_ts = time.time()
        for sig in sigs_data["result"]:
            block_time = sig.get("blockTime", 0)
            if block_time and (now_ts - block_time) < 7200:
                if not sig.get("err"):
                    recent_sigs.append({"signature": sig["signature"], "block_time": block_time,
                                        "time_ago": f"{int((now_ts - block_time) / 60)}m ago"})
    if recent_sigs:
        return {"deposit_confirmed": True, "tx_signature": recent_sigs[0]["signature"],
                "recent_transactions": len(recent_sigs), "balance": balance,
                "note": "Recent deposit detected. Your entry is being processed."}
    else:
        return {"deposit_confirmed": False,
                "reason": f"No recent deposit found. Send exactly {BUY_IN:,.0f} RUSH to {DEPOSIT_ADDRESS}",
                "deposit_address": DEPOSIT_ADDRESS, "balance": balance, "buy_in": BUY_IN}


@app.get("/tournament/player-status")
def tournament_player_status(address: str = ""):
    address = address.strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "address required"})
    db = get_tournament_db()
    tid = get_current_tournament_id()
    ensure_tournament(db)
    reg = db.execute("SELECT * FROM registrations WHERE tournament_id = ? AND address = ?", [tid, address]).fetchone()
    if not reg:
        db.close()
        return {"registered": False, "tournament_id": tid}

    rank_row = db.execute("SELECT COUNT(*) + 1 as rank FROM registrations WHERE tournament_id = ? AND best_score > ?",
                          [tid, reg["best_score"] or 0]).fetchone()
    entries = db.execute("SELECT entries FROM tournaments WHERE id = ?", [tid]).fetchone()
    deposits = reg["deposits"] if "deposits" in reg.keys() else 1
    total_games = reg["total_games"] or 0
    db.close()
    return {"registered": True, "tournament_id": tid, "best_score": reg["best_score"],
            "best_grade": reg["best_grade"], "total_games": total_games, "deposits": deposits,
            "games_remaining": max(0, deposits - total_games),
            "rank": rank_row["rank"], "total_entries": entries["entries"]}


@app.post("/tournament/admin/finalize")
async def tournament_admin_finalize(request: Request):
    body = await request.json()
    admin_key = body.get("admin_key", "")
    if hashlib.sha256(admin_key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})
    tid = body.get("tournament_id", "")
    if not tid:
        return JSONResponse(status_code=400, content={"error": "tournament_id required"})

    db = get_tournament_db()
    t = db.execute("SELECT * FROM tournaments WHERE id = ?", [tid]).fetchone()
    if not t:
        db.close()
        return JSONResponse(status_code=404, content={"error": "Tournament not found"})
    if t["status"] == "finalized":
        db.close()
        return JSONResponse(status_code=400, content={"error": "Already finalized"})

    top3 = db.execute("""
        SELECT address, display_name, best_score, best_grade FROM registrations
        WHERE tournament_id = ? AND best_score > 0 ORDER BY best_score DESC LIMIT 3
    """, [tid]).fetchall()

    pool = t["entries"] * BUY_IN
    winners = {}
    places = ["1st", "2nd", "3rd"]
    for i, place in enumerate(places):
        if i < len(top3):
            winners[place] = {"address": top3[i]["address"], "score": top3[i]["best_score"],
                              "payout": int(pool * PAYOUT_SPLIT[place])}
        else:
            winners[place] = {"address": None, "score": 0, "payout": 0}

    db.execute("""
        UPDATE tournaments SET status = 'finalized',
            winner_1st = ?, winner_2nd = ?, winner_3rd = ?,
            payout_1st = ?, payout_2nd = ?, payout_3rd = ?
        WHERE id = ?
    """, [winners["1st"]["address"], winners["2nd"]["address"], winners["3rd"]["address"],
          winners["1st"]["payout"], winners["2nd"]["payout"], winners["3rd"]["payout"], tid])
    db.commit()
    db.close()
    return {"success": True, "tournament_id": tid, "entries": t["entries"],
            "prize_pool": f"{pool:,.0f} RUSH", "winners": winners}


@app.post("/tournament/admin/clear-leaderboard")
async def tournament_admin_clear_leaderboard(request: Request):
    body = await request.json()
    admin_key = body.get("admin_key", "")
    if hashlib.sha256(admin_key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})
    tid = body.get("tournament_id", "")
    if not tid:
        return JSONResponse(status_code=400, content={"error": "tournament_id required"})

    db = get_tournament_db()
    t = db.execute("SELECT * FROM tournaments WHERE id = ?", [tid]).fetchone()
    if not t:
        db.close()
        return JSONResponse(status_code=404, content={"error": "Tournament not found"})

    # Delete all scores for this tournament
    deleted_scores = db.execute("DELETE FROM scores WHERE tournament_id = ?", [tid]).rowcount
    # Reset registration scores but keep the registrations (players stay registered)
    reset_regs = db.execute("""
        UPDATE registrations SET best_score = 0, best_grade = '', total_games = 0
        WHERE tournament_id = ?
    """, [tid]).rowcount
    # Clear any session locks for this tournament
    db.execute("DELETE FROM session_locks WHERE tournament_id = ?", [tid])
    db.commit()
    db.close()
    logging.info(f"Leaderboard cleared for {tid}: {deleted_scores} scores deleted, {reset_regs} registrations reset")
    return {"success": True, "tournament_id": tid,
            "scores_deleted": deleted_scores, "registrations_reset": reset_regs}


@app.get("/tournament/dev-leaderboard")
def tournament_dev_leaderboard(key: str = "", tournament_id: str = None):
    if hashlib.sha256(key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})

    db = get_tournament_db()
    tid = tournament_id
    if not tid:
        tid = get_current_tournament_id()
        ensure_tournament(db)

    t = db.execute("SELECT * FROM tournaments WHERE id = ?", [tid]).fetchone()
    if not t:
        db.close()
        return JSONResponse(status_code=404, content={"error": "Tournament not found"})

    regs = db.execute("""
        SELECT address, display_name, best_score, best_grade, total_games, registered_at, tx_signature, deposits
        FROM registrations WHERE tournament_id = ? ORDER BY best_score DESC
    """, [tid]).fetchall()

    all_scores = db.execute("""
        SELECT address, score, grade, game_mode, session_time, device_type, is_bust, submitted_at
        FROM scores WHERE tournament_id = ? ORDER BY submitted_at DESC
    """, [tid]).fetchall()

    players = []
    for i, r in enumerate(regs):
        addr = r["address"]
        player_scores = [s for s in all_scores if s["address"] == addr]
        games = [{"score": s["score"], "grade": s["grade"], "game_mode": s["game_mode"],
                  "session_time": s["session_time"] or 0, "device_type": s["device_type"] or "desktop",
                  "is_bust": bool(s["is_bust"]), "submitted_at": s["submitted_at"]} for s in player_scores]
        dep_count = r["deposits"] if "deposits" in r.keys() else 1
        players.append({
            "rank": i + 1, "address": addr, "display_name": r["display_name"] or "",
            "best_score": r["best_score"], "best_grade": r["best_grade"],
            "total_games": r["total_games"], "deposits": dep_count,
            "games_remaining": max(0, dep_count - (r["total_games"] or 0)),
            "cheating": (r["total_games"] or 0) > dep_count,
            "registered_at": r["registered_at"], "tx_signature": r["tx_signature"] or "", "games": games
        })

    pool = t["entries"] * BUY_IN
    payouts = {}
    for i, place in enumerate(["1st", "2nd", "3rd"]):
        if i < len(players):
            payout_amount = int(pool * PAYOUT_SPLIT[place])
            payouts[place] = {"address": players[i]["address"], "display_name": players[i]["display_name"],
                              "score": players[i]["best_score"], "grade": players[i]["best_grade"],
                              "payout_rush": payout_amount, "payout_display": f"{payout_amount:,.0f} RUSH"}
        else:
            payouts[place] = {"address": None, "payout_rush": 0, "payout_display": "N/A"}
    db.close()
    return {"tournament_id": tid, "status": t["status"], "start_time": t["start_time"],
            "end_time": t["end_time"], "entries": t["entries"], "prize_pool": pool,
            "prize_pool_display": f"{pool:,.0f} RUSH", "payouts": payouts, "players": players}


# ── Session Lock Endpoints ──
@app.post("/tournament/claim-session")
async def tournament_claim_session(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address required"})

    db = get_tournament_db()
    tid = get_current_tournament_id()
    now = time.time()

    existing = db.execute("SELECT address, heartbeat_at FROM session_locks WHERE address = ?", [address]).fetchone()
    if existing:
        age = now - existing["heartbeat_at"]
        if age < SESSION_LOCK_TIMEOUT:
            db.close()
            return JSONResponse(status_code=409, content={"error": "Session already active", "code": "SESSION_LOCKED",
                                       "message": "This wallet already has a tournament game in progress on another device or tab. Finish that game first.",
                                       "locked_seconds_ago": int(age)})
        else:
            db.execute("UPDATE session_locks SET tournament_id = ?, locked_at = ?, heartbeat_at = ? WHERE address = ?",
                       [tid, now, now, address])
            db.commit()
            db.close()
            return {"success": True, "message": "Session claimed (previous session expired)"}
    else:
        db.execute("INSERT INTO session_locks (address, tournament_id, locked_at, heartbeat_at) VALUES (?, ?, ?, ?)",
                   [address, tid, now, now])
        db.commit()
        db.close()
        return {"success": True, "message": "Session claimed"}


@app.post("/tournament/heartbeat-session")
async def tournament_heartbeat_session(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address required"})
    db = get_tournament_db()
    now = time.time()
    result = db.execute("UPDATE session_locks SET heartbeat_at = ? WHERE address = ?", [now, address])
    db.commit()
    count = result.rowcount
    db.close()
    if count > 0:
        return {"success": True}
    else:
        return JSONResponse(status_code=404, content={"error": "No active session"})


@app.post("/tournament/release-session")
async def tournament_release_session(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address required"})
    db = get_tournament_db()
    db.execute("DELETE FROM session_locks WHERE address = ?", [address])
    db.commit()
    db.close()
    return {"success": True, "message": "Session released"}


# ══════════════════════════════════════════════
#  VOTE ENDPOINTS
# ══════════════════════════════════════════════
def get_poll_results(db, poll_id):
    rows = db.execute("SELECT vote, SUM(vote_weight), COUNT(*) FROM votes WHERE poll_id = ? GROUP BY vote", [poll_id]).fetchall()
    counts = {"yes": 0, "no": 0, "yes_voters": 0, "no_voters": 0}
    for vote_val, weight_sum, voter_count in rows:
        if vote_val in ("yes", "no"):
            counts[vote_val] = round(weight_sum or 0, 2)
            counts[vote_val + "_voters"] = voter_count
    counts["total"] = round(counts["yes"] + counts["no"], 2)
    counts["total_voters"] = counts["yes_voters"] + counts["no_voters"]
    return counts


def get_existing_vote(db, poll_id, address):
    row = db.execute("SELECT vote, vote_weight FROM votes WHERE poll_id = ? AND address = ?", [poll_id, address]).fetchone()
    return {"vote": row[0], "weight": row[1]} if row else None


@app.post("/vote/verify")
async def vote_verify(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address is required"})

    balance = get_rush_balance(address)
    verified = balance > 0
    result = {"verified": verified, "balance": str(balance), "address": address,
              "deposit_address": DEPOSIT_ADDRESS, "min_deposit": MIN_VOTE_DEPOSIT}
    if verified:
        db = get_votes_db()
        existing = get_existing_vote(db, "sol-chart", address)
        if existing:
            result["existing_vote"] = existing["vote"]
            result["vote_weight"] = existing["weight"]
        db.close()
    return result


@app.post("/vote/check-deposit")
async def vote_check_deposit(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse(status_code=400, content={"error": "Address is required"})

    # Get deposit ATA
    deposit_ata = get_deposit_ata()
    query_address = deposit_ata or DEPOSIT_ADDRESS

    sender_token_data = solana_rpc("getTokenAccountsByOwner", [
        address, {"mint": RUSH_MINT}, {"encoding": "jsonParsed"}
    ])
    if not sender_token_data or not sender_token_data.get("result", {}).get("value"):
        return {"deposit_confirmed": False, "reason": "No RUSH token account found for your wallet."}

    sender_token_account = sender_token_data["result"]["value"][0]["pubkey"]

    sigs_data = solana_rpc("getSignaturesForAddress", [query_address, {"limit": 50}])
    if not sigs_data or not sigs_data.get("result"):
        return {"deposit_confirmed": False, "reason": "No recent transactions found on deposit address."}

    for sig_info in sigs_data["result"]:
        if sig_info.get("err"):
            continue
        sig = sig_info["signature"]
        tx_data = solana_rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx_data or not tx_data.get("result"):
            continue
        tx = tx_data["result"]
        meta = tx.get("meta", {})
        if meta.get("err"):
            continue

        all_instructions = []
        msg = tx.get("transaction", {}).get("message", {})
        all_instructions.extend(msg.get("instructions", []))
        for inner in meta.get("innerInstructions", []):
            all_instructions.extend(inner.get("instructions", []))

        for ix in all_instructions:
            parsed = ix.get("parsed")
            if not parsed:
                continue
            ix_type = parsed.get("type", "")
            info = parsed.get("info", {})
            if ix_type in ("transfer", "transferChecked"):
                source = info.get("source", "")
                destination = info.get("destination", "")
                if source == sender_token_account and destination == query_address:
                    if ix_type == "transferChecked":
                        amount = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
                        if amount >= MIN_VOTE_DEPOSIT:
                            balance = get_rush_balance(address)
                            return {"deposit_confirmed": True, "tx_signature": sig,
                                    "deposit_amount": amount, "current_balance": balance, "vote_weight": balance}
                    else:
                        raw_amount = int(info.get("amount", 0))
                        decimals_data = solana_rpc("getAccountInfo", [sender_token_account, {"encoding": "jsonParsed"}])
                        decimals = 9
                        if decimals_data and decimals_data.get("result", {}).get("value"):
                            token_info = decimals_data["result"]["value"]["data"]["parsed"]["info"]
                            decimals = token_info.get("tokenAmount", {}).get("decimals", 9)
                        ui_amount = raw_amount / (10 ** decimals)
                        if ui_amount >= MIN_VOTE_DEPOSIT:
                            balance = get_rush_balance(address)
                            return {"deposit_confirmed": True, "tx_signature": sig,
                                    "deposit_amount": ui_amount, "current_balance": balance, "vote_weight": balance}

    return {"deposit_confirmed": False, "reason": "No qualifying RUSH deposit found. Please send at least 1 RUSH."}


@app.post("/vote/vote")
async def vote_cast(request: Request):
    body = await request.json()
    address = body.get("address", "").strip()
    vote = body.get("vote", "").strip().lower()
    poll_id = body.get("poll_id", "").strip()
    tx_signature = body.get("tx_signature", "").strip()

    if not address:
        return JSONResponse(status_code=400, content={"error": "Address is required"})
    if vote not in ("yes", "no"):
        return JSONResponse(status_code=400, content={"error": "Vote must be 'yes' or 'no'"})
    if not poll_id:
        return JSONResponse(status_code=400, content={"error": "poll_id is required"})

    balance = get_rush_balance(address)
    if balance <= 0:
        return JSONResponse(status_code=400, content={"error": "No $RUSH tokens found"})
    if not tx_signature:
        return JSONResponse(status_code=400, content={"error": "Deposit not confirmed. Please send 1 RUSH first."})

    db = get_votes_db()
    existing = get_existing_vote(db, poll_id, address)
    if existing:
        results = get_poll_results(db, poll_id)
        db.close()
        return JSONResponse(status_code=400, content={"error": "This wallet has already voted",
                                   "existing_vote": existing["vote"], "vote_weight": existing["weight"],
                                   "results": results})

    try:
        db.execute("INSERT INTO votes (poll_id, address, vote, balance, vote_weight, tx_signature) VALUES (?, ?, ?, ?, ?, ?)",
                   [poll_id, address, vote, balance, balance, tx_signature])
        db.commit()
    except sqlite3.IntegrityError:
        results = get_poll_results(db, poll_id)
        db.close()
        return JSONResponse(status_code=400, content={"error": "This wallet has already voted", "results": results})

    results = get_poll_results(db, poll_id)
    db.close()
    return {"success": True, "vote_weight": balance, "results": results}


@app.get("/vote/results")
def vote_results(poll_id: str = ""):
    if not poll_id:
        return JSONResponse(status_code=400, content={"error": "poll_id is required"})
    db = get_votes_db()
    results = get_poll_results(db, poll_id)
    db.close()
    return results


# ══════════════════════════════════════════════
#  CRON ADMIN ENDPOINTS
# ══════════════════════════════════════════════
@app.get("/cron/status")
def cron_status(key: str = ""):
    if hashlib.sha256(key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})
    return crons.get_scheduler_status()


@app.get("/cron/logs")
def cron_logs(key: str = "", limit: int = 50):
    if hashlib.sha256(key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})
    limit = min(limit, 200)
    log_file = os.path.join(DATA_DIR, "cron_log.json")
    try:
        with open(log_file, "r") as f:
            logs = json.load(f)
        return {"logs": logs[-limit:]}
    except Exception:
        return {"logs": []}


@app.post("/cron/trigger")
async def cron_trigger(request: Request):
    """Manually trigger a cron job (for testing)."""
    body = await request.json()
    admin_key = body.get("admin_key", "")
    if hashlib.sha256(admin_key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})

    job_id = body.get("job_id", "")
    valid_jobs = {
        "rush_monitor": crons.cron_rush_monitor,
        "vote_checker": crons.cron_vote_checker,
        "tournament_end": crons.cron_tournament_end,
    }
    if job_id not in valid_jobs:
        return JSONResponse(status_code=400, content={"error": f"Invalid job_id. Valid: {list(valid_jobs.keys())}"})

    # Run in background thread to not block
    import threading
    t = threading.Thread(target=valid_jobs[job_id], daemon=True)
    t.start()
    return {"success": True, "message": f"Job '{job_id}' triggered", "note": "Running in background"}


# ══════════════════════════════════════════════
#  PLAY.FUN HYBRID INTEGRATION
#  Server-side point submission (SSV mode)
# ══════════════════════════════════════════════
PLAYFUN_API_KEY = os.environ.get("PLAYFUN_API_KEY", "00ade5fd-f5fe-4b3f-b172-e6ddd6b19bfe")
PLAYFUN_SECRET  = os.environ.get("PLAYFUN_SECRET",  "c97fccbd-adae-43da-869d-8c74ff1db0c0")
PLAYFUN_GAME_ID = os.environ.get("PLAYFUN_GAME_ID", "4b568177-ec1b-4976-9cbc-582f35096f66")
PLAYFUN_API_URL = "https://api.play.fun"


def _playfun_hmac(method: str, path: str, timestamp: str) -> str:
    """Generate HMAC-SHA256 signature for play.fun API."""
    message = f"{method.lower()}\n{path.lower()}\n{timestamp}"
    return hmac.new(PLAYFUN_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _playfun_request(method: str, path: str, body: dict = None):
    """Make an authenticated request to the play.fun API."""
    timestamp = str(int(time.time() * 1000))
    sig = _playfun_hmac(method, path, timestamp)
    auth = f"HMAC-SHA256 apiKey={PLAYFUN_API_KEY}, signature={sig}, timestamp={timestamp}"
    url = f"{PLAYFUN_API_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth,
        "x-auth-provider": "hmac",
        "Content-Type": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:500]
        logger.error(f"play.fun API error {e.code}: {err_body}")
        return {"error": e.code, "message": err_body}
    except Exception as e:
        logger.error(f"play.fun API exception: {e}")
        return {"error": 500, "message": str(e)}


@app.post("/playfun/save-points")
async def playfun_save_points(request: Request):
    """
    Hybrid integration endpoint: client sends session token + points,
    server validates and saves via play.fun dev API.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    session_token = body.get("sessionToken", "").strip()
    points = body.get("points", 0)
    player_id = body.get("playerId", "").strip()  # sol:address or did:privy:xxx

    if not session_token:
        return JSONResponse(status_code=400, content={"error": "sessionToken required"})
    if not points or not isinstance(points, (int, float)) or points <= 0:
        return JSONResponse(status_code=400, content={"error": "Positive points value required"})
    if not player_id:
        return JSONResponse(status_code=400, content={"error": "playerId required (sol:address or wallet)"})

    points = int(points)

    # Step 1: Validate the session token with play.fun
    validate_result = _playfun_request("POST", "/play/dev/validate-session-token", {
        "sessionToken": session_token
    })

    if "error" in validate_result:
        logger.warning(f"Session validation failed: {validate_result}")
        return JSONResponse(status_code=401, content={
            "error": "Session validation failed",
            "detail": validate_result.get("message", "Unknown error")
        })

    if not validate_result.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Invalid session token"})

    validated_game = validate_result.get("gameId", "")
    validated_ogp = validate_result.get("ogpId", "")
    logger.info(f"Session validated: gameId={validated_game}, ogpId={validated_ogp}, points={points}")

    # Step 2: Save points via batch-save-points
    save_result = _playfun_request("POST", "/play/dev/batch-save-points", {
        "gameApiKey": PLAYFUN_GAME_ID,
        "points": [{
            "playerId": validated_ogp if validated_ogp else player_id,
            "points": str(points)
        }]
    })

    if "error" in save_result:
        logger.error(f"batch-save-points failed: {save_result}")
        return JSONResponse(status_code=502, content={
            "error": "Failed to save points to play.fun",
            "detail": save_result.get("message", "Unknown error")
        })

    logger.info(f"Points saved: {points} for {player_id} (ogpId={validated_ogp}), result={save_result}")
    return {
        "success": True,
        "points": points,
        "savedCount": save_result.get("savedCount", 0),
        "ogpId": validated_ogp,
        "message": f"Saved {points} points via server-side integration"
    }


@app.post("/playfun/save-points-direct")
async def playfun_save_points_direct(request: Request):
    """
    Direct server-side point save — for cases where session validation
    is not possible (e.g. player not logged in via Privy but identified by wallet).
    Uses wallet address directly with batch-save-points.
    Protected by admin key.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    admin_key = body.get("admin_key", "")
    if hashlib.sha256(admin_key.encode()).hexdigest() != ADMIN_KEY_HASH:
        return JSONResponse(status_code=403, content={"error": "Unauthorized"})

    player_id = body.get("playerId", "").strip()
    points = body.get("points", 0)

    if not player_id:
        return JSONResponse(status_code=400, content={"error": "playerId required"})
    if not points or not isinstance(points, (int, float)) or points <= 0:
        return JSONResponse(status_code=400, content={"error": "Positive points required"})

    save_result = _playfun_request("POST", "/play/dev/batch-save-points", {
        "gameApiKey": PLAYFUN_GAME_ID,
        "points": [{
            "playerId": player_id,
            "points": str(int(points))
        }]
    })

    if "error" in save_result:
        return JSONResponse(status_code=502, content={
            "error": "Failed to save points",
            "detail": save_result.get("message", "")
        })

    return {"success": True, "points": int(points), "result": save_result}


@app.get("/playfun/leaderboard")
def playfun_leaderboard():
    """Proxy the play.fun leaderboard."""
    result = _playfun_request("GET", f"/play/dev/leaderboard/{PLAYFUN_GAME_ID}")
    return result


# ══════════════════════════════════════════════
#  STATIC PAGES
# ══════════════════════════════════════════════
STATIC_DIR = Path(__file__).parent / "static"

@app.get("/vote-page")
def vote_page_redirect():
    """Redirect /vote-page to /vote-page/ so relative asset paths resolve correctly."""
    return RedirectResponse(url="/vote-page/", status_code=301)

@app.get("/vote-page/")
@app.get("/vote-page/{path:path}")
def serve_vote_page(path: str = "index.html"):
    if path == "" or path == "/":
        path = "index.html"
    file_path = STATIC_DIR / "vote" / path
    if file_path.exists() and file_path.is_file():
        media_types = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".png": "image/png"}
        suffix = file_path.suffix
        return FileResponse(file_path, media_type=media_types.get(suffix, "application/octet-stream"))
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.get("/dev")
def dev_page_redirect():
    """Redirect /dev to /dev/ so relative asset paths resolve correctly."""
    return RedirectResponse(url="/dev/", status_code=301)

@app.get("/dev/")
@app.get("/dev/{path:path}")
def serve_dev_page(path: str = "index.html"):
    if path == "" or path == "/":
        path = "index.html"
    file_path = STATIC_DIR / "dev" / path
    if file_path.exists() and file_path.is_file():
        media_types = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".png": "image/png"}
        suffix = file_path.suffix
        return FileResponse(file_path, media_type=media_types.get(suffix, "application/octet-stream"))
    return JSONResponse(status_code=404, content={"error": "Not found"})


# ══════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
