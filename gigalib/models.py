import uuid
from datetime import datetime

from gigalib import db


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    platform = db.Column(db.String(50), nullable=False)  # steam, xbox, ea, ubisoft
    app_id = db.Column(db.String(100))  # platform-specific ID
    image_url = db.Column(db.String(500))
    playtime_hours = db.Column(db.Float, default=0)
    last_played = db.Column(db.String(50))
    genre = db.Column(db.String(200))
    tags = db.Column(
        db.String(500)
    )  # comma-separated tags e.g. "co-op,roguelike,indie"
    description = db.Column(db.Text)  # short game description
    review = db.Column(db.Text)  # user's personal review/notes
    is_installed = db.Column(db.Boolean, default=False)
    critic_rating = db.Column(db.Float)  # 0-100 from IGDB
    rating_tier = db.Column(db.String(50))  # e.g. "Mighty", "Strong"
    main_story_hours = db.Column(db.Float)  # HLTB main story
    completionist_hours = db.Column(db.Float)  # HLTB completionist
    is_multiplayer = db.Column(db.Boolean, default=False)
    is_gamepass = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "platform": self.platform,
            "app_id": self.app_id,
            "image_url": self.image_url,
            "playtime_hours": self.playtime_hours,
            "last_played": self.last_played,
            "genre": self.genre,
            "tags": self.tags,
            "description": self.description,
            "review": self.review,
            "is_installed": self.is_installed,
            "critic_rating": self.critic_rating,
            "rating_tier": self.rating_tier,
            "main_story_hours": self.main_story_hours,
            "completionist_hours": self.completionist_hours,
            "is_multiplayer": self.is_multiplayer,
            "is_gamepass": self.is_gamepass,
        }


class Conversation(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = db.Column(db.String(200), nullable=False, default="New conversation")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    messages = db.relationship(
        "ConversationMessage",
        backref="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )

    def to_summary_dict(self):
        last_message = self.messages[-1] if self.messages else None
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "message_count": len(self.messages),
            "last_message": last_message.content if last_message else "",
        }


class ConversationMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.String(36),
        db.ForeignKey("conversation.id"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AccountLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    remote_user_id = db.Column(db.String(100), index=True)
    handle = db.Column(db.String(80), index=True)
    display_name = db.Column(db.String(120))
    access_token = db.Column(db.Text)
    service_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    last_sync_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            "id": self.id,
            "remote_user_id": self.remote_user_id,
            "handle": self.handle,
            "service_url": self.service_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_sync_at": (
                self.last_sync_at.isoformat() if self.last_sync_at else None
            ),
            "connected": bool(self.access_token),
        }


class SocialPrivacySettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    share_playtime = db.Column(db.Boolean, default=False, nullable=False)
    share_last_played = db.Column(db.Boolean, default=False, nullable=False)
    share_reviews = db.Column(db.Boolean, default=False, nullable=False)
    visibility = db.Column(db.String(30), default="friends", nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {
            "share_playtime": self.share_playtime,
            "share_last_played": self.share_last_played,
            "visibility": self.visibility,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Friend(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    remote_user_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    handle = db.Column(db.String(80), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False)
    avatar_url = db.Column(db.String(500))
    friendship_status = db.Column(db.String(30), default="accepted", nullable=False)
    last_library_sync_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "remote_user_id": self.remote_user_id,
            "handle": self.handle,
            "avatar_url": self.avatar_url,
            "friendship_status": self.friendship_status,
            "last_library_sync_at": (
                self.last_library_sync_at.isoformat()
                if self.last_library_sync_at
                else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    remote_request_id = db.Column(db.String(100), unique=True, index=True)
    direction = db.Column(db.String(20), nullable=False)
    remote_user_id = db.Column(db.String(100), nullable=False, index=True)
    handle = db.Column(db.String(80), nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(30), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "remote_request_id": self.remote_request_id,
            "direction": self.direction,
            "remote_user_id": self.remote_user_id,
            "handle": self.handle,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class RemoteLibrarySnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    remote_user_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    snapshot_version = db.Column(db.Integer, default=1, nullable=False)
    snapshot_json = db.Column(db.Text, nullable=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    library_synced_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            "id": self.id,
            "remote_user_id": self.remote_user_id,
            "snapshot_version": self.snapshot_version,
            "snapshot_json": self.snapshot_json,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "library_synced_at": (
                self.library_synced_at.isoformat() if self.library_synced_at else None
            ),
        }
