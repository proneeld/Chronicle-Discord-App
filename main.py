import sys
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests  # type: ignore
if sys.platform == "win32":
    # Use the SelectorEventLoop instead of the ProactorEventLoop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv  # type: ignore
import discord  # type: ignore
from discord.commands import Option  # type: ignore
from discord.ext import commands, tasks  # type: ignore
import pytz  # type: ignore  # pip install pytz
from keep_alive import keep_alive

# Configuration
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

_admin_user = os.getenv("ADMIN_USER")
try:
    ADMIN_USER_ID: Optional[int] = int(_admin_user) if _admin_user else None
except ValueError:
    ADMIN_USER_ID = None
    print("Warning: ADMIN_USER must be a numeric Discord user ID.")

# The API README recommends V2. Set VLR_API_BASE_URL to your self-hosted
# root (for example, http://127.0.0.1:3001) or directly to a /v2 URL.
_api_root = os.getenv("VLR_API_BASE_URL", "https://vlrggapi.vercel.app").rstrip("/")
VLR_API_BASE_URL = _api_root if _api_root.endswith("/v2") else f"{_api_root}/v2"
VLR_API_TIMEOUT_SECONDS = 15

keep_alive()
TZ = pytz.timezone("America/Phoenix")
# End configuration

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True  # so we can see who’s in voice channels

bot = discord.Bot(intents=intents)


# Global Database (only one meeting at a time)
# Only schedule one meeting, scheduling another one overwrites
meeting = {
    "scheduled_time": None,        # datetime (America/Phoenix) when we check the voice channel
    "voice_channel_id": None,      # int ID of the voice channel
    "participants": set(),         # set of user IDs (ints)
    "lateness_counts": {},         # dict { user_id: int, … } accumulated across meetings
    "processed": False,            # once we've checked attendance, set True
    "reminder_5_sent": False,      # once we've sent the 5-minute reminder
    "text_channel_id": None        # ID of the text channel where !schedule was invoked
}
# ──────────────────────────────────────────────────────────────────────────────

# DATABASE STUFF
# This bot uses an on-disk SQLite database to keep user balances and
# outstanding bets to track money even when bot is offline. Balances are
# tracked in a "balances" table, and bets on upcoming matches are tracked
# in a separate "bets" table. The tables are created on startup if they do not exist.

# Path to the SQLite database file. It will live alongside this script.
DATABASE_FILE = os.path.join(os.path.dirname(__file__), "balances.db")

# Starting amount of points each new user receives.
STARTING_BALANCE = 1000

# Daily bonus configuration. If a user's balance drops below
# DAILY_BONUS_THRESHOLD, they will automatically receive DAILY_BONUS_AMOUNT
# points once every 24 hours (DAILY_BONUS_INTERVAL seconds) when they query
# their balance or participate in a bet.
DAILY_BONUS_THRESHOLD = 100
DAILY_BONUS_AMOUNT = 20
DAILY_BONUS_INTERVAL = 24 * 60 * 60  # seconds in a day


def init_db() -> None:
    """Make SQLite databse and make sure it exists"""
    conn = sqlite3.connect(DATABASE_FILE)
    with conn:
        # Table for user balances
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL,
                last_daily_bonus INTEGER DEFAULT 0
            )
            """
        )
        # Table for bets
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_page TEXT NOT NULL,
                match_event TEXT,
                team1 TEXT NOT NULL,
                team2 TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                team_bet TEXT NOT NULL,
                amount INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                start_notified INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0
            )
            """
        )
    conn.close()


def _maybe_apply_daily_bonus(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """
    Apply a daily bonus using the caller's existing transaction. Reusing the
    same connection avoids an unnecessary nested SQLite connection and reduces
    the chance of database-lock errors.
    """
    current_time = int(datetime.now().timestamp())
    balance = row["balance"]
    last_bonus = row["last_daily_bonus"] or 0
    if balance < DAILY_BONUS_THRESHOLD and (current_time - last_bonus) >= DAILY_BONUS_INTERVAL:
        balance += DAILY_BONUS_AMOUNT
        conn.execute(
            "UPDATE balances SET balance = ?, last_daily_bonus = ? WHERE user_id = ?",
            (balance, current_time, row["user_id"]),
        )
    return balance


def get_balance(user_id: int) -> int:
    """
    Gets a user's current balance from the database, creating a new record
    if necessary and applying any eligible daily bonus.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.execute("SELECT * FROM balances WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            # Initialize new user
            conn.execute(
                "INSERT INTO balances (user_id, balance, last_daily_bonus) VALUES (?, ?, 0)",
                (user_id, STARTING_BALANCE),
            )
            balance = STARTING_BALANCE
        else:
            balance = _maybe_apply_daily_bonus(conn, row)
    conn.close()
    return balance


def update_balance(user_id: int, new_balance: int) -> None:
    """Set a user's balance to a new value."""
    conn = sqlite3.connect(DATABASE_FILE)
    with conn:
        conn.execute(
            "INSERT INTO balances (user_id, balance, last_daily_bonus) VALUES (?, ?, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
            (user_id, new_balance),
        )
    conn.close()


def get_leaderboard(limit: int = 5) -> List[Tuple[int, int]]:
    """
    Return a list of (user_id, balance) tuples for the top balances.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.execute(
            "SELECT user_id, balance FROM balances ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    conn.close()
    return [(row["user_id"], row["balance"]) for row in rows]


def get_rank_and_balance(user_id: int) -> Tuple[int, int]:
    """
    Compute a user's rank (1-indexed) and return a tuple of (rank, balance).
    This will also ensure the user exists in the database.
    """
    balance = get_balance(user_id)
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS higher FROM balances WHERE balance > ?",
            (balance,),
        )
        higher_count = cur.fetchone()["higher"]
    conn.close()
    return higher_count + 1, balance


def _normalize_match_page(match_page: str) -> str:
    """
    Normalize a match_page string so that different representations of the same
    match (full URL vs. path) compare equal. Always returns just the path.
    """
    if not match_page:
        return match_page
    # If it's a full URL, extract the path
    if match_page.startswith("http"):
        try:
            return urlparse(match_page).path
        except Exception:
            return match_page
    return match_page


def place_bet(
    match_page: str,
    match_event: str,
    team1: str,
    team2: str,
    user_id: int,
    team_bet: str,
    amount: int,
    channel_id: int,
) -> bool:
    """Atomically verify funds, deduct the wager, and persist the bet."""
    mp = _normalize_match_page(match_page)
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM balances WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            balance = STARTING_BALANCE
            conn.execute(
                "INSERT INTO balances (user_id, balance, last_daily_bonus) VALUES (?, ?, 0)",
                (user_id, balance),
            )
        else:
            balance = _maybe_apply_daily_bonus(conn, row)

        if amount <= 0 or amount > balance:
            conn.commit()
            return False

        conn.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.execute(
            """
            INSERT INTO bets (
                match_page, match_event, team1, team2, user_id, team_bet,
                amount, channel_id, start_notified, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (mp, match_event, team1, team2, user_id, team_bet, amount, channel_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        conn.rollback()
        print(f"Could not place bet: {exc}")
        return False
    finally:
        conn.close()


def get_open_bets() -> List[Dict]:
    """
    Fetch all bets that have not yet been resolved. Returns a list of
    dictionaries with keys corresponding to the bets table columns.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.execute(
            "SELECT * FROM bets WHERE resolved = 0",
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def mark_start_notified(match_page: str) -> None:
    """Mark all bets for a match as having been notified of the start."""
    mp = _normalize_match_page(match_page)
    conn = sqlite3.connect(DATABASE_FILE)
    with conn:
        conn.execute(
            "UPDATE bets SET start_notified = 1 WHERE match_page = ? AND start_notified = 0",
            (mp,),
        )
    conn.close()


def resolve_bets(match_page: str, winning_team: str) -> Tuple[List[Dict], List[Dict]]:
    """
    Resolve all outstanding bets in one SQLite transaction. Winners receive
    double their wager because the original wager was deducted when placed.
    """
    mp = _normalize_match_page(match_page)
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    winners: List[Dict] = []
    losers: List[Dict] = []
    with conn:
        bet_rows = conn.execute(
            "SELECT * FROM bets WHERE match_page = ? AND resolved = 0",
            (mp,),
        ).fetchall()

        for row in bet_rows:
            bet = dict(row)
            if bet["team_bet"] == winning_team:
                winners.append(bet)
                payout = bet["amount"] * 2
                conn.execute(
                    """
                    INSERT INTO balances (user_id, balance, last_daily_bonus)
                    VALUES (?, ?, 0)
                    ON CONFLICT(user_id) DO UPDATE
                    SET balance = balances.balance + excluded.balance
                    """,
                    (bet["user_id"], payout),
                )
            else:
                losers.append(bet)

        conn.execute(
            "UPDATE bets SET resolved = 1 WHERE match_page = ? AND resolved = 0",
            (mp,),
        )
    conn.close()
    return winners, losers


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    init_db()

    # on_ready can run again after Discord reconnects, so only start each loop once.
    if not meeting_watcher.is_running():
        meeting_watcher.start()
    if not bet_watcher.is_running():
        bet_watcher.start()


@tasks.loop(seconds=30)
async def meeting_watcher():
    """
    Runs every 30 seconds and does two things when meeting["scheduled_time"] is set:
      1) If now ≥ scheduled_time – 5min and 5-minute reminder not yet sent, send it.
      2) If now ≥ scheduled_time and not yet processed, check attendance and warn absentees.
    """
    if meeting["scheduled_time"] is None:
        return

    now_pst = datetime.now(tz=TZ)
    scheduled: datetime = meeting["scheduled_time"]

    # 1) Five-minute reminder
    five_minute_mark = scheduled - timedelta(minutes=5)
    if (not meeting["reminder_5_sent"]) and (now_pst >= five_minute_mark) and (now_pst < scheduled):
        channel = bot.get_channel(meeting["text_channel_id"])
        if isinstance(channel, discord.TextChannel):
            mentions = " ".join(f"<@{uid}>" for uid in meeting["participants"])
            # Find the voice-channel name
            vc = None
            for g in bot.guilds:
                cand = g.get_channel(meeting["voice_channel_id"])
                if isinstance(cand, discord.VoiceChannel):
                    vc = cand
                    break
            vc_name = vc.name if vc else f"(ID {meeting['voice_channel_id']})"
            await channel.send(
                f"{mentions}\n⏰ **5-Minute Reminder:** Meeting in **{vc_name}** in 5 minutes! Please be ready my niggas!"
            )
        meeting["reminder_5_sent"] = True

    # 2) On-time attendance check
    if meeting["processed"]:
        return

    if now_pst < scheduled:
        return  # not yet time to check attendance

    # It’s time to check attendance
    voice_chan = None
    for guild in bot.guilds:
        ch = guild.get_channel(meeting["voice_channel_id"])
        if isinstance(ch, discord.VoiceChannel):
            voice_chan = ch
            break

    if voice_chan is None:
        # Voice channel was deleted or not found; mark processed and exit
        meeting["processed"] = True
        return

    # Who is currently in that VoiceChannel idk lol
    connected_member_ids = {member.id for member in voice_chan.members}

    # Of the scheduled participants, who is absent hopefully not anyone :(
    absent_ids = meeting["participants"] - connected_member_ids

    to_ping = []
    for user_id in absent_ids:
        meeting["lateness_counts"].setdefault(user_id, 0)
        meeting["lateness_counts"][user_id] += 1

        # If this is the second time they’ve missed, add them to “to_ping”
        # these guys are losers
        if meeting["lateness_counts"][user_id] == 2:
            to_ping.append(user_id)

    # Ping everyone who just hit a lateness_count of 2
    if to_ping:
        text_chan = bot.get_channel(meeting["text_channel_id"])
        if isinstance(text_chan, discord.TextChannel):
            mentions = " ".join(f"<@{uid}>" for uid in to_ping)
            await text_chan.send(
                f"{mentions} – How hard is it to join the vc on a certain time twice a week you fucking retard. Do this shit another time "
                f"and you're going to get IP Banned :3"
            )

    meeting["processed"] = True  # so we don’t check this same meeting again


@meeting_watcher.before_loop
async def before_meeting_watcher():
    await bot.wait_until_ready()

# COMMAND: /commands 
@bot.slash_command(name="commands", description="List of commands")
async def list_commands(ctx):
    await ctx.respond(f"- **/schedule**: Schedule a voice channel meeting.\n"
                      f"- **/list**: Lists currently schedule meeting (if any, if meeting was in the past it will be deleted\n\n"
                      f"- **/warnings**: Shows only the users that have been warned for being late to a VC meeting\n\n"
                      f"- **/reset_lateness**: (ADMIN ONLY) Resets warnings given to all users\n\n"
                      f"- **/regionranks**: Gets the top 5 teams in the specified region\n"
                      f"- **/recentmatches**: Gets the most recent matches from each event\n"
                      f"- **/upcomingmatches**: Gets upcoming matches for each event\n"
                      f"- **/livescore**: Gets live score for ongoing games")
# COMMAND: !schedule
@bot.slash_command(name="schedule", description="Schedule a voice-channel meeting in Arizona time (MST)")
async def schedule(
    ctx: discord.ApplicationContext,
    date: Option(str, "Date in YYYY-MM-DD", required=True), # type: ignore
    time: Option(str, "Time in 24H, HH:MM", required=True), # type: ignore
    voice_channel: Option(discord.VoiceChannel, "Voice Channel", required=True), # type: ignore
    participant1: Option(discord.Member, "Participant 1", required=False) = None, # type: ignore
    participant2: Option(discord.Member, "Participant 2", required=False) = None, # type: ignore
    participant3: Option(discord.Member, "Participant 3", required=False) = None, # type: ignore
    participant4: Option(discord.Member, "Participant 4", required=False) = None, # type: ignore
    participant5: Option(discord.Member, "Participant 5", required=False) = None,): # type: ignore
    """
    Schedule a meeting.
      date_str: "YYYY-MM-DD"
      time_str: "HH:MM" (24-hour, in Arizona time)
      voice_channel: a voice-channel mention (e.g. #General-Voice)
      mentions: list of @users who must join
    """
    # 1) Parse date and time in America/Phoenix
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        dt_pst = TZ.localize(naive)
    except ValueError:
        await ctx.send("❌ Incorrect date and time format. Please use `YYYY-MM-DD HH:MM` in 24-hour Arizona time.")
        return

    now_pst = datetime.now(tz=TZ)
    if dt_pst <= now_pst:
        await ctx.send("❌ You must choose a future date/time in Arizona time.")
        return

    members = [m for m in [participant1, participant2, participant3, participant4, participant5] if m]
    if not members:
        await ctx.send("❌ Please add some friends to remind to join the VC!")
        return

    # Overwrite the existing meeting with this new one
    meeting["scheduled_time"]  = dt_pst
    meeting["voice_channel_id"] = voice_channel.id
    meeting["participants"]    = {m.id for m in members}
    meeting["processed"]       = False
    meeting["reminder_5_sent"] = False
    # Keep any existing lateness_counts so they accumulate across meetings
    meeting["text_channel_id"] = ctx.channel.id

    human_time = dt_pst.strftime("%Y-%m-%d %H:%M MST")
    human_list = " ".join(m.mention for m in members)

    await ctx.respond(
        f"✅ Ya'll better pull up at **{human_time}** in **{voice_channel.name}**.\n"
        f"Participants: {human_list}\n\n"
        f"I will send a 5-minute reminder, then check the vc when it's time. Missing twice earns you a warning"
    )


# ─── COMMAND: !list ────────────────────────────────────────────────────────────
@bot.slash_command(name="list", description="List the currently scheduled meeting (if any).")
async def list_meeting(ctx: discord.ApplicationContext):
    # If there's no scheduled_time at all, immediately say "no meeting"
    if meeting["scheduled_time"] is None:
        await ctx.respond("ℹ️ There is currently **no** meeting scheduled.")
        return

    now_pst = datetime.now(tz=TZ)
    scheduled: datetime = meeting["scheduled_time"]

    # If the scheduled time is already in the past, “age it out”:
    if scheduled < now_pst:
        # Clear all meeting fields except lateness_counts
        meeting["scheduled_time"]  = None
        meeting["voice_channel_id"] = None
        meeting["participants"]     = set()
        meeting["processed"]        = False
        meeting["reminder_5_sent"]  = False
        meeting["text_channel_id"]  = None

        await ctx.respond("ℹ️ The previous meeting has passed and has been removed from the list. No meeting is scheduled now.")
        return

    # Otherwise, it’s still a future meeting. Show its details:
    human_time = scheduled.strftime("%Y-%m-%d %H:%M MST")

    # Find the voice-channel name
    voice_chan = None
    for guild in bot.guilds:
        ch = guild.get_channel(meeting["voice_channel_id"])
        if isinstance(ch, discord.VoiceChannel):
            voice_chan = ch
            break
    vc_name = voice_chan.name if voice_chan else f"(ID {meeting['voice_channel_id']} – not found)"

    part_mentions = " ".join(f"<@{uid}>" for uid in meeting["participants"])
    lateness_summary = []
    for uid in meeting["participants"]:
        count = meeting["lateness_counts"].get(uid, 0)
        lateness_summary.append(f"<@{uid}>: {count} absence{'s' if count != 1 else ''}")

    await ctx.respond(
        f"📅 **Scheduled meeting:** {human_time}\n"
        f"📢 **Voice channel:** {vc_name}\n"
        f"👥 **Participants:** {part_mentions}\n"
        f"🕑 **Absence counts so far:**\n• " + "\n• ".join(lateness_summary)
    )


# COMMAND: /warnings
@bot.slash_command(name="warnings", description="Show only those users who have already been warned (lateness ≥ 1).")
async def warnings(ctx: discord.ApplicationContext):
    warned = [uid for uid, cnt in meeting["lateness_counts"].items() if cnt >= 1]

    if not warned:
        await ctx.respond("✅ No users have received a warning yet.")
        return

    lines = []
    for uid in warned:
        count = meeting["lateness_counts"][uid]
        lines.append(f"<@{uid}>: {count} absences")

    await ctx.respond(
        "**Users with warnings (absences ≥ 1):**\n" +
        "\n".join(lines)
    )


# (Optional) COMMAND: /reset_lateness
@bot.slash_command(name="reset_lateness", description="(Admin only) Reset all lateness counts to zero.")
@commands.has_permissions(administrator=True)
async def reset_lateness(ctx: discord.ApplicationContext):
    meeting["lateness_counts"].clear()
    await ctx.respond("✅ All lateness counts have been reset to zero.")


# VCT event names are kept in one place so match commands and betting stay in sync.
VCT_TIER_1_EVENTS = (
    "VCT 2026: Americas Kickoff",
    "VCT 2026: EMEA Kickoff",
    "VCT 2026: Pacific Kickoff",
    "VCT 2026: China Kickoff",
    "Valorant Masters Santiago 2026",
    "VCT 2026: Pacific Stage 1",
    "VCT 2026: Americas Stage 1",
    "VCT 2026: EMEA Stage 1",
    "VCT 2026: China Stage 1",
    "Valorant Masters London 2026",
    "VCT 2026: Pacific Stage 2",
    "VCT 2026: Americas Stage 2",
    "VCT 2026: EMEA Stage 2",
    "VCT 2026: China Stage 2",
    "Valorant Champions 2026",
)


def _request_vlr_api_sync(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Perform one V2 API request with a timeout and useful error logging."""
    url = f"{VLR_API_BASE_URL}/{endpoint.lstrip('/')}"
    try:
        response = requests.get(url, params=params, timeout=VLR_API_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        print(f"VLR API request failed for {url}: {exc}")
        return None
    except ValueError as exc:
        print(f"VLR API returned invalid JSON for {url}: {exc}")
        return None

    if not isinstance(payload, dict) or payload.get("status") != "success":
        print(f"VLR API returned an unexpected V2 response for {url}: {payload}")
        return None
    return payload


async def _request_vlr_api(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Run blocking requests work off the Discord event loop."""
    return await asyncio.to_thread(_request_vlr_api_sync, endpoint, params)


def _extract_segments(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract the standardized V2 data.segments list safely."""
    if not payload:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    segments = data.get("segments")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


async def get_regionranks_info(region: str) -> Optional[Dict[str, Any]]:
    return await _request_vlr_api("rankings", {"region": region})


async def get_matches_info(query: str) -> Optional[Dict[str, Any]]:
    return await _request_vlr_api("match", {"q": query})


async def get_recent_match() -> Optional[Dict[str, Any]]:
    return await get_matches_info("results")


async def get_upcoming_match() -> Optional[Dict[str, Any]]:
    return await get_matches_info("upcoming")


async def get_live_score() -> Optional[Dict[str, Any]]:
    return await get_matches_info("live_score")


def _round_val(value: Any) -> int:
    if value in (None, "", "N/A"):
        return 0
    return _safe_int(value)


def _vlr_match_url(match_page: Any) -> str:
    if not isinstance(match_page, str) or not match_page:
        return "https://www.vlr.gg"
    if match_page.startswith(("http://", "https://")):
        return match_page
    return f"https://www.vlr.gg/{match_page.lstrip('/')}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_event_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    normalized = " ".join(name.strip().casefold().split())
    if normalized.startswith("valorant champions tour "):
        normalized = "vct " + normalized.removeprefix("valorant champions tour ")
    elif normalized.startswith("champions tour "):
        normalized = "vct " + normalized.removeprefix("champions tour ")
    return normalized


VCT_TIER_1_EVENT_KEYS = {_normalize_event_name(name) for name in VCT_TIER_1_EVENTS}


def _ordered_tier_one_matches(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return at most one match per configured event in display order."""
    first_by_event: Dict[str, Dict[str, Any]] = {}
    for segment in segments:
        event_name = segment.get("match_event") or segment.get("tournament_name")
        key = _normalize_event_name(event_name)
        if key in VCT_TIER_1_EVENT_KEYS and key not in first_by_event:
            first_by_event[key] = segment

    return [
        first_by_event[key]
        for event_name in VCT_TIER_1_EVENTS
        if (key := _normalize_event_name(event_name)) in first_by_event
    ]


def _chunk_blocks(blocks: List[str], limit: int = 1900) -> List[str]:
    """Group formatted blocks without exceeding Discord's message limit."""
    chunks: List[str] = []
    current = ""
    for block in blocks:
        block = block.strip()
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
        if len(block) <= limit:
            current = block
        else:
            chunks.extend(block[i:i + limit] for i in range(0, len(block), limit))
            current = ""

    if current:
        chunks.append(current)
    return chunks


async def _respond_with_blocks(ctx: discord.ApplicationContext, blocks: List[str]) -> None:
    chunks = _chunk_blocks(blocks)
    if not chunks:
        return
    await ctx.respond(chunks[0])
    for chunk in chunks[1:]:
        await ctx.send(chunk)


# COMMAND: /regionranks
@bot.slash_command(name="regionranks", description="Filters to show only the major teams in each region, sorted by rank.")
async def regionranks(
    ctx: discord.ApplicationContext,
    region: Option(str, "Region code", required=True, choices=[
        "na", "la", "la-s", "la-n", "cn", "eu", "ap", "kr", "jp"
    ]),  # type: ignore
):
    region_key = region.lower()
    if region_key in ("na", "la", "la-s", "la-n"):
        whitelist = {
            "100 Thieves", "Cloud9", "Evil Geniuses", "FURIA", "KRÜ Esports",
            "Leviatán", "LOUD", "MIBR", "NRG", "Sentinels", "G2 Esports", "ENVY",
        }
    elif region_key == "cn":
        whitelist = {
            "All Gamers", "Bilibili Gaming", "EDward Gaming", "FunPlus Phoenix",
            "JDG Esports", "Nova Esports", "Titan Esports Club", "Trace Esports",
            "TYLOO", "Wolves Esports", "Dragon Ranger Gaming", "Xi Lai Gaming",
        }
    elif region_key == "eu":
        whitelist = {
            "FNATIC", "BBL Esports", "FUT Esports", "Karmine Corp", "Gentle Mates",
            "Natus Vincere", "Team Heretics", "Team Liquid", "Team Vitality", "GIANTX",
            "ULF Esports", "PCIFIC Esports",
        }
    elif region_key in ("ap", "kr", "jp"):
        whitelist = {
            "DetonatioN FocusMe", "DRX", "Gen.G", "Global Esports", "Paper Rex",
            "Rex Regum Qeon", "T1", "VARREL", "Team Secret", "ZETA DIVISION",
            "Nongshim RedForce", "FULL SENSE",
        }
    else:
        return await ctx.respond("❌ Please select a valid region.", ephemeral=True)

    payload = await get_regionranks_info(region_key)
    if payload is None:
        return await ctx.respond("❌ Could not fetch ranking data from the V2 API.")
    segments = _extract_segments(payload)
    if not segments:
        return await ctx.respond("❌ The V2 API returned no ranking data for that region.")

    filtered = [team for team in segments if team.get("team") in whitelist]
    filtered.sort(key=lambda team: _safe_int(team.get("rank"), 999999))

    if not filtered:
        return await ctx.respond("❌ None of the requested teams were found in the ranking data.")

    blocks = []
    for team in filtered:
        last_played = team.get("last_played_team") or team.get("last_played") or "Unknown"
        blocks.append(
            f"**Rank {team.get('rank', '?')} – {team.get('team', 'Unknown team')}**\n"
            f"Last played: {last_played}\n"
            f"Record: {team.get('record', 'N/A')}\n"
            f"Earnings: {team.get('earnings', 'N/A')}"
        )
    await _respond_with_blocks(ctx, blocks)


# COMMAND: /recentmatches
@bot.slash_command(name="recentmatches", description="Gets the results of the recent matches.")
async def recentmatch_cmd(ctx: discord.ApplicationContext):
    payload = await get_recent_match()
    if payload is None:
        return await ctx.respond("❌ Could not fetch match data from the V2 API.")
    segments = _extract_segments(payload)
    matches = _ordered_tier_one_matches(segments)
    if not matches:
        return await ctx.respond("❌ No recent results found for 2026 VCT Tier 1 matches.")

    blocks = [
        f"**{match.get('tournament_name', 'Unknown event')}**\n"
        f"**{match.get('round_info', 'Unknown round')}**\n"
        f"**{match.get('team1', 'TBD')} vs. {match.get('team2', 'TBD')}**\n"
        f"**Final Score:** {match.get('score1', '?')} - {match.get('score2', '?')}\n"
        f"Game happened {match.get('time_completed', 'recently')}\n"
        f"vlr.gg link: {_vlr_match_url(match.get('match_page'))}"
        for match in matches
    ]
    await _respond_with_blocks(ctx, blocks)


# COMMAND: /upcomingmatches
@bot.slash_command(name="upcomingmatches", description="Gets upcoming VCT Tier 1 matches from all regions")
async def upcomingmatches_cmd(ctx: discord.ApplicationContext):
    payload = await get_upcoming_match()
    if payload is None:
        return await ctx.respond("❌ Could not fetch match data from the V2 API.")
    segments = _extract_segments(payload)
    matches = _ordered_tier_one_matches(segments)
    if not matches:
        return await ctx.respond(
            "❌ No upcoming matches found for 2026 VCT Tier 1 events. The games may be too far in the future."
        )

    blocks = [
        f"**Upcoming game for {match.get('match_event', 'Unknown event')}**\n"
        f"**{match.get('match_series', 'Unknown series')}**\n"
        f"**{match.get('team1', 'TBD')} vs. {match.get('team2', 'TBD')}**\n"
        f"Game is **{match.get('time_until_match', 'TBD')}**\n"
        f"vlr.gg link: {_vlr_match_url(match.get('match_page'))}"
        for match in matches
    ]
    await _respond_with_blocks(ctx, blocks)


# COMMAND: /livescore
@bot.slash_command(name="livescore", description="Gets live score for VCT Tier 1 matches")
async def matches(ctx: discord.ApplicationContext):
    payload = await get_live_score()
    if payload is None:
        return await ctx.respond("❌ Could not fetch live match data from the V2 API.")
    segments = _extract_segments(payload)
    live_matches = _ordered_tier_one_matches(segments)
    if not live_matches:
        return await ctx.respond("❌ No ongoing matches. Use /upcomingmatches to see the next one.")

    blocks = []
    for match in live_matches:
        team1_map_total = _round_val(match.get("team1_round_ct")) + _round_val(match.get("team1_round_t"))
        team2_map_total = _round_val(match.get("team2_round_ct")) + _round_val(match.get("team2_round_t"))
        team1 = match.get("team1", "Team 1")
        team2 = match.get("team2", "Team 2")
        blocks.append(
            f"**{match.get('match_event', 'Unknown event')}** • **{match.get('match_series', 'Unknown series')}**\n"
            f"**Series:** {team1} {match.get('score1', '?')} - {match.get('score2', '?')} {team2}\n"
            f"**Current Map:** {match.get('current_map', 'Unknown')}\n"
            f"**Current Score:** {team1} {team1_map_total} - {team2_map_total} {team2}\n"
            f"vlr.gg link: {_vlr_match_url(match.get('match_page'))}"
        )
    await _respond_with_blocks(ctx, blocks)


# COMMAND: /valgamble
# BASE COMMAND WILL BE INDIVIDUAL BETTING, TWICE AMOUNT GIVEN IF WON, NOTHING GIVEN IF LOSS
# 1000 POINTS GIVEN TO EACH PLAYER, IF POINT COUNT GETS BELOW 100 THEN 20 IS GIVEN PER DAY
# BOT UPDATE IDEAS: WIN MULTIPLIER BASED ON AMOUNT OF PEOPLE BETTING ON ONE EVENT; ODDS FOR WIN AMOUNT

# COMMAND: /balance
@bot.slash_command(name="balance", description="Display your current points balance.")
async def balance_command(ctx: discord.ApplicationContext):
    """Respond with the caller's current balance, creating an account if needed."""
    bal = get_balance(ctx.author.id)
    await ctx.respond(f"💰 <@{ctx.author.id}>, your current balance is **{bal}** points.")

# COMMAND: /setmoney
@bot.slash_command(name="setmoney", description="Admin command to edit a user's balance.")
async def setmoney(
    ctx: discord.ApplicationContext,
    user: Option(discord.Member, "User to modify", required=True), # type: ignore
    amount: Option(int, "New balance amount", required=True) # type: ignore
):
    # Only allow the specific Discord ID
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.respond("❌ You are not allowed to use this command.", ephemeral=True)
        return

    update_balance(user.id, amount)

    await ctx.respond(
        f"💰 Balance for {user.mention} has been set to **{amount}** points."
    )

# COMMAND: /leaderboard
@bot.slash_command(name="leaderboard", description="Show the top 5 richest users and your rank.")
async def leaderboard_command(ctx: discord.ApplicationContext):
    top = get_leaderboard(5)
    lines = []
    for idx, (uid, bal) in enumerate(top, start=1):
        lines.append(f"{idx}. <@{uid}> — {bal} points")
    rank, bal = get_rank_and_balance(ctx.author.id)
    caller_in_top = any(uid == ctx.author.id for uid, _ in top)
    if caller_in_top:
        footer = f"\n\nYou are **#{rank}** with **{bal}** points and appear in the list above."
    else:
        footer = f"\n\nYou are **#{rank}** with **{bal}** points."
    await ctx.respond(f"🏆 **Leaderboard** 🏆\n" + "\n".join(lines) + footer)


# COMMAND: /gamble
@bot.slash_command(name="gamble", description="Gamble on the next VCT match by choosing a team.")
async def gamble_command(
    ctx: discord.ApplicationContext,
    amount: Option(int, "The number of points you want to wager (-1 for all-in)", required=True)  # type: ignore
):
    """
    Handle a gambling interaction where the user first confirms their intent,
    then selects which team will win the upcoming match. Bets are stored in
    the database and processed automatically when the match starts/finishes.
    """
    user_id = ctx.author.id

    # Get current balance first
    bal = get_balance(user_id)

    # Special case: -1 means all-in
    if amount == -1:
        amount = bal

    # Reject invalid amounts
    if amount <= 0:
        return await ctx.respond("❌ The amount must be a positive integer, or use **-1** to go all-in.")

    # Prevent betting more than available
    if amount > bal:
        return await ctx.respond(
            f"❌ You don't have enough points to wager **{amount}**. Your current balance is **{bal}**."
        )

    # Fetch upcoming match data without blocking Discord's event loop.
    payload = await get_upcoming_match()
    if payload is None:
        return await ctx.respond("❌ Could not fetch upcoming match data from the V2 API.")
    segments = _extract_segments(payload)
    matches = _ordered_tier_one_matches(segments)
    if not matches:
        return await ctx.respond("❌ No upcoming VCT matches are currently available to bet on.")

    match = matches[0]
    match_event = str(match.get("match_event") or "Unknown Event")
    team1 = str(match.get("team1") or "TBD")
    team2 = str(match.get("team2") or "TBD")
    match_page = str(match.get("match_page") or "")
    if team1 == "TBD" or team2 == "TBD" or not match_page:
        return await ctx.respond("❌ The upcoming match data is incomplete. Please try again later.")

    class ConfirmGambleView(discord.ui.View):
        def __init__(self, author_id: int, amount: int):
            super().__init__(timeout=60)
            self.author_id = author_id
            self.amount = amount

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
        async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):  # type: ignore
            if interaction.user.id != self.author_id:
                return await interaction.response.send_message(
                    "❌ You cannot respond to someone else's bet.",
                    ephemeral=True
                )

            current_bal = get_balance(self.author_id)
            if self.amount > current_bal:
                return await interaction.response.edit_message(
                    content=f"❌ Your balance has changed and you no longer have enough points to wager {self.amount}.",
                    view=None
                )

            class TeamSelectView(discord.ui.View):
                def __init__(self, author_id: int, amount: int):
                    super().__init__(timeout=60)
                    self.author_id = author_id
                    self.amount = amount

                @discord.ui.button(label=team1, style=discord.ButtonStyle.blurple)
                async def choose_team1(self, btn: discord.ui.Button, inter: discord.Interaction):  # type: ignore
                    if inter.user.id != self.author_id:
                        return await inter.response.send_message(
                            "❌ You cannot choose a team for someone else's bet.",
                            ephemeral=True
                        )

                    if not place_bet(
                        match_page, match_event, team1, team2,
                        self.author_id, team1, self.amount, ctx.channel.id,
                    ):
                        return await inter.response.edit_message(
                            content=(
                                f"❌ Your balance changed or the bet could not be saved. "
                                f"You need at least {self.amount} points."
                            ),
                            view=None,
                        )

                    await inter.response.edit_message(
                        content=(
                            f"✅ Bet placed! You wagered **{self.amount}** points on **{team1}** to win the next "
                            f"match (**{match_event}**). We'll notify you when the match starts and pay out when it ends."
                        ),
                        view=None
                    )
                    self.stop()

                @discord.ui.button(label=team2, style=discord.ButtonStyle.blurple)
                async def choose_team2(self, btn: discord.ui.Button, inter: discord.Interaction):  # type: ignore
                    if inter.user.id != self.author_id:
                        return await inter.response.send_message(
                            "❌ You cannot choose a team for someone else's bet.",
                            ephemeral=True
                        )

                    if not place_bet(
                        match_page, match_event, team1, team2,
                        self.author_id, team2, self.amount, ctx.channel.id,
                    ):
                        return await inter.response.edit_message(
                            content=(
                                f"❌ Your balance changed or the bet could not be saved. "
                                f"You need at least {self.amount} points."
                            ),
                            view=None,
                        )

                    await inter.response.edit_message(
                        content=(
                            f"✅ Bet placed! You wagered **{self.amount}** points on **{team2}** to win the next "
                            f"match (**{match_event}**). We'll notify you when the match starts and pay out when it ends."
                        ),
                        view=None
                    )
                    self.stop()

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
                async def cancel_team(self, btn: discord.ui.Button, inter: discord.Interaction):  # type: ignore
                    if inter.user.id != self.author_id:
                        return await inter.response.send_message(
                            "❌ You cannot cancel someone else's bet.",
                            ephemeral=True
                        )
                    await inter.response.edit_message(
                        content="❌ Bet cancelled.",
                        view=None
                    )
                    self.stop()

            team_view = TeamSelectView(self.author_id, self.amount)
            await interaction.response.edit_message(
                content=f"Select the team you think will win the upcoming match (Event: {match_event}).",
                view=team_view
            )
            self.stop()

        @discord.ui.button(label="No", style=discord.ButtonStyle.red)
        async def decline(self, button: discord.ui.Button, interaction: discord.Interaction):  # type: ignore
            if interaction.user.id != self.author_id:
                return await interaction.response.send_message(
                    "❌ You cannot decline someone else's bet.",
                    ephemeral=True
                )
            await interaction.response.edit_message(
                content="❌ Bet cancelled.",
                view=None
            )
            self.stop()

    view = ConfirmGambleView(user_id, amount)
    await ctx.respond(
        f"You are about to wager **{amount}** points on the upcoming match **{team1} vs {team2}** (Event: {match_event}).\n"
        f"Your current balance is **{bal}** points.\n"
        "Are you sure you want to proceed?",
        view=view
    )


# TASK: bet_watcher
@tasks.loop(seconds=60)
async def bet_watcher():
    """
    Periodically checks all unresolved bets to determine whether the match has
    started or finished. When a match starts, it pings all users who placed
    bets on that match. When a match finishes, it determines the winner,
    pays out the winners, resolves all bets for that match, and sends a
    summary message.
    """
    open_bets = get_open_bets()
    if not open_bets:
        return
    # Fetch both endpoints concurrently and keep blocking HTTP work off the event loop.
    live_data, recent_data = await asyncio.gather(
        get_live_score(),
        get_recent_match(),
    )
    segments_live = _extract_segments(live_data)
    segments_recent = _extract_segments(recent_data)
    # Group bets by normalized match_page
    bets_by_match: Dict[str, List[Dict]] = {}
    for bet in open_bets:
        mp = bet["match_page"]
        bets_by_match.setdefault(mp, []).append(bet)
    for mp, bets in bets_by_match.items():
        # Determine if start notification should be sent
        if bets and bets[0]["start_notified"] == 0:
            started = False
            for seg in segments_live:
                seg_mp = _normalize_match_page(seg.get("match_page") or "")
                if seg_mp == mp:
                    started = True
                    break
            if started:
                # Ping all bettors in their respective channels
                channel_to_users: Dict[int, Set[int]] = {}
                for bet in bets:
                    channel_to_users.setdefault(bet["channel_id"], set()).add(bet["user_id"])
                for ch_id, users in channel_to_users.items():
                    channel = bot.get_channel(ch_id)
                    if channel:
                        mentions = " ".join(f"<@{uid}>" for uid in users)
                        await channel.send(
                            f"🎮 The match between **{bets[0]['team1']}** and **{bets[0]['team2']}** is starting now! {mentions}"
                        )
                mark_start_notified(mp)
        # Determine if the match has finished
        finished_segment = None
        for seg in segments_recent:
            seg_mp = _normalize_match_page(seg.get("match_page") or "")
            if seg_mp == mp:
                finished_segment = seg
                break
        if finished_segment:
            # Only resolve a completed match when both scores are valid and unequal.
            score1 = _safe_int(finished_segment.get("score1"), -1)
            score2 = _safe_int(finished_segment.get("score2"), -1)
            if score1 < 0 or score2 < 0 or score1 == score2:
                continue

            winner_team = (
                finished_segment.get("team1")
                if score1 > score2
                else finished_segment.get("team2")
            )
            if not isinstance(winner_team, str) or not winner_team:
                continue

            winners, losers = resolve_bets(mp, winner_team)
            # Organize summary per channel
            channel_to_bets: Dict[int, List[Dict]] = {}
            for bet in winners + losers:
                channel_to_bets.setdefault(bet["channel_id"], []).append(bet)
            for ch_id, bet_list in channel_to_bets.items():
                channel = bot.get_channel(ch_id)
                if not channel:
                    continue
                # Build the summary message
                parts: List[str] = []
                parts.append(
                    f"🏁 The match between **{finished_segment['team1']}** and **{finished_segment['team2']}** has concluded."
                )
                parts.append(f"Winner: **{winner_team}**")
                winners_mentions = [f"<@{b['user_id']}>" for b in bet_list if b["team_bet"] == winner_team]
                losers_mentions = [f"<@{b['user_id']}>" for b in bet_list if b["team_bet"] != winner_team]
                if winners_mentions:
                    parts.append(
                        f"Winners ({len(winners_mentions)}): {', '.join(winners_mentions)} — you have been paid!"
                    )
                if losers_mentions:
                    parts.append(
                        f"Losers ({len(losers_mentions)}): {', '.join(losers_mentions)} — better luck next time."
                    )
                await channel.send("\n".join(parts))


@bet_watcher.before_loop
async def before_bet_watcher():
    """Ensure the bot is ready before the bet watcher starts."""
    await bot.wait_until_ready()


# RUN THE BOT
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)
