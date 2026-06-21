import json
import os
import re
import threading
import uuid
from datetime import datetime

from flask import Blueprint, current_app, jsonify, render_template, request

from gigalib import db
from gigalib.assistant import ask_assistant
from gigalib.enricher import enrich_game
from gigalib.models import (Conversation, ConversationMessage, Friend,
                            FriendRequest, Game, RemoteLibrarySnapshot)
from gigalib.platforms import sync_all_platforms
from gigalib.social import (SocialServiceError, accept_remote_friend_request,
                            canonical_title, compare_remote_friends,
                            compare_with_friend, compare_with_friends,
                            decline_remote_friend_request,
                            fetch_remote_friend_library, get_account_link,
                            get_or_create_privacy_settings,
                            list_remote_friend_requests, list_remote_friends,
                            list_remote_messages, login_remote_account,
                            register_remote_account, search_remote_users,
                            send_remote_friend_request, send_remote_message,
                            social_overview, sync_local_social_snapshot,
                            sync_remote_social_snapshot,
                            update_privacy_settings, update_remote_presence)

main_bp = Blueprint("main", __name__)

_ENRICH_STATUS_LOCK = threading.Lock()
_ENRICH_STATUS = {
    "running": False,
    "completed": False,
    "processed": 0,
    "total": 0,
    "enriched": 0,
    "errors": 0,
    "remaining": 0,
    "message": "",
}


def _unenriched_query():
    return Game.query.filter((Game.description == None) | (Game.description == ""))


def _set_enrich_status(**updates):
    with _ENRICH_STATUS_LOCK:
        _ENRICH_STATUS.update(updates)


def _get_enrich_status():
    with _ENRICH_STATUS_LOCK:
        return dict(_ENRICH_STATUS)


def _run_enrich_job(app, total):
    processed = 0
    enriched_count = 0
    errors = 0

    with app.app_context():
        try:
            games = _unenriched_query().all()
            for game in games:
                try:
                    if enrich_game(game):
                        enriched_count += 1
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    errors += 1

                processed += 1
                _set_enrich_status(
                    processed=processed,
                    enriched=enriched_count,
                    errors=errors,
                    remaining=max(total - processed, 0),
                    message=f"Processed {processed} of {total} games",
                )

            still_missing = _unenriched_query().count()
            _set_enrich_status(
                running=False,
                completed=True,
                processed=processed,
                total=total,
                enriched=enriched_count,
                errors=errors,
                remaining=still_missing,
                message="Enrichment complete",
            )
        except Exception as exc:
            db.session.rollback()
            _set_enrich_status(
                running=False,
                completed=True,
                processed=processed,
                total=total,
                enriched=enriched_count,
                errors=errors + 1,
                remaining=_unenriched_query().count(),
                message=f"Enrichment failed: {exc}",
            )
        finally:
            db.session.remove()


def _conversation_title(message):
    text = " ".join((message or "").split()).strip()
    if not text:
        return "New conversation"
    return text[:80]


def _conversation_messages(conversation):
    return [msg.to_dict() for msg in conversation.messages]


def _conversation_summary(conversation):
    return conversation.to_summary_dict()


def _title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


_NON_GAME_TITLES = {
    ("xbox", "all console games"),
}


def _is_non_game_record(game):
    return (
        (game.platform or "").lower(),
        (game.title or "").strip().lower(),
    ) in _NON_GAME_TITLES


def _game_records_query():
    return Game.query.order_by(Game.title.asc()).all()


def _visible_game_records(games):
    return [game for game in games if not _is_non_game_record(game)]


def _dedupe_rank(game):
    platform = (game.platform or "").lower()
    platform_rank = {
        "all": 0,
        "steam": 1,
        "ea": 2,
        "ubisoft": 3,
        "xbox": 4,
    }.get(platform, 5)

    return (
        platform_rank,
        not game.is_installed,
        -(game.playtime_hours or 0),
        not bool(game.image_url),
        game.id,
    )


def _deduped_game_records(games):
    visible_games = _visible_game_records(games)
    preferred_by_title = {}

    for game in visible_games:
        key = _title_key(game.title)
        if not key:
            key = f"{game.platform}:{game.app_id or game.id}"

        current = preferred_by_title.get(key)
        if current is None or _dedupe_rank(game) < _dedupe_rank(current):
            preferred_by_title[key] = game

    return sorted(
        preferred_by_title.values(), key=lambda game: (game.title or "").lower()
    )


def _deduped_title_count(games):
    return len(
        {
            _title_key(game.title)
            for game in _visible_game_records(games)
            if _title_key(game.title)
        }
    )


def _duplicate_title_count(games):
    seen = set()
    duplicates = set()
    for game in _visible_game_records(games):
        key = _title_key(game.title)
        if not key:
            continue
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return len(duplicates)


def _library_stats(games):
    visible_games = _visible_game_records(games)
    deduped_games = _deduped_game_records(visible_games)
    return {
        "total_games": len(deduped_games),
        "raw_games": len(visible_games),
        "displayed_games": len(deduped_games),
        "duplicate_titles": _duplicate_title_count(visible_games),
        "installed_games": sum(1 for game in deduped_games if game.is_installed),
        "unplayed_games": sum(
            1 for game in deduped_games if (game.playtime_hours or 0) == 0
        ),
        "total_playtime_hours": round(
            sum((game.playtime_hours or 0) for game in deduped_games), 1
        ),
    }


def _mentioned_handles(message):
    return sorted(
        {
            match.lower()
            for match in re.findall(r"@([a-zA-Z0-9_\-]{3,40})", message or "")
        }
    )


def _friend_library_context_from_snapshot(friend, snapshot_payload, source):
    snapshot = snapshot_payload.get("snapshot", snapshot_payload)
    games = snapshot.get("games", []) if isinstance(snapshot, dict) else []
    sorted_games = sorted(
        games,
        key=lambda item: (
            not bool(item.get("is_installed")),
            -(item.get("playtime_hours") or 0),
            (item.get("display_title") or "").lower(),
        ),
    )
    included_games = []
    for item in sorted_games[:150]:
        included_games.append(
            {
                "title": item.get("display_title") or item.get("canonical_title"),
                "platforms": item.get("platforms") or [],
                "is_installed": bool(item.get("is_installed")),
                "is_multiplayer": bool(item.get("is_multiplayer")),
                "playtime_hours": item.get("playtime_hours"),
                "genre": item.get("genre"),
                "tags": item.get("tags") or [],
                "critic_rating": item.get("critic_rating"),
                "rating_tier": item.get("rating_tier"),
            }
        )

    return {
        "handle": friend.handle,
        "source": source,
        "synced_at": snapshot_payload.get("synced_at")
        or (
            friend.last_library_sync_at.isoformat()
            if friend.last_library_sync_at
            else None
        ),
        "total_games": len(games),
        "included_games": len(included_games),
        "omitted_games": max(0, len(games) - len(included_games)),
        "installed_games": sum(1 for item in games if item.get("is_installed")),
        "multiplayer_games": sum(1 for item in games if item.get("is_multiplayer")),
        "games": included_games,
    }


def _build_social_context_for_message(message):
    handles = _mentioned_handles(message)
    if not handles:
        return None

    context = {"mentioned_handles": handles, "friend_libraries": [], "unavailable": []}
    for handle in handles:
        friend = Friend.query.filter_by(handle=handle).first()
        if not friend:
            context["unavailable"].append(
                {
                    "handle": handle,
                    "reason": "No accepted friend with this handle is cached locally",
                }
            )
            continue

        try:
            payload = fetch_remote_friend_library(friend.id)
            context["friend_libraries"].append(
                _friend_library_context_from_snapshot(friend, payload, "remote")
            )
            continue
        except SocialServiceError as exc:
            cached = RemoteLibrarySnapshot.query.filter_by(
                remote_user_id=friend.remote_user_id
            ).first()
            if not cached:
                context["unavailable"].append({"handle": handle, "reason": exc.message})
                continue

            try:
                snapshot = json.loads(cached.snapshot_json)
            except ValueError:
                context["unavailable"].append(
                    {"handle": handle, "reason": "Cached friend library is unreadable"}
                )
                continue

            context["friend_libraries"].append(
                _friend_library_context_from_snapshot(
                    friend,
                    {
                        "snapshot": snapshot,
                        "synced_at": (
                            cached.library_synced_at.isoformat()
                            if cached.library_synced_at
                            else None
                        ),
                    },
                    "cached",
                )
            )

    return context


def _queue_app_open_sync():
    from gigalib.app import trigger_open_sync

    try:
        trigger_open_sync(current_app._get_current_object())
    except Exception as exc:
        current_app.logger.info("App-open sync was not queued: %s", exc)


@main_bp.route("/")
def index():
    all_games = _game_records_query()
    games = _deduped_game_records(all_games)
    platforms = (
        db.session.query(Game.platform).distinct().order_by(Game.platform.asc()).all()
    )
    platform_list = [p[0] for p in platforms]
    stats = _library_stats(all_games)
    _queue_app_open_sync()
    return render_template(
        "index.html",
        games=games,
        platforms=platform_list,
        stats=stats,
        canonical_title=canonical_title,
    )


@main_bp.route("/conversations")
def conversations_page():
    _queue_app_open_sync()
    return render_template("conversations.html")


@main_bp.route("/social")
def social_page():
    _queue_app_open_sync()
    return render_template("social.html")


@main_bp.route("/api/conversations")
def list_conversations():
    conversations = Conversation.query.order_by(Conversation.updated_at.desc()).all()
    return jsonify([_conversation_summary(conv) for conv in conversations])


@main_bp.route("/api/conversations/<conversation_id>")
def get_conversation(conversation_id):
    conversation = db.session.get(Conversation, conversation_id)
    if not conversation:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(
        {
            "conversation": _conversation_summary(conversation),
            "messages": _conversation_messages(conversation),
        }
    )


@main_bp.route("/api/social/overview")
def get_social_overview():
    games = _deduped_game_records(_game_records_query())
    return jsonify(social_overview(games))


@main_bp.route("/api/social/account")
def get_social_account():
    account = get_account_link()
    settings = get_or_create_privacy_settings()
    return jsonify(
        {
            "account": account.to_dict() if account else None,
            "privacy": settings.to_dict(),
        }
    )


@main_bp.route("/api/social/register", methods=["POST"])
def register_social_account():
    data = request.get_json() or {}
    handle = (data.get("handle") or "").strip().lstrip("@").lower()
    password = data.get("password") or ""
    service_url = (data.get("service_url") or "").strip() or None

    if not handle:
        return jsonify({"error": "Handle is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not re.match(r"^[a-zA-Z0-9_\-]{3,40}$", handle):
        return (
            jsonify(
                {
                    "error": "Handle must be 3-40 letters, numbers, underscores, or hyphens"
                }
            ),
            400,
        )

    try:
        account = register_remote_account(handle, password, service_url=service_url)
        return (
            jsonify(
                {
                    "account": account.to_dict(),
                    "message": "Created your social account.",
                }
            ),
            201,
        )
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/login", methods=["POST"])
def login_social_account():
    data = request.get_json() or {}
    handle = (data.get("handle") or "").strip().lstrip("@").lower()
    password = data.get("password") or ""
    service_url = (data.get("service_url") or "").strip() or None

    if not handle or not password:
        return jsonify({"error": "Handle and password are required"}), 400

    try:
        account = login_remote_account(handle, password, service_url=service_url)
        return jsonify(
            {
                "account": account.to_dict(),
                "message": "Signed in to your social account.",
            }
        )
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/logout", methods=["POST"])
def logout_social_account():
    account = get_account_link()
    if account:
        account.access_token = None
        db.session.commit()
    return jsonify({"ok": True})


@main_bp.route("/api/social/privacy", methods=["PUT"])
def update_social_privacy():
    settings = update_privacy_settings(request.get_json() or {})
    return jsonify(settings.to_dict())


@main_bp.route("/api/social/sync", methods=["POST"])
def sync_social_library():
    games = _deduped_game_records(_game_records_query())
    try:
        return jsonify(sync_remote_social_snapshot(games))
    except SocialServiceError as exc:
        if exc.status_code == 400:
            return jsonify({"error": exc.message}), exc.status_code
        fallback = sync_local_social_snapshot(games)
        fallback["warning"] = exc.message
        return jsonify(fallback), 202


@main_bp.route("/api/social/presence", methods=["POST"])
@main_bp.route("/api/social/check-in", methods=["POST"])
def update_social_presence():
    try:
        return jsonify(update_remote_presence())
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friends")
def list_social_friends():
    try:
        return jsonify(list_remote_friends())
    except SocialServiceError:
        overview = social_overview(_deduped_game_records(_game_records_query()))
        return jsonify(overview["friends"])


@main_bp.route("/api/social/friends/<int:friend_id>/library")
def get_social_friend_library(friend_id):
    try:
        return jsonify(fetch_remote_friend_library(friend_id))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friends/<int:friend_id>/messages")
def list_social_messages(friend_id):
    try:
        return jsonify(list_remote_messages(friend_id))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friends/<int:friend_id>/messages", methods=["POST"])
def send_social_message(friend_id):
    data = request.get_json() or {}
    body = data.get("body") or ""
    try:
        return jsonify(send_remote_message(friend_id, body))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/search")
def search_social_users():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify([])
    try:
        return jsonify(search_remote_users(query))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friend-requests")
def list_social_friend_requests():
    try:
        return jsonify(list_remote_friend_requests())
    except SocialServiceError:
        requests = FriendRequest.query.order_by(FriendRequest.created_at.desc()).all()
        return jsonify([friend_request.to_dict() for friend_request in requests])


@main_bp.route("/api/social/friend-requests", methods=["POST"])
def create_social_friend_request():
    data = request.get_json() or {}
    handle = (data.get("handle") or "").strip().lstrip("@").lower()
    if not handle:
        return jsonify({"error": "Handle is required"}), 400
    try:
        return jsonify(send_remote_friend_request(handle))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friend-requests/<int:request_id>/accept", methods=["POST"])
def accept_social_friend_request(request_id):
    try:
        return jsonify(accept_remote_friend_request(request_id))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/friend-requests/<int:request_id>/decline", methods=["POST"])
def decline_social_friend_request(request_id):
    try:
        return jsonify(decline_remote_friend_request(request_id))
    except SocialServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@main_bp.route("/api/social/compare")
def compare_social_friend():
    friend_ids = request.args.getlist("friend_id", type=int)
    if not friend_ids:
        return jsonify({"error": "friend_id is required"}), 400

    games = _deduped_game_records(_game_records_query())
    try:
        return jsonify(compare_remote_friends(friend_ids))
    except SocialServiceError:
        pass

    if len(friend_ids) == 1:
        comparison = compare_with_friend(friend_ids[0], games)
    else:
        comparison = compare_with_friends(friend_ids, games)

    if comparison is None:
        return jsonify({"error": "Friend not found"}), 404
    return jsonify(comparison)


@main_bp.route("/sync", methods=["POST"])
def sync_games():
    """Sync games from all connected platforms."""
    results = sync_all_platforms()
    return jsonify(results)


@main_bp.route("/games")
def get_games():
    """Get all games, optionally filtered by platform."""
    platform = request.args.get("platform")
    if platform:
        games = _visible_game_records(
            Game.query.filter_by(platform=platform).order_by(Game.title.asc()).all()
        )
    else:
        games = _deduped_game_records(_game_records_query())
    return jsonify([g.to_dict() for g in games])


@main_bp.route("/games/add", methods=["POST"])
def add_game():
    """Manually add a game to the library."""
    data = request.get_json()
    title = data.get("title", "").strip()
    platform = data.get("platform", "").strip()
    genre = data.get("genre", "").strip() or None

    if not title or not platform:
        return jsonify({"error": "Title and platform are required"}), 400

    game = Game(title=title, platform=platform, genre=genre)
    db.session.add(game)
    db.session.commit()
    return jsonify(game.to_dict()), 201


@main_bp.route("/enrich", methods=["POST"])
def enrich():
    """Start background enrichment and report job status."""
    current_status = _get_enrich_status()
    if current_status["running"]:
        return jsonify(current_status), 202

    total = _unenriched_query().count()
    if total == 0:
        status = {
            "running": False,
            "completed": True,
            "processed": 0,
            "total": 0,
            "enriched": 0,
            "errors": 0,
            "remaining": 0,
            "message": "No games need enrichment",
        }
        _set_enrich_status(**status)
        return jsonify(status)

    _set_enrich_status(
        running=True,
        completed=False,
        processed=0,
        total=total,
        enriched=0,
        errors=0,
        remaining=total,
        message=f"Starting enrichment for {total} games",
    )

    app = current_app._get_current_object()
    threading.Thread(target=_run_enrich_job, args=(app, total), daemon=True).start()
    return jsonify(_get_enrich_status()), 202


@main_bp.route("/api/enrich-status")
def enrich_status():
    return jsonify(_get_enrich_status())


@main_bp.route("/games/<int:game_id>")
def get_game(game_id):
    """Get a single game's full details."""
    game = Game.query.get_or_404(game_id)
    return jsonify(game.to_dict())


@main_bp.route("/games/<int:game_id>/launch", methods=["POST"])
def launch_game(game_id):
    """Launch a game via its platform protocol."""
    game = Game.query.get_or_404(game_id)
    launch_url = None

    if game.platform == "steam" and game.app_id:
        launch_url = f"steam://rungameid/{game.app_id}"
    elif game.platform == "ea" and game.app_id:
        if game.app_id.startswith("Origin.OFR"):
            launch_url = f"origin2://game/launch?offerIds={game.app_id}"
        else:
            # GUID-based older Origin games
            launch_url = f"link2ea://launchgame/{game.app_id}"
    elif game.platform == "ubisoft" and game.app_id:
        launch_url = f"uplay://launch/{game.app_id}"

    if not launch_url:
        return jsonify({"error": "No launch URL available for this game"}), 400

    try:
        os.startfile(launch_url)
        return jsonify({"status": "launched", "url": launch_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/assistant", methods=["POST"])
def assistant():
    """Interactive chat with the AI assistant."""
    data = request.get_json() or {}
    message = data.get("message", "")
    games = _deduped_game_records(_game_records_query())
    conversation_id = data.get("conversation_id")
    history = data.get("history", [])

    conversation = None
    if conversation_id:
        conversation = db.session.get(Conversation, conversation_id)

    if conversation and conversation.messages:
        history = [
            {"role": msg.role, "content": msg.content} for msg in conversation.messages
        ][-8:]

    social_context = _build_social_context_for_message(message)
    response = ask_assistant(
        message,
        games,
        history=history,
        social_context=social_context,
    )

    if not conversation:
        conversation = Conversation(
            id=conversation_id or str(uuid.uuid4()),
            title=_conversation_title(message),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(conversation)
    else:
        if conversation.title == "New conversation":
            conversation.title = _conversation_title(message)
        conversation.updated_at = datetime.utcnow()

    db.session.add(
        ConversationMessage(
            conversation_id=conversation.id,
            role="user",
            content=message,
        )
    )
    db.session.add(
        ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=response,
        )
    )
    db.session.commit()

    return jsonify({"response": response, "conversation_id": conversation.id})
