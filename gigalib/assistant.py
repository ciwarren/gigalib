import os
import json
from google import genai
from google.genai import types
from gigalib.models import Game
from gigalib import db


def _build_library_context(games):
    """Build a rich context payload from the game library."""
    game_list = [
        {
            "title": g.title,
            "platform": g.platform,
            "playtime_hours": g.playtime_hours,
            "genre": g.genre,
            "tags": g.tags,
            "description": g.description,
            "review": g.review,
            "last_played": g.last_played,
            "is_installed": g.is_installed,
            "critic_rating": g.critic_rating,
            "rating_tier": g.rating_tier,
            "main_story_hours": g.main_story_hours,
            "completionist_hours": g.completionist_hours,
        }
        for g in games
    ]

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
    }

    return game_list, stats


def ask_assistant(user_message, games, history=None):
    """Interactive chat with Gemini — full access to the game library data."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-api-key":
        return "Gemini API key not configured. Add GEMINI_API_KEY to your .env file."

    client = genai.Client(api_key=api_key)
    game_list, stats = _build_library_context(games)

    system_instruction = f"""You are GigaLib Assistant — an elite video game curator, backlog strategist, and gaming culture expert with FULL access to the user's game library database.

## YOUR CAPABILITIES:
1. **Recommend games** based on mood, time available, genre preference, or any criteria
2. **Query the library** — answer questions like "how many horror games do I own?", "what's my most played platform?", "list all games I haven't touched"
3. **Analyze patterns** — identify gaming habits, suggest what's missing, notice trends
4. **Compare games** — help decide between two options the user is considering
5. **Track progress** — estimate how long to finish a game based on current playtime vs HLTB data
6. **General gaming chat** — discuss games, lore, upcoming content, strategies

## LIBRARY STATS (live data):
{json.dumps(stats, indent=2)}

## FULL GAME LIBRARY ({stats['total_games']} games):
{json.dumps(game_list, indent=2)}

## RULES:
- You have the ENTIRE library loaded. You can search, filter, count, and analyze it freely.
- When recommending, pull FROM their library unless they ask about new purchases.
- If they ask data questions ("how many...?", "list all...?", "which games...?"), query the library data above and give precise answers.
- If a game has a personal review, treat it as ground truth about the user's feelings.
- Games with is_installed=true can be launched immediately. Prefer these for "play right now" requests.
- Be conversational, witty, and opinionated. You're a gaming buddy, not a search engine.
- Keep responses concise but rich. No fluff."""

    # Build conversation messages
    contents = []
    if history:
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    import time

    models_to_try = ["gemini-3.5-flash", "gemini-2.0-flash"]
    last_error = None

    for model in models_to_try:
        for attempt in range(2):
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
                if "429" in err_str or "503" in err_str or "RESOURCE_EXHAUSTED" in err_str or "UNAVAILABLE" in err_str:
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break  # try next model
                else:
                    return f"Gemini error: {err_str}"

    return "I'm being rate-limited right now. Give me a moment and try again!"
