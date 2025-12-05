import sys
import asyncio
import requests  # type: ignore
import sqlite3
import random
from urllib.parse import urlparse
if sys.platform == "win32":
    # Use the SelectorEventLoop instead of the ProactorEventLoop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv # type: ignore
import os
import discord # type: ignore
from discord.commands import Option # type: ignore
from discord.ext import commands, tasks # type: ignore
from datetime import datetime, timedelta
import pytz  # type: ignore # pip install pytz
from keep_alive import keep_alive
from typing import List, Dict, Tuple, Set

# Configuration stuff
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
base_axsddlr_url = "https://vlrggapi.vercel.app/"
base_vlresports_url = "https://vlr.orlandomm.net/api/v1/"

keep_alive()
# We treat all input times as America/Los_Angeles
TZ = pytz.timezone("America/Los_Angeles")
# End configuration

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True  # so we can see who’s in voice channels

bot = discord.Bot(intents=intents)


# Global Database (only one meeting at a time)
# Only schedule one meeting, scheduling another one overwrites
meeting = {
    "scheduled_time": None,        # datetime (PST) when we check the voice channel
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


def _maybe_apply_daily_bonus(row: sqlite3.Row) -> int:
    """
    Apply a daily bonus to the user's balance if their balance is below
    DAILY_BONUS_THRESHOLD and at least DAILY_BONUS_INTERVAL seconds have
    passed since their last bonus. Returns the possibly updated balance.
    """
    current_time = int(datetime.utcnow().timestamp())
    balance = row["balance"]
    last_bonus = row["last_daily_bonus"] or 0
    if balance < DAILY_BONUS_THRESHOLD and (current_time - last_bonus) >= DAILY_BONUS_INTERVAL:
        balance += DAILY_BONUS_AMOUNT
        conn = sqlite3.connect(DATABASE_FILE)
        with conn:
            conn.execute(
                "UPDATE balances SET balance = ?, last_daily_bonus = ? WHERE user_id = ?",
                (balance, current_time, row["user_id"]),
            )
        conn.close()
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
            balance = _maybe_apply_daily_bonus(row)
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


def store_bet(match_page: str, match_event: str, team1: str, team2: str, user_id: int, team_bet: str, amount: int, channel_id: int) -> None:
    """Persist a new bet for a given match."""
    mp = _normalize_match_page(match_page)
    conn = sqlite3.connect(DATABASE_FILE)
    with conn:
        conn.execute(
            "INSERT INTO bets (match_page, match_event, team1, team2, user_id, team_bet, amount, channel_id, start_notified, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (mp, match_event, team1, team2, user_id, team_bet, amount, channel_id),
        )
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
    Resolve all outstanding bets for a given match. Winners receive double
    their wager (they already paid the wager when placing the bet). Losers
    receive nothing. Returns two lists: winners and losers, each entry being
    the bet row dict.
    """
    mp = _normalize_match_page(match_page)
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    winners = []
    losers = []
    with conn:
        cur = conn.execute(
            "SELECT * FROM bets WHERE match_page = ? AND resolved = 0",
            (mp,),
        )
        bet_rows = cur.fetchall()
        for row in bet_rows:
            bet = dict(row)
            if bet["team_bet"] == winning_team:
                # winner
                winners.append(bet)
                # pay double the amount (because original amount already deducted)
                user_id = bet["user_id"]
                amount = bet["amount"]
                current_balance = get_balance(user_id)
                update_balance(user_id, current_balance + amount * 2)
            else:
                losers.append(bet)
        # Mark all bets for this match as resolved
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
    # Start the background loop that watches for when to send reminders / check attendance
    meeting_watcher.start()
    # Make the database and start the bet watcher when the bot is ready
    init_db()
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
@bot.slash_command(name="schedule", description="Schedule a voice-channel meeting. Time zone is in PST")
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
      time_str: "HH:MM" (24-hour, in PST)
      voice_channel: a voice-channel mention (e.g. #General-Voice)
      mentions: list of @users who must join
    """
    # 1) Parse date & time (in PST)
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        dt_pst = TZ.localize(naive)
    except ValueError:
        await ctx.send("❌ Incorrect date and time format. Please use `YYYY-MM-DD HH:MM` in 24h PST.")
        return

    now_pst = datetime.now(tz=TZ)
    if dt_pst <= now_pst:
        await ctx.send("❌ You must choose a future date/time (PST).")
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

    human_time = dt_pst.strftime("%Y-%m-%d %H:%M PST")
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
    human_time = scheduled.strftime("%Y-%m-%d %H:%M PST")

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


# HELPER
def get_regionranks_info(region: str):
    url = f"{base_axsddlr_url}rankings?region={region}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None

def get_matches_info(idk: str):
    url = f"{base_axsddlr_url}match?q={idk}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None

# COMMAND: /regionranks 
@bot.slash_command(name="regionranks", description="Filters to show only the major teams in each region, sorted by rank.")
async def regionranks(ctx: discord.ApplicationContext,
                      region: Option(str, "Region code", required=True, choices=[
                          "na", "la", "la-s", "la-n", "cn", "eu", "ap", "kr", "jp"])): # type: ignore
    if not region:
        return await ctx.send(
            "❌ Please pick one of the following:\n`na (North America)`\n`la (LATAM)`\n`la-s (More LATAM)`\n`la-n (Even MORE LATAM)`\n`cn (China)`\n`eu (EMEA)`\n`ap (APAC)`\n`kr (Korea (also part of APAC))`\n`jp (Japan (also part of APAC))`"
        )

    region_key = region.lower()
    # 1) Define your whitelists (unchanged) …
    if region_key in ("na", "la", "la-s", "la-n"):
        whitelist = { "100 Thieves","Cloud9","Evil Geniuses","FURIA","KRÜ Esports",
                      "Leviatán","LOUD","MIBR","NRG","Sentinels","G2 Esports" }
    elif region_key == "cn":
        whitelist = { "All Gamers","Bilibili Gaming","EDward Gaming","FunPlus Phoenix",
                      "JDG Esports","Nova Esports","Titan Esports Club","Trace Esports",
                      "TYLOO","Wolves Esports","Dragon Ranger Gaming","Xi Lai Gaming" }
    elif region_key == "eu":
        whitelist = { "FNATIC","BBL Esports","FUT Esports","Karmine Corp","KOI",
                      "Natus Vincere","Team Heretics","Team Liquid","Team Vitality","GIANTX" }
    elif region_key in ("ap", "kr", "jp"):
        whitelist = { "DetonatioN FocusMe","DRX","Gen.G","Global Esports","Paper Rex",
                      "Rex Regum Qeon","T1","TALON","Team Secret","ZETA DIVISION",
                      "Nongshim RedForce","BOOM Esports" }
    else:
        return await ctx.respond(
            "❌ Please select a region!", ephemeral=True)

    # 2) Fetch the API data
    data = get_regionranks_info(region_key)
    if not data or "data" not in data:
        return await ctx.respond("❌ Could not fetch ranking data.")

    # 3) Filter to only whitelisted teams **and** sort by their rank as an integer
    filtered = [
        t for t in data["data"]
        if t["team"] in whitelist
    ]
    filtered.sort(key=lambda t: int(t["rank"]))

    if not filtered:
        return await ctx.respond("❌ None of the requested teams were found in the data.")

    # 4) Format the sorted list
    lines = []
    for t in filtered:
        lines.append(
            f"**Rank {t['rank']} – {t['team']}**\n"
            f"Last played: {t['last_played_team']}\n"
            f"Earnings: {t['earnings']}\n"
        )

    await ctx.respond("\n".join(lines))
    return None

# HELPERS
def get_recent_match():
    url = f"{base_axsddlr_url}match?q=results"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None

def get_upcoming_match():
    url = f"{base_axsddlr_url}match?q=upcoming"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None

def get_live_score():
    url = f"{base_axsddlr_url}match?q=live_score"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None

def _round_val(v: str) -> int:
    if not v or v == "N/A":
        return 0
    try:
        return int(v)
    except ValueError:
        return 0

# COMMAND: /recentmatches
@bot.slash_command(name="recentmatches", description="Gets the results of the recent matches.")
async def recentmatch_cmd(ctx: discord.ApplicationContext):

    data = get_recent_match()
    if not data or "data" not in data or "segments" not in data["data"]:
        return await ctx.respond("❌ Could not fetch match data.")

    events = [
        "VCT 2025: China Stage 2", "Esports World Cup 2025", "VCT 2025: Pacific Stage 2", "VCT 2025: EMEA Stage 2",
        "VCT 2025: Americas Stage 2", "Valorant Champions 2025"
    ]

    segments = data["data"]["segments"]
    output_lines = []

    for event_name in events:
        match = next(
            (seg for seg in segments if seg.get("tournament_name") == event_name),
            None
        )
        if match:
            output_lines.append(
                f"**{match['tournament_name']}**\n"
                f"**{match['round_info']}**\n"
                f"**{match['team1']} vs. {match['team2']}**\n"
                f"**Final Score: ** {match['score1']} - {match['score2']}\n"
                f"Game happened {match['time_completed']}\n"
                f"vlr.gg link: https://www.vlr.gg{match['match_page']}"
            )
    if output_lines:
        await ctx.respond("\n".join(output_lines))
    else:
        await ctx.respond("❌ No recent results found for 2025 VCT Tier 1 matches.")
    return None


# COMMAND: /upcomingmatches
@bot.slash_command(name="upcomingmatches", description="Gets upcoming VCT Tier 1 matches from all regions")
async def upcomingmatches_cmd(ctx: discord.ApplicationContext):
    data = get_upcoming_match()
    if not data or "data" not in data or "segments" not in data["data"]:
        return await ctx.respond("❌ Could not fetch match data.")

    events = [
        "VCT 2025: China Stage 2", "Esports World Cup 2025", "VCT 2025: Pacific Stage 2", "VCT 2025: EMEA Stage 2",
        "VCT 2025: Americas Stage 2", "Valorant Champions 2025"
    ]

    segments = data["data"]["segments"]
    output_lines = []

    for event_name in events:
        match = next((seg for seg in segments if seg.get("match_event") == event_name), None)
        if match:
            output_lines.append(
                f"**Upcoming game for **{match['match_event']}\n"
                f"**{match['match_series']}**\n"
                f"**{match['team1']} vs. {match['team2']}**\n"
                f"Game is **{match['time_until_match']}**\n"
                f"vlr.gg link: {match['match_page']}\n"
            )
    if output_lines:
        await ctx.respond("\n".join(output_lines))
    else:
        await ctx.respond("❌ No upcoming matches found for 2025 VCT Tier 1 matches. (Game might be too far into the future)")
    return None

# COMMAND: !livescore
@bot.slash_command(name="livescore", description="Gets live score for VCT Tier 1 matches")
async def matches(ctx: discord.ApplicationContext):
    data = get_live_score()
    if not data or "data" not in data or "segments" not in data["data"]:
        return await ctx.respond("❌ Could not fetch match data.")

    events = [
        "VCT 2025: China Stage 2", "Esports World Cup 2025", "VCT 2025: Pacific Stage 2", "VCT 2025: EMEA Stage 2",
        "VCT 2025: Americas Stage 2", "Valorant Champions 2025"
    ]

    segments = data["data"]["segments"]
    output_lines = []

    for event_name in events:
        match = next((seg for seg in segments if seg.get("match_event") == event_name), None)
        if not match:
            continue

        r1_ct = _round_val(match.get("team1_round_ct"))
        r1_t = _round_val(match.get("team1_round_t"))
        r2_ct = _round_val(match.get("team2_round_ct"))
        r2_t = _round_val(match.get("team2_round_t"))

        team1_map_total = r1_ct + r1_t
        team2_map_total = r2_ct + r2_t

        series_score = f"**{match['team1']}** {match['score1']} - {match['score2']} **{match['team2']}**"

        output_lines.append(
            f"**{match['match_event']}** • **{match['match_series']}**\n"
            f"**Series:**\n {series_score}\n"
            f"**Current Map:**\n {match['current_map']}\n"
            f"**Current Score:**\n **{match['team1']}** {team1_map_total} - {team2_map_total} **{match['team2']}**\n"
            f"vlr.gg link: {match['match_page']}\n"
        )
    if output_lines:
        await ctx.respond("\n".join(output_lines))
    else:
        await ctx.respond("❌ No ongoing matches. Use /upcomingmatches to see the next one")
    return None


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
    amount: Option(int, "The number of points you want to wager", required=True)  # type: ignore
):
    """
    Handle a gambling interaction where the user first confirms their intent,
    then selects which team will win the upcoming match. Bets are stored in
    the database and processed automatically when the match starts/finishes.
    """
    user_id = ctx.author.id
    # Ensure the amount is positive
    if amount <= 0:
        return await ctx.respond("❌ The amount must be a positive integer.")
    # Fetch upcoming match data
    data = get_upcoming_match()
    if not data or "data" not in data or "segments" not in data["data"]:
        return await ctx.respond("❌ Could not fetch upcoming match data.")
    segments = data["data"]["segments"]
    # Use the same event ordering as /upcomingmatches
    events = [
        "VCT 2025: China Stage 2", "Esports World Cup 2025", "VCT 2025: Pacific Stage 2", "VCT 2025: EMEA Stage 2",
        "VCT 2025: Americas Stage 2", "Valorant Champions 2025"
    ]
    match = None
    for event_name in events:
        match = next((seg for seg in segments if seg.get("match_event") == event_name), None)
        if match:
            break
    if not match:
        return await ctx.respond("❌ No upcoming VCT matches are currently available to bet on.")
    # Extract match details
    match_event = match.get("match_event") or "Unknown Event"
    team1 = match.get("team1")
    team2 = match.get("team2")
    match_page = match.get("match_page")
    # Normalize match_page for storage and comparisons
    normalized_mp = _normalize_match_page(match_page)
    # Check user's balance
    bal = get_balance(user_id)
    if amount > bal:
        return await ctx.respond(
            f"❌ You don't have enough points to wager **{amount}**. Your current balance is **{bal}**."
        )
    # First confirmation view
    class ConfirmGambleView(discord.ui.View):
        def __init__(self, author_id: int, amount: int):
            super().__init__(timeout=60)
            self.author_id = author_id
            self.amount = amount

        @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
        async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):  # type: ignore
            # Only the original user can confirm
            if interaction.user.id != self.author_id:
                return await interaction.response.send_message("❌ You cannot respond to someone else's bet.",
                                                             ephemeral=True)
            # Double-check balance in case it changed since the command was invoked
            current_bal = get_balance(self.author_id)
            if self.amount > current_bal:
                return await interaction.response.edit_message(
                    content=f"❌ Your balance has changed and you no longer have enough points to wager {self.amount}.",
                    view=None
                )
            # Present team selection view
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
                    # Deduct wager and store bet
                    bal_now = get_balance(self.author_id)
                    if self.amount > bal_now:
                        return await inter.response.edit_message(
                            content=f"❌ Your balance has changed and you no longer have enough points to wager {self.amount}.",
                            view=None
                        )
                    update_balance(self.author_id, bal_now - self.amount)
                    store_bet(match_page, match_event, team1, team2, self.author_id, team1, self.amount, ctx.channel.id)
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
                    bal_now = get_balance(self.author_id)
                    if self.amount > bal_now:
                        return await inter.response.edit_message(
                            content=f"❌ Your balance has changed and you no longer have enough points to wager {self.amount}.",
                            view=None
                        )
                    update_balance(self.author_id, bal_now - self.amount)
                    store_bet(match_page, match_event, team1, team2, self.author_id, team2, self.amount, ctx.channel.id)
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
                content=(
                    f"Select the team you think will win the upcoming match (Event: {match_event})."
                ),
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
    # Fetch live and recent match data once per run
    live_data = get_live_score()
    recent_data = get_recent_match()
    segments_live = []
    segments_recent = []
    if live_data and "data" in live_data and "segments" in live_data["data"]:
        segments_live = live_data["data"]["segments"]
    if recent_data and "data" in recent_data and "segments" in recent_data["data"]:
        segments_recent = recent_data["data"]["segments"]
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
            # Parse scores to determine winner
            s1 = finished_segment.get("score1")
            s2 = finished_segment.get("score2")
            try:
                score1 = int(s1)
            except (TypeError, ValueError):
                score1 = 0
            try:
                score2 = int(s2)
            except (TypeError, ValueError):
                score2 = 0
            winner_team = finished_segment.get("team1") if score1 >= score2 else finished_segment.get("team2")
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
    bot.run(token)