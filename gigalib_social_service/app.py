import argparse
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

from gigalib_social_service.models import (AccessToken, FriendRequest,
                                           Friendship, LibrarySnapshot,
                                           SocialMessage, User, db)

load_dotenv()

HANDLE_RE = re.compile(r"^[a-z0-9_-]{3,40}$")
DEFAULT_DATABASE_URI = "sqlite:///gigalib-social.db"
ONLINE_WINDOW_SECONDS = 90


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.getenv("SOCIAL_SECRET_KEY", os.getenv("SECRET_KEY", "dev-key")),
        SQLALCHEMY_DATABASE_URI=os.getenv("SOCIAL_DATABASE_URL", DEFAULT_DATABASE_URI),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=int(os.getenv("SOCIAL_MAX_CONTENT_LENGTH", "2097152")),
    )
    if config:
        app.config.update(config)

    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        ensure_schema()

    register_routes(app)
    return app


def register_routes(app):
    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "gigalib-social"})

    @app.post("/v1/auth/register")
    def register():
        data = request.get_json() or {}
        handle = normalize_handle(data.get("handle"))
        password = str(data.get("password") or "")
        if not handle:
            return jsonify({"error": "Handle is required"}), 400
        if not valid_password(password):
            return jsonify({"error": "Password must be at least 8 characters"}), 400
        if not HANDLE_RE.match(handle):
            return (
                jsonify(
                    {
                        "error": "Handle must be 3-40 lowercase letters, numbers, underscores, or hyphens"
                    }
                ),
                400,
            )

        existing = User.query.filter_by(handle=handle).first()
        if existing:
            if not existing.password_hash:
                existing.password_hash = generate_password_hash(password)
                existing.updated_at = datetime.utcnow()
                token = create_access_token(existing)
                db.session.commit()
                return jsonify({"user": existing.to_dict(), "access_token": token}), 200
            return jsonify({"error": "Handle is already registered"}), 409

        user = User(handle=handle, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.flush()
        token = create_access_token(user)
        db.session.commit()
        return jsonify({"user": user.to_dict(), "access_token": token}), 201

    @app.post("/v1/auth/login")
    def login():
        data = request.get_json() or {}
        handle = normalize_handle(data.get("handle"))
        password = str(data.get("password") or "")
        if not handle or not password:
            return jsonify({"error": "Handle and password are required"}), 400

        user = User.query.filter_by(handle=handle).first()
        if user and not user.password_hash:
            return (
                jsonify(
                    {
                        "error": "Create an account with this handle once to set a password"
                    }
                ),
                409,
            )
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({"error": "Invalid handle or password"}), 401

        token = create_access_token(user)
        db.session.commit()
        return jsonify({"user": user.to_dict(), "access_token": token})

    @app.get("/v1/me")
    @require_auth
    def me():
        return jsonify({"user": g.current_user.to_dict()})

    @app.post("/v1/presence")
    @app.post("/v1/presence/check-in")
    @require_auth
    def update_presence():
        g.current_user.last_seen_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"user": user_presence_dict(g.current_user)})

    @app.post("/v1/auth/logout")
    @require_auth
    def logout():
        g.current_token.revoked_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True})

    @app.get("/v1/users/search")
    @require_auth
    def search_users():
        query = normalize_handle(request.args.get("q"))
        if not query:
            return jsonify([])
        users = (
            User.query.filter(User.handle.contains(query), User.id != g.current_user.id)
            .order_by(User.handle.asc())
            .limit(20)
            .all()
        )
        return jsonify([user.to_dict() for user in users])

    @app.put("/v1/library/snapshot")
    @require_auth
    def put_library_snapshot():
        snapshot = request.get_json() or {}
        games = snapshot.get("games")
        if not isinstance(games, list):
            return jsonify({"error": "Snapshot must include a games array"}), 400

        snapshot_version = int(snapshot.get("snapshot_version") or 1)
        encoded = json.dumps(snapshot, separators=(",", ":"))
        record = LibrarySnapshot.query.filter_by(user_id=g.current_user.id).first()
        if not record:
            record = LibrarySnapshot(user_id=g.current_user.id, snapshot_json=encoded)
            db.session.add(record)
        else:
            record.snapshot_json = encoded
            record.updated_at = datetime.utcnow()
        record.snapshot_version = snapshot_version
        db.session.commit()
        return jsonify({"snapshot": record.to_summary_dict(), "game_count": len(games)})

    @app.get("/v1/friends")
    @require_auth
    def list_friends():
        friendships = friendships_for_user(g.current_user)
        return jsonify(
            [
                friendship_dict_for(friendship, g.current_user)
                for friendship in friendships
            ]
        )

    @app.get("/v1/messages")
    @require_auth
    def list_messages():
        friend_id = request.args.get("friend_id", type=int)
        if not friend_id or not are_friends(g.current_user.id, friend_id):
            return jsonify({"error": "Friend not found"}), 404
        friend = db.session.get(User, friend_id)
        if not friend:
            return jsonify({"error": "Friend not found"}), 404

        messages = (
            SocialMessage.query.filter(
                (
                    (SocialMessage.sender_user_id == g.current_user.id)
                    & (SocialMessage.receiver_user_id == friend.id)
                )
                | (
                    (SocialMessage.sender_user_id == friend.id)
                    & (SocialMessage.receiver_user_id == g.current_user.id)
                )
            )
            .order_by(SocialMessage.created_at.asc())
            .limit(100)
            .all()
        )
        now = datetime.utcnow()
        for message in messages:
            if message.receiver_user_id == g.current_user.id and not message.read_at:
                message.read_at = now
        db.session.commit()
        return jsonify(
            {
                "friend": user_presence_dict(friend),
                "messages": [
                    message.to_dict_for(g.current_user) for message in messages
                ],
            }
        )

    @app.post("/v1/messages")
    @require_auth
    def send_message():
        data = request.get_json() or {}
        friend_id = int(data.get("friend_id") or 0)
        body = " ".join(str(data.get("body") or "").split()).strip()
        if not friend_id or not are_friends(g.current_user.id, friend_id):
            return jsonify({"error": "Friend not found"}), 404
        if not body:
            return jsonify({"error": "Message is required"}), 400
        if len(body) > 2000:
            return jsonify({"error": "Message must be 2000 characters or less"}), 400

        friend = db.session.get(User, friend_id)
        if not friend:
            return jsonify({"error": "Friend not found"}), 404
        message = SocialMessage(
            sender_user_id=g.current_user.id,
            receiver_user_id=friend.id,
            body=body,
        )
        db.session.add(message)
        db.session.commit()
        return jsonify(message.to_dict_for(g.current_user)), 201

    @app.get("/v1/friends/<int:friend_id>/library")
    @require_auth
    def get_friend_library(friend_id):
        friend = db.session.get(User, friend_id)
        if not friend or not are_friends(g.current_user.id, friend.id):
            return jsonify({"error": "Friend not found"}), 404
        snapshot = LibrarySnapshot.query.filter_by(user_id=friend.id).first()
        if not snapshot:
            return jsonify({"error": "Friend has not synced a library"}), 404
        return jsonify(
            {
                "user": friend.to_dict(),
                "snapshot": json.loads(snapshot.snapshot_json),
                "synced_at": (
                    snapshot.updated_at.isoformat() if snapshot.updated_at else None
                ),
            }
        )

    @app.get("/v1/friend-requests")
    @require_auth
    def list_friend_requests():
        requests = (
            FriendRequest.query.filter(
                (
                    (FriendRequest.sender_user_id == g.current_user.id)
                    | (FriendRequest.receiver_user_id == g.current_user.id)
                ),
                FriendRequest.status == "pending",
            )
            .order_by(FriendRequest.created_at.desc())
            .all()
        )
        return jsonify(
            [friend_request.to_dict_for(g.current_user) for friend_request in requests]
        )

    @app.post("/v1/friend-requests")
    @require_auth
    def create_friend_request():
        data = request.get_json() or {}
        handle = normalize_handle(data.get("handle"))
        if not handle:
            return jsonify({"error": "Friend handle is required"}), 400
        receiver = User.query.filter_by(handle=handle).first()
        if not receiver:
            return jsonify({"error": "User not found"}), 404
        if receiver.id == g.current_user.id:
            return jsonify({"error": "You cannot friend yourself"}), 400
        if are_friends(g.current_user.id, receiver.id):
            return jsonify({"error": "You are already friends"}), 409

        existing = pending_request_between(g.current_user.id, receiver.id)
        if existing:
            return jsonify(existing.to_dict_for(g.current_user)), 200

        friend_request = FriendRequest(
            sender_user_id=g.current_user.id,
            receiver_user_id=receiver.id,
        )
        db.session.add(friend_request)
        db.session.commit()
        return jsonify(friend_request.to_dict_for(g.current_user)), 201

    @app.post("/v1/friend-requests/<int:request_id>/accept")
    @require_auth
    def accept_friend_request(request_id):
        friend_request = db.session.get(FriendRequest, request_id)
        if not friend_request or friend_request.receiver_user_id != g.current_user.id:
            return jsonify({"error": "Friend request not found"}), 404
        if friend_request.status != "pending":
            return jsonify({"error": "Friend request is not pending"}), 409

        user_a_id, user_b_id = Friendship.ordered_pair(
            friend_request.sender_user_id, friend_request.receiver_user_id
        )
        friendship = Friendship.query.filter_by(
            user_a_id=user_a_id, user_b_id=user_b_id
        ).first()
        if not friendship:
            friendship = Friendship(user_a_id=user_a_id, user_b_id=user_b_id)
            db.session.add(friendship)
        friend_request.status = "accepted"
        friend_request.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(friendship_dict_for(friendship, g.current_user))

    @app.post("/v1/friend-requests/<int:request_id>/decline")
    @require_auth
    def decline_friend_request(request_id):
        friend_request = db.session.get(FriendRequest, request_id)
        if not friend_request or friend_request.receiver_user_id != g.current_user.id:
            return jsonify({"error": "Friend request not found"}), 404
        if friend_request.status != "pending":
            return jsonify({"error": "Friend request is not pending"}), 409
        friend_request.status = "declined"
        friend_request.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(friend_request.to_dict_for(g.current_user))

    @app.post("/v1/compare")
    @require_auth
    def compare():
        data = request.get_json() or {}
        handles = [normalize_handle(handle) for handle in data.get("handles", [])]
        handles = [handle for handle in handles if handle]
        if not handles:
            return jsonify({"error": "At least one friend handle is required"}), 400

        friends = User.query.filter(User.handle.in_(handles)).all()
        missing_handles = sorted(
            set(handles).difference({friend.handle for friend in friends})
        )
        not_friends = [
            friend.handle
            for friend in friends
            if not are_friends(g.current_user.id, friend.id)
        ]
        if not_friends:
            return (
                jsonify(
                    {"error": "Can only compare with friends", "handles": not_friends}
                ),
                403,
            )

        comparison = compare_snapshots(g.current_user, friends)
        comparison["missing_handles"] = missing_handles
        return jsonify(comparison)


def normalize_handle(value):
    return str(value or "").strip().lstrip("@").lower()


def valid_password(value):
    return isinstance(value, str) and len(value) >= 8


def is_online(user):
    return bool(
        user.last_seen_at
        and datetime.utcnow() - user.last_seen_at
        <= timedelta(seconds=ONLINE_WINDOW_SECONDS)
    )


def user_presence_dict(user):
    data = user.to_dict()
    data["is_online"] = is_online(user)
    return data


def friendship_dict_for(friendship, user):
    data = friendship.to_dict_for(user)
    data["user"] = user_presence_dict(friendship.other_user(user))
    return data


def ensure_schema():
    columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
    table_name = '"user"' if db.engine.dialect.name == "postgresql" else "user"
    if "password_hash" not in columns:
        db.session.execute(
            text(f"ALTER TABLE {table_name} ADD COLUMN password_hash VARCHAR(255)")
        )
    if "last_seen_at" not in columns:
        db.session.execute(
            text(f"ALTER TABLE {table_name} ADD COLUMN last_seen_at DATETIME")
        )
    db.session.commit()


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(user):
    token = secrets.token_urlsafe(32)
    db.session.add(AccessToken(user_id=user.id, token_hash=hash_token(token)))
    return token


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return jsonify({"error": "Bearer token required"}), 401
        token_hash = hash_token(auth_header[len(prefix) :].strip())
        token = AccessToken.query.filter_by(
            token_hash=token_hash, revoked_at=None
        ).first()
        if not token:
            return jsonify({"error": "Invalid token"}), 401
        g.current_token = token
        g.current_user = token.user
        return view(*args, **kwargs)

    return wrapped


def friendships_for_user(user):
    return (
        Friendship.query.filter(
            (Friendship.user_a_id == user.id) | (Friendship.user_b_id == user.id)
        )
        .order_by(Friendship.created_at.desc())
        .all()
    )


def are_friends(user_id, other_user_id):
    user_a_id, user_b_id = Friendship.ordered_pair(user_id, other_user_id)
    return bool(
        Friendship.query.filter_by(user_a_id=user_a_id, user_b_id=user_b_id).first()
    )


def pending_request_between(user_id, other_user_id):
    return FriendRequest.query.filter(
        FriendRequest.status == "pending",
        (
            (
                (FriendRequest.sender_user_id == user_id)
                & (FriendRequest.receiver_user_id == other_user_id)
            )
            | (
                (FriendRequest.sender_user_id == other_user_id)
                & (FriendRequest.receiver_user_id == user_id)
            )
        ),
    ).first()


def snapshot_games_for_user(user):
    record = LibrarySnapshot.query.filter_by(user_id=user.id).first()
    if not record:
        return None
    snapshot = json.loads(record.snapshot_json)
    return {item["canonical_title"]: item for item in snapshot.get("games", [])}


def compare_snapshots(user, friends):
    libraries = [{"handle": user.handle, "games": snapshot_games_for_user(user)}]
    missing_snapshots = []
    for friend in friends:
        games = snapshot_games_for_user(friend)
        if games is None:
            missing_snapshots.append(friend.handle)
            continue
        libraries.append({"handle": friend.handle, "games": games})

    if any(library["games"] is None for library in libraries):
        missing_snapshots.append(user.handle)
        libraries = [library for library in libraries if library["games"] is not None]
    if len(libraries) < 2:
        return {
            "handles": [user.handle] + [friend.handle for friend in friends],
            "missing_snapshots": sorted(set(missing_snapshots)),
            "shared_games": [],
            "shared_installed_games": [],
            "shared_multiplayer_games": [],
            "best_candidates": [],
        }

    shared_keys = set(libraries[0]["games"])
    for library in libraries[1:]:
        shared_keys.intersection_update(library["games"])

    shared_games = []
    shared_installed = []
    shared_multiplayer = []
    best_candidates = []
    for key in sorted(shared_keys):
        items = [library["games"][key] for library in libraries]
        display_title = items[0].get("display_title") or key
        all_installed = all(item.get("is_installed") for item in items)
        any_multiplayer = any(item.get("is_multiplayer") for item in items)
        rating = next(
            (item.get("critic_rating") for item in items if item.get("critic_rating")),
            70,
        )
        score = (
            (3 if all_installed else 0) + (2 if any_multiplayer else 0) + rating / 50
        )

        shared_games.append(display_title)
        if all_installed:
            shared_installed.append(display_title)
        if any_multiplayer:
            shared_multiplayer.append(display_title)
        best_candidates.append(
            {
                "title": display_title,
                "score": round(score, 2),
                "reason": "Everyone owns it"
                + (", everyone has it installed" if all_installed else "")
                + (", and it looks multiplayer-friendly" if any_multiplayer else ""),
            }
        )

    best_candidates.sort(key=lambda item: item["score"], reverse=True)
    return {
        "handles": [library["handle"] for library in libraries],
        "missing_snapshots": sorted(set(missing_snapshots)),
        "shared_games": shared_games,
        "shared_installed_games": shared_installed,
        "shared_multiplayer_games": shared_multiplayer,
        "best_candidates": best_candidates[:10],
    }


def main():
    parser = argparse.ArgumentParser(description="Run the GigaLib social API")
    parser.add_argument("--host", default=os.getenv("SOCIAL_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("SOCIAL_PORT", "8081"))
    )
    args = parser.parse_args()
    app = create_app()

    try:
        from waitress import serve

        print(f"Starting GigaLib social API on http://{args.host}:{args.port}")
        serve(app, host=args.host, port=args.port)
    except ImportError:
        print("waitress not installed, using Flask dev server")
        app.run(host=args.host, port=args.port, debug=False)
