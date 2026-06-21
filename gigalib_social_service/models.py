from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    handle = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    last_seen_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "handle": self.handle,
            "last_seen_at": (
                self.last_seen_at.isoformat() if self.last_seen_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AccessToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = db.Column(db.DateTime)

    user = db.relationship("User", backref=db.backref("tokens", lazy=True))


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    receiver_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    sender = db.relationship("User", foreign_keys=[sender_user_id])
    receiver = db.relationship("User", foreign_keys=[receiver_user_id])

    __table_args__ = (
        db.UniqueConstraint(
            "sender_user_id",
            "receiver_user_id",
            "status",
            name="uq_pending_friend_request",
        ),
    )

    def to_dict_for(self, user):
        other = self.receiver if self.sender_user_id == user.id else self.sender
        return {
            "id": self.id,
            "direction": "outgoing" if self.sender_user_id == user.id else "incoming",
            "user": other.to_dict(),
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Friendship(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_a_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    user_b_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user_a = db.relationship("User", foreign_keys=[user_a_id])
    user_b = db.relationship("User", foreign_keys=[user_b_id])

    __table_args__ = (
        db.UniqueConstraint("user_a_id", "user_b_id", name="uq_friendship_pair"),
    )

    @staticmethod
    def ordered_pair(user_id, other_user_id):
        return tuple(sorted((user_id, other_user_id)))

    def other_user(self, user):
        return self.user_b if self.user_a_id == user.id else self.user_a

    def to_dict_for(self, user):
        other = self.other_user(user)
        return {
            "id": self.id,
            "user": other.to_dict(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LibrarySnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False
    )
    snapshot_version = db.Column(db.Integer, default=1, nullable=False)
    snapshot_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = db.relationship(
        "User", backref=db.backref("library_snapshot", uselist=False)
    )

    def to_summary_dict(self):
        return {
            "user_id": self.user_id,
            "snapshot_version": self.snapshot_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SocialMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    receiver_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    read_at = db.Column(db.DateTime)

    sender = db.relationship("User", foreign_keys=[sender_user_id])
    receiver = db.relationship("User", foreign_keys=[receiver_user_id])

    def to_dict_for(self, user):
        return {
            "id": self.id,
            "direction": "outgoing" if self.sender_user_id == user.id else "incoming",
            "sender": self.sender.to_dict(),
            "receiver": self.receiver.to_dict(),
            "body": self.body,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
        }
