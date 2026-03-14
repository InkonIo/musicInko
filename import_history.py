"""
Импортирует историю за последние 3 месяца с Last.fm в PostgreSQL.
Запускать один раз локально:
    pip install -r requirements.txt
    python import_history.py
"""
import os
import time
import requests
import psycopg2
import logging
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

LASTFM_API_KEY  = os.environ["LASTFM_API_KEY"]
LASTFM_USERNAME = os.environ["LASTFM_USERNAME"]
DATABASE_URL    = os.environ["DATABASE_URL"]

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

def fetch_page(page: int, from_ts: int) -> dict:
    params = {
        "method":   "user.getrecenttracks",
        "user":     LASTFM_USERNAME,
        "api_key":  LASTFM_API_KEY,
        "format":   "json",
        "limit":    200,
        "page":     page,
        "from":     from_ts,
        "extended": 0,
    }
    resp = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def parse_tracks(data: dict) -> list[dict]:
    tracks = data.get("recenttracks", {}).get("track", [])
    result = []
    for t in tracks:
        if t.get("@attr", {}).get("nowplaying"):
            continue
        ts = t.get("date", {}).get("uts")
        if not ts:
            continue

        title  = t.get("name", "Unknown")
        artist = t.get("artist", {}).get("#text", "Unknown")
        album  = t.get("album",  {}).get("#text", "")

        cover = None
        for img in reversed(t.get("image", [])):
            url = img.get("#text", "")
            if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                cover = url
                break

        result.append({
            "title":     title,
            "artist":    artist,
            "album":     album,
            "cover":     cover,
            "played_at": datetime.fromtimestamp(int(ts), tz=timezone.utc),
        })
    return result

def bulk_insert(tracks: list[dict]):
    if not tracks:
        return
    with get_conn() as con:
        with con.cursor() as cur:
            cur.executemany(
                "INSERT INTO plays (track, artist, album, cover_url, played_at) VALUES (%s,%s,%s,%s,%s)",
                [(t["title"], t["artist"], t["album"], t["cover"], t["played_at"]) for t in tracks]
            )

def main():
    init_db()

    from_ts = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())
    log.info("Импортируем с %s (последние 3 месяца)", datetime.fromtimestamp(from_ts).strftime("%d.%m.%Y"))

    first        = fetch_page(1, from_ts)
    total_pages  = int(first["recenttracks"]["@attr"]["totalPages"])
    total_tracks = int(first["recenttracks"]["@attr"]["total"])
    log.info("Найдено треков за 3 месяца: %d (~%d страниц)", total_tracks, total_pages)

    all_tracks = parse_tracks(first)
    log.info("Страница 1/%d", total_pages)

    for page in range(2, total_pages + 1):
        try:
            data = fetch_page(page, from_ts)
            all_tracks.extend(parse_tracks(data))
            log.info("Страница %d/%d — итого %d треков", page, total_pages, len(all_tracks))
            time.sleep(0.25)
        except Exception as e:
            log.error("Ошибка на странице %d: %s", page, e)
            time.sleep(2)

    log.info("Загружаем в базу...")
    bulk_insert(all_tracks)
    log.info("✅ Готово! Импортировано %d треков.", len(all_tracks))

if __name__ == "__main__":
    main()