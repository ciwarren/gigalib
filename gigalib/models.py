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
