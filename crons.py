#!/usr/bin/env python3
"""
TAP RUSH — Cron Jobs (runs inside FastAPI via APScheduler)

Three scheduled tasks:
1. RUSH token monitor + deposit watcher  (every 6 hours)
2. Vote checker                          (twice daily: 9:10pm, 9:10am UTC)
3. Tournament end + payout cheat sheet   (daily at 11:00pm UTC / 3pm PST)

Notifications sent via Gmail SMTP.
"""

import json
import os
import time
import logging
import urllib.request
from datetime import datetime, timezone

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
DATA_DIR = os.environ.get("DATA_DIR", ".")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "player585@proton.me")

RUSH_MINT = "ZZdUjmm6stModTGwB7yQk9RphzbV6WYHMD5Wz7oPLAY"
DEPOSIT_ATA = "p7dB4kZFt1q7VxNd9wtNTFt7q39kiQBQTYYc4KbXXNg"
DEX_API = f"https://api.dexscreener.com/latest/dex/tokens/{RUSH_MINT}"
SOLANA_RPC = "https://solana-rpc.publicnode.com"
ADMIN_KEY = "taprush2026admin"

# File paths for persistent state
BASELINE_FILE = os.path.join(DATA_DIR, "cron_rush_baseline.json")
DEPOSITS_FILE = os.path.join(DATA_DIR, "cron_rush_deposits.json")
VOTE_COUNT_FILE = os.path.join(DATA_DIR, "cron_rush_votes.json")
CRON_LOG_FILE = os.path.join(DATA_DIR, "cron_log.json")

logger = logging.getLogger("taprush.crons")

# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════
def _read_json(path, default=None):
    """Read a JSON file, return default if missing/corrupt."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _write_json(path, data):
    """Write data to a JSON file."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _fetch_json(url, method="GET", body=None, timeout=20):
    """Fetch JSON from a URL."""
    try:
        if body:
            data_bytes = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Fetch error {url}: {e}")
        return None


def _solana_rpc(method, params):
    """Call Solana RPC."""
    return _fetch_json(SOLANA_RPC, body={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    })


def _log_cron(task_name, status, detail=""):
    """Append to cron log file for admin visibility."""
    log = _read_json(CRON_LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    log.append({
        "task": task_name,
        "status": status,
        "detail": detail[:500],
        "time": datetime.now(timezone.utc).isoformat()
    })
    # Keep last 200 entries
    if len(log) > 200:
        log = log[-200:]
    _write_json(CRON_LOG_FILE, log)


# ══════════════════════════════════════════════
#  EMAIL NOTIFICATION
# ══════════════════════════════════════════════
def send_email(subject, body_text):
    """Send an email notification via Resend HTTPS API.
    
    Railway Hobby plan blocks SMTP ports (25/465/587), so we use
    Resend's REST API over HTTPS instead. Free tier: 100 emails/day.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email notification")
        _log_cron("email", "skipped", "No RESEND_API_KEY env var")
        return False

    from_addr = os.environ.get("EMAIL_FROM", "TAP RUSH <onboarding@resend.dev>")

    try:
        html_body = body_text.replace("\n", "<br>")
        html = f"""<html><body style="font-family: monospace; font-size: 14px; padding: 16px; background: #111; color: #eee;">
        <h2 style="color: #e6b800;">{subject}</h2>
        <div>{html_body}</div>
        <hr style="margin-top: 24px; border-color: #333;">
        <p style="color: #888; font-size: 12px;">TAP RUSH Monitoring — Railway</p>
        </body></html>"""

        payload = json.dumps({
            "from": from_addr,
            "to": [NOTIFY_EMAIL],
            "subject": subject,
            "html": html,
            "text": body_text
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        logger.info(f"Email sent via Resend: {subject} (id={result.get('id','?')})")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        _log_cron("email", "error", str(e))
        return False


# ══════════════════════════════════════════════
#  CRON 1: RUSH TOKEN MONITOR + DEPOSIT WATCHER
# ══════════════════════════════════════════════
def cron_rush_monitor():
    """
    Runs every 6 hours.
    Part 1: Check RUSH token price/volume via DexScreener.
    Part 2: Check for new deposits to the ATA.
    """
    logger.info("=== CRON: RUSH Monitor starting ===")
    alerts = []

    # ── Part 1: Token Metrics ──
    try:
        dex_data = _fetch_json(DEX_API)
        baseline = _read_json(BASELINE_FILE, {})

        price = None
        market_cap = None
        volume_24h = None
        txns_buys = None
        txns_sells = None

        if dex_data and dex_data.get("pairs"):
            pair = dex_data["pairs"][0]
            price = float(pair.get("priceUsd") or 0)
            market_cap = float(pair.get("marketCap") or pair.get("fdv") or 0)
            volume_24h = float(pair.get("volume", {}).get("h24") or 0)
            txns = pair.get("txns", {}).get("h24", {})
            txns_buys = int(txns.get("buys") or 0)
            txns_sells = int(txns.get("sells") or 0)

            # Compare with baseline
            old_price = baseline.get("price_usd")
            old_volume = baseline.get("volume_24h")
            old_buys = baseline.get("txns_buys")

            # Volume spike check (> $50)
            if volume_24h and volume_24h > 50:
                alerts.append(f"VOLUME SPIKE: 24h volume is ${volume_24h:,.2f} (above $50 threshold)")

            # Price move check (10%+)
            if old_price and old_price > 0 and price:
                pct_change = ((price - old_price) / old_price) * 100
                if abs(pct_change) >= 10:
                    direction = "UP" if pct_change > 0 else "DOWN"
                    alerts.append(f"PRICE MOVE {direction}: {pct_change:+.1f}% (${old_price:.8f} -> ${price:.8f})")

            # New buys check
            if old_buys is not None and txns_buys is not None and txns_buys > old_buys:
                new_buys = txns_buys - old_buys
                alerts.append(f"NEW BUYS: {new_buys} new buy transactions (total: {txns_buys})")

        # Update baseline
        new_baseline = {
            "price_usd": price,
            "market_cap": market_cap,
            "volume_24h": volume_24h,
            "txns_buys": txns_buys,
            "txns_sells": txns_sells,
            "last_checked": datetime.now(timezone.utc).isoformat()
        }
        _write_json(BASELINE_FILE, new_baseline)

    except Exception as e:
        logger.error(f"Token monitor error: {e}")
        _log_cron("rush_monitor_token", "error", str(e))

    # ── Part 2: Deposit Monitoring ──
    try:
        rpc_result = _solana_rpc("getSignaturesForAddress", [DEPOSIT_ATA, {"limit": 20}])
        current_sigs = []
        if rpc_result and rpc_result.get("result"):
            current_sigs = rpc_result["result"]

        current_sig_ids = [s["signature"] for s in current_sigs]
        seen_data = _read_json(DEPOSITS_FILE, [])

        # Handle both formats: list of objects or list of strings
        if seen_data and isinstance(seen_data[0], dict):
            seen_sig_ids = [s.get("signature", "") for s in seen_data]
        elif seen_data and isinstance(seen_data[0], str):
            seen_sig_ids = seen_data
        else:
            seen_sig_ids = []

        new_sigs = [s for s in current_sig_ids if s not in seen_sig_ids]

        if not seen_sig_ids:
            # First run — set baseline, don't alert
            _write_json(DEPOSITS_FILE, current_sig_ids)
            _log_cron("rush_monitor_deposits", "baseline_set", f"{len(current_sig_ids)} signatures stored")
        elif new_sigs:
            # New deposits detected
            new_details = []
            for sig in current_sigs:
                if sig["signature"] in new_sigs:
                    bt = sig.get("blockTime", 0)
                    time_str = datetime.fromtimestamp(bt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if bt else "unknown"
                    new_details.append(f"  {sig['signature'][:20]}... at {time_str}")

            deposit_alert = f"NEW DEPOSITS: {len(new_sigs)} new transaction(s) on deposit ATA\n" + "\n".join(new_details)
            alerts.append(deposit_alert)

            # Update seen list
            _write_json(DEPOSITS_FILE, current_sig_ids)
        else:
            _log_cron("rush_monitor_deposits", "no_change", "No new deposits")

    except Exception as e:
        logger.error(f"Deposit monitor error: {e}")
        _log_cron("rush_monitor_deposits", "error", str(e))

    # ── Send notification if anything notable ──
    if alerts:
        subject = "TAP RUSH — Token/Deposit Alert"
        body = "RUSH Token Monitor Report\n"
        body += f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        body += "=" * 40 + "\n\n"
        body += "\n\n".join(alerts)

        if price is not None:
            body += f"\n\nCurrent Price: ${price:.8f}"
        if volume_24h is not None:
            body += f"\n24h Volume: ${volume_24h:,.2f}"

        send_email(subject, body)
        _log_cron("rush_monitor", "alert_sent", f"{len(alerts)} alerts")
    else:
        _log_cron("rush_monitor", "quiet", "No notable changes")

    logger.info(f"=== CRON: RUSH Monitor done — {len(alerts)} alerts ===")


# ══════════════════════════════════════════════
#  CRON 2: VOTE CHECKER
# ══════════════════════════════════════════════
def cron_vote_checker():
    """
    Runs twice daily.
    Checks for new votes on the TAP RUSH voting page.
    """
    logger.info("=== CRON: Vote Checker starting ===")

    try:
        # Use localhost since this runs inside the same FastAPI process
        # But we can also just query the DB directly
        # Let's use the Railway URL to keep it simple and testable
        api_base = os.environ.get("API_BASE_URL", "https://web-production-0b074.up.railway.app")
        vote_data = _fetch_json(f"{api_base}/vote/results?poll_id=sol-chart")

        if not vote_data:
            _log_cron("vote_checker", "error", "Could not fetch vote results")
            return

        current_total = vote_data.get("total_voters", 0)
        prev = _read_json(VOTE_COUNT_FILE, {"total_voters": 0})
        prev_total = prev.get("total_voters", 0)

        if current_total > prev_total:
            new_votes = current_total - prev_total

            subject = f"TAP RUSH — {new_votes} New Vote(s) Detected"
            body = f"Vote Checker Report\n"
            body += f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            body += "=" * 40 + "\n\n"
            body += f"NEW VOTERS: {new_votes}\n\n"
            body += f"YES: {vote_data.get('yes', 0):,.2f} RUSH weight ({vote_data.get('yes_voters', 0)} voters)\n"
            body += f"NO:  {vote_data.get('no', 0):,.2f} RUSH weight ({vote_data.get('no_voters', 0)} voters)\n"
            body += f"TOTAL: {vote_data.get('total', 0):,.2f} RUSH weight ({current_total} voters)\n"

            send_email(subject, body)
            _write_json(VOTE_COUNT_FILE, {"total_voters": current_total})
            _log_cron("vote_checker", "alert_sent", f"{new_votes} new votes, total={current_total}")
        else:
            _write_json(VOTE_COUNT_FILE, {"total_voters": current_total})
            _log_cron("vote_checker", "quiet", f"No new votes (total={current_total})")

    except Exception as e:
        logger.error(f"Vote checker error: {e}")
        _log_cron("vote_checker", "error", str(e))

    logger.info("=== CRON: Vote Checker done ===")


# ══════════════════════════════════════════════
#  CRON 3: TOURNAMENT END + PAYOUT CHEAT SHEET
# ══════════════════════════════════════════════
def cron_tournament_end():
    """
    Runs daily at 11:00pm UTC (3pm PST).
    Fetches dev leaderboard, sends payout cheat sheet, finalizes tournament.
    """
    logger.info("=== CRON: Tournament End starting ===")

    try:
        api_base = os.environ.get("API_BASE_URL", "https://web-production-0b074.up.railway.app")
        dev_data = _fetch_json(f"{api_base}/tournament/dev-leaderboard?key={ADMIN_KEY}")

        if not dev_data:
            _log_cron("tournament_end", "error", "Could not fetch dev leaderboard")
            send_email(
                "TAP RUSH Tournament — ERROR",
                "Failed to fetch dev leaderboard. Check Railway logs."
            )
            return

        tid = dev_data.get("tournament_id", "unknown")
        entries = dev_data.get("entries", 0)
        prize_pool = dev_data.get("prize_pool_display", "0 RUSH")
        payouts = dev_data.get("payouts", {})
        players = dev_data.get("players", [])

        subject = f"TAP RUSH Tournament Ended — {tid}"

        if entries == 0:
            body = f"Tournament: {tid}\n"
            body += f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            body += "=" * 40 + "\n\n"
            body += "No players today.\n"
        else:
            body = f"Tournament: {tid}\n"
            body += f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            body += "=" * 40 + "\n\n"
            body += f"Total Entries: {entries}\n"
            body += f"Prize Pool: {prize_pool}\n\n"
            body += "PAYOUT CHEAT SHEET\n"
            body += "-" * 40 + "\n"

            medals = {"1st": "1st", "2nd": "2nd", "3rd": "3rd"}
            for place in ["1st", "2nd", "3rd"]:
                p = payouts.get(place, {})
                addr = p.get("address") or "N/A"
                score = p.get("score", 0)
                payout_display = p.get("payout_display", "N/A")
                body += f"  {medals[place]}: {addr}\n"
                body += f"       Score: {score} pts — Send {payout_display}\n\n"

            body += "\nPLAYER SUMMARY\n"
            body += "-" * 40 + "\n"
            for p in players:
                addr = p.get("address", "?")
                best = p.get("best_score", 0)
                games = p.get("total_games", 0)
                deposits = p.get("deposits", 1)
                grade = p.get("best_grade", "")
                cheating = p.get("cheating", False)
                game_list = p.get("games", [])

                device = "desktop"
                session = 0
                if game_list:
                    device = game_list[0].get("device_type", "desktop")
                    session = game_list[0].get("session_time", 0)

                body += f"  {addr[:8]}...{addr[-4:]}\n"
                body += f"    Best: {best} ({grade}) | Games: {games}/{deposits}"
                if cheating:
                    body += " [CHEATING FLAG]"
                body += f"\n    Device: {device} | Session: {session}s\n\n"

        # Always send notification
        send_email(subject, body)
        _log_cron("tournament_end", "notified", f"tid={tid}, entries={entries}")

        # Finalize the tournament
        finalize_result = _fetch_json(
            f"{api_base}/tournament/admin/finalize",
            method="POST",
            body={"admin_key": ADMIN_KEY, "tournament_id": tid}
        )
        if finalize_result and finalize_result.get("success"):
            _log_cron("tournament_end", "finalized", f"tid={tid}")
            logger.info(f"Tournament {tid} finalized")
        else:
            error_msg = str(finalize_result) if finalize_result else "No response"
            _log_cron("tournament_end", "finalize_warn", error_msg)
            logger.warning(f"Tournament finalize issue: {error_msg}")

    except Exception as e:
        logger.error(f"Tournament end error: {e}")
        _log_cron("tournament_end", "error", str(e))
        send_email(
            "TAP RUSH Tournament — ERROR",
            f"Tournament end cron failed:\n{str(e)}"
        )

    logger.info("=== CRON: Tournament End done ===")


# ══════════════════════════════════════════════
#  SCHEDULER SETUP (called from main.py)
# ══════════════════════════════════════════════
_scheduler = None

def start_scheduler():
    """Initialize and start APScheduler with all cron jobs."""
    global _scheduler

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Cron 1: RUSH monitor — every 6 hours at :14 past
    # (matches old schedule: 14 2,8,14,20 * * *)
    _scheduler.add_job(
        cron_rush_monitor,
        CronTrigger(minute=14, hour="2,8,14,20"),
        id="rush_monitor",
        name="RUSH Token Monitor + Deposits",
        replace_existing=True,
        misfire_grace_time=300
    )

    # Cron 2: Vote checker — twice daily
    # (matches old schedule: 10 5,17 * * *)
    _scheduler.add_job(
        cron_vote_checker,
        CronTrigger(minute=10, hour="5,17"),
        id="vote_checker",
        name="Vote Checker",
        replace_existing=True,
        misfire_grace_time=300
    )

    # Cron 3: Tournament end — daily at 23:00 UTC (3pm PST)
    # (matches old schedule: 0 23 * * *)
    _scheduler.add_job(
        cron_tournament_end,
        CronTrigger(minute=0, hour=23),
        id="tournament_end",
        name="Tournament End + Payout",
        replace_existing=True,
        misfire_grace_time=300
    )

    _scheduler.start()
    logger.info("APScheduler started with 3 cron jobs")
    return _scheduler


def stop_scheduler():
    """Shutdown the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


def get_scheduler_status():
    """Return status of all scheduled jobs."""
    if not _scheduler:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.isoformat() if next_run else None,
            "trigger": str(job.trigger)
        })
    return {"running": _scheduler.running, "jobs": jobs}
