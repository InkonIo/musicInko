import os
import time
import requests
import logging
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── ENV ───────────────────────────────────────────────────────────────────────
LASTFM_API_KEY      = os.environ["LASTFM_API_KEY"]
LASTFM_USERNAME     = os.environ["LASTFM_USERNAME"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
DATABASE_URL        = os.environ["DATABASE_URL"]

POLL_INTERVAL    = 30  # секунд
WEEKLY_POST_DAY  = 4   # 0=пн … 6=вс, 4=пт
WEEKLY_POST_HOUR = 14  # 19:00 Алматы (UTC+5)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS plays (
                    id        SERIAL PRIMARY KEY,
                    track     TEXT NOT NULL,
                    artist    TEXT NOT NULL,
                    album     TEXT,
                    cover_url TEXT,
                    played_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weekly_posted (
                    week_key TEXT PRIMARY KEY
                )
            """)
    log.info("DB ready.")

def save_play(track: dict):
    with get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO plays (track, artist, album, cover_url) VALUES (%s,%s,%s,%s)",
                (track["title"], track["artist"], track["album"], track["cover"])
            )

def get_week_stats(since: datetime) -> dict:
    with get_conn() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT track, artist, cover_url, COUNT(*) AS cnt
                FROM plays WHERE played_at >= %s
                GROUP BY track, artist, cover_url
                ORDER BY cnt DESC LIMIT 5
            """, (since,))
            top_tracks = cur.fetchall()

            cur.execute("""
                SELECT artist, COUNT(*) AS cnt
                FROM plays WHERE played_at >= %s
                GROUP BY artist
                ORDER BY cnt DESC LIMIT 3
            """, (since,))
            top_artists = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*) FROM plays WHERE played_at >= %s", (since,)
            )
            total_plays = cur.fetchone()[0]

            cur.execute("""
                SELECT EXTRACT(HOUR FROM played_at)::int AS h, COUNT(*) AS cnt
                FROM plays WHERE played_at >= %s
                GROUP BY h ORDER BY cnt DESC LIMIT 1
            """, (since,))
            peak = cur.fetchone()

    total_hours = (total_plays * 2.5) / 60
    return {
        "top_tracks":  top_tracks,
        "top_artists": top_artists,
        "total_plays": total_plays,
        "total_hours": total_hours,
        "peak_hour":   peak[0] if peak else None,
    }

def already_posted_this_week(week_key: str) -> bool:
    with get_conn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM weekly_posted WHERE week_key=%s", (week_key,))
            return cur.fetchone() is not None

def mark_week_posted(week_key: str):
    with get_conn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO weekly_posted (week_key) VALUES (%s) ON CONFLICT DO NOTHING",
                (week_key,)
            )

# ── LAST.FM ───────────────────────────────────────────────────────────────────
def get_current_track() -> dict | None:
    resp = requests.get(
        "https://ws.audioscrobbler.com/2.0/",
        params={
            "method":  "user.getrecenttracks",
            "user":    LASTFM_USERNAME,
            "api_key": LASTFM_API_KEY,
            "format":  "json",
            "limit":   1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    tracks = resp.json().get("recenttracks", {}).get("track", [])
    if not tracks:
        return None

    track = tracks[0]
    if not track.get("@attr", {}).get("nowplaying"):
        return None

    title  = track.get("name", "Unknown")
    artist = track.get("artist", {}).get("#text", "Unknown")
    album  = track.get("album",  {}).get("#text", "")

    cover = None
    for img in reversed(track.get("image", [])):
        url = img.get("#text", "")
        if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
            cover = url
            break

    return {
        "id":     f"{artist}_{title}",
        "title":  title,
        "artist": artist,
        "album":  album,
        "cover":  cover,
    }

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg_post(text: str, photo: str | None = None):
    if photo:
        r = requests.post(f"{TG_API}/sendPhoto", json={
            "chat_id":              TELEGRAM_CHANNEL_ID,
            "photo":                photo,
            "caption":              text,
            "parse_mode":           "HTML",
            "disable_notification": True,
        }, timeout=15)
    else:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id":              TELEGRAM_CHANNEL_ID,
            "text":                 text,
            "parse_mode":           "HTML",
            "disable_notification": True,
        }, timeout=15)
    r.raise_for_status()

def mood_label(peak_hour: int | None) -> str:
    if peak_hour is None:   return "🎵 разная"
    if 6  <= peak_hour < 12: return "☀️ утренняя, бодрая"
    if 12 <= peak_hour < 18: return "⚡ дневная, энергичная"
    if 18 <= peak_hour < 23: return "🌆 вечерняя"
    return "🌙 ночная, атмосферная"

def post_weekly_report():
    now      = datetime.now(timezone.utc)
    since    = now - timedelta(days=7)
    stats    = get_week_stats(since)
    week_key = now.strftime("%Y-W%W")

    if already_posted_this_week(week_key):
        return
    if stats["total_plays"] == 0:
        log.info("No plays this week, skipping.")
        return

    lines = ["📊 <b>Музыкальная неделя</b>\n"]

    if stats["top_tracks"]:
        lines.append("🎵 <b>Топ треков:</b>")
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, (track, artist, _, cnt) in enumerate(stats["top_tracks"]):
            lines.append(f"{medals[i]} {artist} — {track} <i>({cnt})</i>")

    if stats["top_artists"]:
        lines.append("\n👤 <b>Топ артистов:</b>")
        for i, (artist, cnt) in enumerate(stats["top_artists"], 1):
            lines.append(f"{i}. {artist} <i>({cnt} треков)</i>")

    h = int(stats["total_hours"])
    m = int((stats["total_hours"] - h) * 60)
    lines.append(f"\n⏱ Слушал: <b>{h} ч {m} мин</b> ({stats['total_plays']} треков)")
    lines.append(f"🎭 Настроение: <b>{mood_label(stats['peak_hour'])}</b>")

    cover = stats["top_tracks"][0][2] if stats["top_tracks"] else None
    tg_post("\n".join(lines), photo=cover)
    mark_week_posted(week_key)
    log.info("Weekly report posted.")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    init_db()
    log.info("Bot started. Polling every %ds.", POLL_INTERVAL)
    last_id: str | None = None

    while True:
        try:
            track = get_current_track()
            if track and track["id"] != last_id:
                save_play(track)
                log.info("Saved: %s — %s", track["artist"], track["title"])
                last_id = track["id"]
            elif not track:
                last_id = None

            now = datetime.now()
            if now.weekday() == WEEKLY_POST_DAY and now.hour == WEEKLY_POST_HOUR:
                post_weekly_report()

        except requests.RequestException as e:
            log.error("Network error: %s", e)
        except Exception as e:
            log.exception("Unexpected error: %s", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()