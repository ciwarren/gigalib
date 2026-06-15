"""
Enrichment layer: fetches metadata from IGDB and HowLongToBeat
to give Gemini maximum context about each game.
"""

import os
import re
import asyncio
import requests
from howlongtobeatpy import HowLongToBeat
from gigalib.models import Game
from gigalib import db


# --- IGDB (via Twitch OAuth) ---

def _get_igdb_token():
    """Exchange Twitch client credentials for an IGDB access token."""
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None, None

    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return client_id, data["access_token"]


def _query_igdb(client_id, token, title):
    """Query IGDB for a game's rating, genres, summary, and cover."""
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }

    # Sanitize title for IGDB search (commas/semicolons break APICALYPSE syntax)
    search_title = title.replace('"', '')
    search_title = search_title.replace(';', '')
    search_title = search_title.replace(',', '')
    # Remove trademark/copyright symbols
    search_title = re.sub(r'[®™©]', '', search_title)
    # Replace underscores with spaces (e.g. Watch_Dogs)
    search_title = search_title.replace('_', ' ')
    # Remove platform suffixes
    search_title = re.sub(
        r'\s*(Xbox Series X\s*\|\s*S|Xbox One|Windows Edition|PC Edition|'
        r'\(PC\)|\(Game Preview\)|\(Alpha Testing\)|\(Obsolete\))\s*',
        ' ', search_title, flags=re.IGNORECASE
    ).strip()
    # Remove "for Xbox Series X|S" patterns
    search_title = re.sub(r'\s+for\s+Xbox.*$', '', search_title, flags=re.IGNORECASE).strip()
    # Remove trailing bare "Windows" (e.g. "Cooking Simulator Windows")
    search_title = re.sub(r'\s+Windows$', '', search_title, flags=re.IGNORECASE).strip()
    # Collapse multiple spaces
    search_title = re.sub(r'\s{2,}', ' ', search_title).strip()

    # Search with higher limit to find exact match among results
    body = (
        f'fields name, total_rating, genres.name, summary, cover.url, themes.name, game_modes.name;'
        f' search "{search_title}"; limit 10;'
    )

    resp = requests.post(
        "https://api.igdb.com/v4/games",
        headers=headers,
        data=body,
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()

    # If no results, try stripping edition/subtitle suffixes
    if not results:
        stripped = re.sub(
            r'\s*[-:]\s*(Definitive|Anniversary|Complete|Game of the Year|GOTY|'
            r'Ultimate|Gold|Legacy|Remastered|Enhanced|Deluxe|Premium|Standard|'
            r'Classic|Limited.*Edition|.*Special Edition)\b.*$',
            '', search_title, flags=re.IGNORECASE
        ).strip()
        # Try removing trailing "Legacy"/"Classic" without a separator
        if stripped == search_title:
            stripped = re.sub(r'\s+(Legacy|Classic)$', '', search_title, flags=re.IGNORECASE).strip()
        # Try removing " - Multiplayer" / " - Single Player" suffixes
        if stripped == search_title:
            stripped = re.sub(r'\s*-\s*(Multiplayer|Single Player|Staging Branch|'
                             r'Public Testing|Test Server|Public Beta Client|'
                             r'Experimental Server)\s*$', '', search_title, flags=re.IGNORECASE).strip()
        # Try removing parenthetical platform info
        if stripped == search_title:
            stripped = re.sub(r'\s*\([^)]*\)\s*$', '', search_title).strip()
        # Try removing regional edition suffixes like "The ANZ Special Edition"
        if stripped == search_title:
            stripped = re.sub(r'\s+The\s+\w+\s+(Special\s+)?Edition$', '', search_title, flags=re.IGNORECASE).strip()
        # Try removing " UNLIMITED" or " COMPLETE" (all caps suffixes)
        if stripped == search_title:
            stripped = re.sub(r'\s+(UNLIMITED|COMPLETE|COLLECTION)$', '', search_title).strip()
        # Try removing "Playtest" / "Open Beta" / "Demo" / "PTS" suffixes
        if stripped == search_title:
            stripped = re.sub(r'\s+(Playtest|Open Beta|Demo|PTS|Beta|Launcher)$', '', search_title, flags=re.IGNORECASE).strip()
        # Try removing "Limited XYZ Edition" (e.g. "Watch Dogs Limited Asia Edition")
        if stripped == search_title:
            stripped = re.sub(r'\s+Limited\s+\w+\s+Edition$', '', search_title, flags=re.IGNORECASE).strip()
        if stripped != search_title:
            body = (
                f'fields name, total_rating, genres.name, summary, cover.url, themes.name, game_modes.name;'
                f' search "{stripped}"; limit 10;'
            )
            resp = requests.post(
                "https://api.igdb.com/v4/games",
                headers=headers,
                data=body,
                timeout=15,
            )
            if resp.ok:
                results = resp.json()

    # If no exact match found in search results, try exact name query
    exact_found = any(r.get("name", "").lower() == title.lower() for r in results) if results else False
    if not exact_found:
        body_exact = (
            f'fields name, total_rating, genres.name, summary, cover.url, themes.name, game_modes.name;'
            f' where name = "{search_title}"; limit 5;'
        )
        resp2 = requests.post(
            "https://api.igdb.com/v4/games",
            headers=headers,
            data=body_exact,
            timeout=15,
        )
        if resp2.ok:
            exact_results = resp2.json()
            if exact_results:
                results = exact_results + (results or [])

    if not results:
        return None

    # Prefer exact name match with the most data (summary + rating)
    game = results[0]
    exact_matches = [r for r in results if r.get("name", "").lower() == title.lower()]
    if exact_matches:
        # Pick the one with summary and highest rating
        exact_matches.sort(key=lambda r: (bool(r.get("summary")), r.get("total_rating") or 0), reverse=True)
        game = exact_matches[0]
    return {
        "critic_rating": round(game.get("total_rating", 0), 1) if game.get("total_rating") else None,
        "genres": ", ".join(g["name"] for g in game.get("genres", [])),
        "description": game.get("summary", ""),
        "cover_url": game.get("cover", {}).get("url", ""),
        "themes": ", ".join(t["name"] for t in game.get("themes", [])),
        "is_multiplayer": any(
            m.get("name", "").lower() in ("multiplayer", "co-operative", "split screen", "battle royale")
            for m in game.get("game_modes", [])
        ),
    }


# --- HowLongToBeat ---

async def _query_hltb(title):
    """Query HowLongToBeat for completion times."""
    results = await HowLongToBeat().async_search(title)
    if not results:
        return None

    # Pick the best match (first result is highest similarity)
    best = results[0]
    return {
        "main_story_hours": best.main_story if best.main_story else None,
        "completionist_hours": best.completionist if best.completionist else None,
    }


def _get_hltb_data(title):
    """Synchronous wrapper for HLTB async search."""
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_query_hltb(title))
        loop.close()
        return result
    except Exception:
        return None


# --- Rating Tier Assignment ---

def _assign_tier(rating):
    """Assign a tier based on critic rating (OpenCritic-style)."""
    if rating is None:
        return None
    if rating >= 85:
        return "Mighty"
    elif rating >= 70:
        return "Strong"
    elif rating >= 50:
        return "Fair"
    else:
        return "Weak"


# --- Main Enrichment Function ---

def enrich_game(game):
    """Enrich a single game with IGDB and HLTB data."""
    enriched = False

    # Skip if fully enriched
    if game.critic_rating is not None and game.main_story_hours is not None and game.description:
        return False

    # IGDB enrichment
    if game.critic_rating is None or not game.description:
        try:
            client_id, token = _get_igdb_token()
            if client_id and token:
                igdb_data = _query_igdb(client_id, token, game.title)
                if igdb_data:
                    if igdb_data["critic_rating"]:
                        game.critic_rating = igdb_data["critic_rating"]
                        game.rating_tier = _assign_tier(igdb_data["critic_rating"])
                    if igdb_data["genres"] and not game.genre:
                        game.genre = igdb_data["genres"]
                    if igdb_data["description"] and not game.description:
                        game.description = igdb_data["description"]
                    if igdb_data["themes"] and not game.tags:
                        game.tags = igdb_data["themes"]
                    if igdb_data["cover_url"] and not game.image_url:
                        # IGDB returns //images.igdb.com/... URLs, prepend https:
                        url = igdb_data["cover_url"]
                        if url.startswith("//"):
                            url = "https:" + url
                        # Get high-res version
                        url = url.replace("t_thumb", "t_cover_big")
                        game.image_url = url
                    if igdb_data["is_multiplayer"]:
                        game.is_multiplayer = True
                    enriched = True
        except Exception:
            pass  # Don't fail the whole enrichment if IGDB is down

    # HLTB enrichment
    if game.main_story_hours is None:
        hltb_data = _get_hltb_data(game.title)
        if hltb_data:
            game.main_story_hours = hltb_data["main_story_hours"]
            game.completionist_hours = hltb_data["completionist_hours"]
            enriched = True

    return enriched


def enrich_all_games():
    """Enrich all games in the library that are missing metadata."""
    games = Game.query.filter(
        (Game.critic_rating.is_(None)) | (Game.main_story_hours.is_(None))
    ).all()

    enriched_count = 0
    errors = 0

    for game in games:
        try:
            if enrich_game(game):
                enriched_count += 1
        except Exception:
            errors += 1

    db.session.commit()
    return {"enriched": enriched_count, "errors": errors, "total_checked": len(games)}
