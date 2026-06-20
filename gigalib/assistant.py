import json
import os
import re
import time
from datetime import datetime, timezone

from google import genai
from google.genai import types

from gigalib import db
from gigalib.models import Game

MAX_GAMES_IN_PROMPT = 700


def _build_library_context(games):
    """Build a rich context payload from the game library."""

    def _compact_text(value, limit=80):
        if not value:
            return None
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "..."

    # Keep prompt context compact to reduce token usage/rate-limit pressure.
    prioritized = sorted(
        games,
        key=lambda g: (
            0 if g.is_installed else 1,
            -(g.playtime_hours or 0),
            (g.title or "").lower(),
        ),
    )
    prompt_games = prioritized[:MAX_GAMES_IN_PROMPT]

    game_list = []
    for g in prompt_games:
        game_list.append(
            {
                "title": g.title,
                "platform": g.platform,
                "playtime_hours": round(g.playtime_hours or 0, 1),
                "genre": _compact_text(g.genre, 60),
                "is_installed": bool(g.is_installed),
                "critic_rating": g.critic_rating,
                "rating_tier": g.rating_tier,
                "main_story_hours": g.main_story_hours,
                "completionist_hours": g.completionist_hours,
                "days_since_last_played": _days_since_last_played(g),
            }
        )

    # Build aggregate stats for quick reference
    total = len(games)
    platforms = {}
    genres = {}
    total_playtime = 0
    installed_count = sum(1 for g in games if g.is_installed)
    unplayed = sum(1 for g in games if g.playtime_hours == 0)

    for g in games:
        platforms[g.platform] = platforms.get(g.platform, 0) + 1
        total_playtime += g.playtime_hours or 0
        if g.genre:
            for genre in g.genre.split(","):
                genre = genre.strip()
                genres[genre] = genres.get(genre, 0) + 1

    top_genres = sorted(genres.items(), key=lambda x: x[1], reverse=True)[:10]

    stats = {
        "total_games": total,
        "platforms": platforms,
        "installed": installed_count,
        "unplayed": unplayed,
        "total_playtime_hours": round(total_playtime, 1),
        "top_genres": dict(top_genres),
        "prompt_games_included": len(game_list),
        "prompt_games_omitted": max(0, total - len(game_list)),
    }

    return game_list, stats


def _parse_last_played(last_played):
    """Parse last_played into a timezone-aware datetime, if possible."""
    if not last_played:
        return None

    if isinstance(last_played, datetime):
        dt = last_played
    else:
        raw = str(last_played).strip()
        if not raw:
            return None
        # Handle common ISO formats like 2025-12-30T10:20:30Z
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _days_since_last_played(game):
    dt = _parse_last_played(game.last_played)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _extract_avoid_titles(user_message, history, games):
    """Infer titles the user asked to avoid from recent-play feedback."""
    lowered = (user_message or "").lower()
    signals = [
        "played this recently",
        "played it recently",
        "i played this recently",
        "already played",
        "not that one",
        "something else",
    ]

    if not any(s in lowered for s in signals):
        return set()

    haystacks = [user_message or ""]
    if history:
        haystacks.extend(msg.get("content", "") for msg in history[-6:])

    avoid = set()
    for game in games:
        title = (game.title or "").strip()
        if title and any(title.lower() in h.lower() for h in haystacks):
            avoid.add(title.lower())
    return avoid


def _extract_titles_from_text(text):
    """Extract likely game titles from assistant markdown/text output."""
    if not text:
        return set()

    titles = set()

    # Common recommendation formatting: **Game Title**
    for match in re.findall(r"\*\*([^*]{2,120})\*\*", text):
        candidate = match.strip(" .:-")
        if candidate:
            titles.add(candidate)

    # Headings like: 1. The Choice: Game Title
    for line in text.splitlines():
        m = re.match(r"\s*\d+\.\s+[^:]{0,80}:\s+(.+)$", line)
        if m:
            candidate = m.group(1).strip(" .:-*")
            if candidate:
                titles.add(candidate)

    return titles


def _derive_repeat_exclusions(user_message, history, games):
    """Build a deterministic title exclusion set for repeat-feedback messages."""
    lowered = (user_message or "").lower()
    repeat_feedback = any(
        s in lowered
        for s in [
            "played this recently",
            "played it recently",
            "i played this recently",
            "already played",
            "something else",
            "different one",
            "another one",
            "not that one",
        ]
    )

    if not repeat_feedback or not history:
        return set()

    # Prefer the latest assistant turn as the set of recommendations to avoid.
    last_assistant = next(
        (
            msg.get("content", "")
            for msg in reversed(history)
            if msg.get("role") == "assistant"
        ),
        "",
    )
    if not last_assistant:
        return set()

    extracted = {t.lower() for t in _extract_titles_from_text(last_assistant)}
    library_titles = {(g.title or "").strip().lower() for g in games if g.title}
    return extracted.intersection(library_titles)


def _fallback_ranked_candidates(games, user_message, history):
    """Return ranked candidates for local fallback recommendations."""
    message = (user_message or "").lower()
    wants_not_recent = any(
        phrase in message
        for phrase in [
            "haven't played in a while",
            "havent played in a while",
            "not played in a while",
            "long time",
            "something else",
            "different game",
        ]
    )

    avoid_titles = _extract_avoid_titles(user_message, history, games)
    min_stale_days = 30 if wants_not_recent else 0

    candidates = []
    for g in games:
        title = (g.title or "").strip()
        if not title or title.lower() in avoid_titles:
            continue

        playtime = g.playtime_hours or 0
        days = _days_since_last_played(g)

        if wants_not_recent and playtime <= 0:
            # User asked for a game they have history with.
            continue
        if wants_not_recent and days is not None and days < min_stale_days:
            continue

        score = 0
        score += (g.critic_rating or 70) / 10
        score += 2 if g.is_installed else 0
        score += 1.5 if playtime > 0 else 0

        if days is not None:
            score += min(days / 45, 5)
        elif playtime > 0:
            # Missing last_played is common; still allow previously-played titles.
            score += 2

        tier_bonus = {
            "legendary": 3,
            "mighty": 2.5,
            "strong": 2,
            "good": 1,
        }
        score += tier_bonus.get((g.rating_tier or "").lower(), 0)

        candidates.append((score, g, days))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[:3]


def _fallback_response(user_message, games, history=None, rate_limited=False):
    """Generate a useful local response when Gemini is unavailable."""
    msg = (user_message or "").lower()

    if "how many" in msg and (
        "unplayed" in msg or "haven't played" in msg or "havent played" in msg
    ):
        unplayed = sum(1 for g in games if (g.playtime_hours or 0) == 0)
        prefix = (
            "Gemini is rate-limited, but I can still answer from your local library.\n\n"
            if rate_limited
            else ""
        )
        return f"{prefix}You have **{unplayed} unplayed games** in your library."

    picks = _fallback_ranked_candidates(games, user_message, history or [])
    if not picks:
        base = "I couldn't find strong matches with that constraint in your local data."
        if rate_limited:
            return f"Gemini is rate-limited right now. {base} Try loosening the filter (for example: 'installed only' or 'any genre')."
        return base

    lines = []
    if rate_limited:
        lines.append(
            "Gemini is rate-limited right now, so I pulled these from your local library logic:"
        )
        lines.append("")

    lines.append("Try one of these:")
    for idx, (_, game, days) in enumerate(picks, start=1):
        playtime = f"{(game.playtime_hours or 0):.1f}h"
        install = "Installed" if game.is_installed else "Not Installed"
        last_seen = f"{days}d ago" if days is not None else "last played unknown"
        lines.append(
            f"{idx}. **{game.title}** ({game.platform}) - {playtime}, {install}, {last_seen}"
        )

    lines.append("")
    lines.append(
        "If you want, I can narrow this to a mood (chill/intense), session length, or installed-only."
    )
    return "\n".join(lines)


def ask_assistant(user_message, games, history=None):
    """Interactive chat with Gemini — full access to the game library data."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-api-key":
        return "Gemini API key not configured. Add GEMINI_API_KEY to your .env file."

    client = genai.Client(api_key=api_key)
    game_list, stats = _build_library_context(games)

    system_instruction = f"""You are GigaLib Assistant — an elite video game curator, backlog strategist, and gaming culture expert with access to the user's game library data.

## YOUR CAPABILITIES:
1. **Recommend games** based on mood, time available, genre preference, or any criteria
2. **Query the library** — answer questions like "how many horror games do I own?", "what's my most played platform?", "list all games I haven't touched"
3. **Analyze patterns** — identify gaming habits, suggest what's missing, notice trends
4. **Compare games** — help decide between two options the user is considering
5. **Track progress** — estimate how long to finish a game based on current playtime vs HLTB data
6. **General gaming chat** — discuss games, lore, upcoming content, strategies

## LIBRARY STATS (live data):
{json.dumps(stats, separators=(",", ":"))}

## LIBRARY SNAPSHOT ({stats['prompt_games_included']} of {stats['total_games']} games):
{json.dumps(game_list, separators=(",", ":"))}

## RULES:
- Use the library stats for precise aggregate numbers.
- Use the library snapshot for recommendations and examples.
- If the user asks for an exhaustive list and data is missing from snapshot, say so briefly and offer to narrow scope.
- When recommending, pull FROM their library unless they ask about new purchases.
- If they ask data questions ("how many...?", "list all...?", "which games...?"), query the library data above and give precise answers.
- If a game has a personal review, treat it as ground truth about the user's feelings.
- Games with is_installed=true can be launched immediately. Prefer these for "play right now" requests.
- If the user says they played a recommendation recently or asks for something else, do not repeat that same game. Treat that as an explicit negative preference for this thread.
- For "haven't played in a while" style requests, prioritize games with meaningful play history that have been untouched for a long time.
- Be conversational, witty, and opinionated. You're a gaming buddy, not a search engine.
- Keep responses concise but rich. No fluff."""

    # Build conversation messages
    contents = []
    if history:
        for msg in history[-8:]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=msg["content"])])
            )

    hard_exclusions = _derive_repeat_exclusions(user_message, history or [], games)
    if hard_exclusions:
        exclusions = ", ".join(sorted(hard_exclusions))
        user_message = (
            f"{user_message}\n\n"
            "Hard constraint for this reply: do NOT recommend any of these titles again: "
            f"{exclusions}."
        )

    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    models_to_try = ["gemini-3.5-flash", "gemini-2.0-flash"]
    last_error = None

    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                    ),
                )

                if response.text:
                    return response.text
                return "Hmm, I blanked. Try asking differently!"

            except Exception as e:
                last_error = e
                err_str = str(e)
                if (
                    "429" in err_str
                    or "503" in err_str
                    or "RESOURCE_EXHAUSTED" in err_str
                    or "UNAVAILABLE" in err_str
                ):
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    break  # try next model
                else:
                    return f"Gemini error: {err_str}"

    return _fallback_response(user_message, games, history=history, rate_limited=True)
