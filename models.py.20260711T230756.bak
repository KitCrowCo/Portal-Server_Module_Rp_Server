from sqlalchemy import Column, Integer, String, Boolean, Text, JSON, DateTime, ForeignKey, UniqueConstraint, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime, os

RP_DB_PATH = os.getenv("RP_DB_URL", "sqlite:////app/data/rp_server/rp_server.db")
engine = create_engine(RP_DB_PATH, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class UserSession(Base):
    """Persistent per-user RP state: active room, persona, preferences."""
    __tablename__ = "user_sessions"
    username       = Column(String, primary_key=True)
    role           = Column(String, default="user")
    current_room   = Column(String, default="Public")   # room id
    current_persona = Column(String, default="")        # persona name (display name)
    settings       = Column(JSON, default={"font_size": 14, "theme": "dark"})
    info           = Column(JSON, default={})           # info: extensible JSON for future per-user room-specific preferences

class Channel(Base):
    __tablename__ = "channels"
    id         = Column(String, primary_key=True)   # safe slug, used in room_id as suffix
    title      = Column(String, nullable=False)
    owner      = Column(String, nullable=False)
    info       = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Room(Base):
    """A persistent chat room. public rooms are admin-managed; private rooms by owner."""
    __tablename__ = "rooms"
    id          = Column(String, primary_key=True, index=True)
    owner       = Column(String, nullable=True)         # username of creator
    room_type   = Column(String, default="public")      # "public" | "private"
    background  = Column(String, default="")            # asset path for stage bg
    title       = Column(String, default="")            # display name (defaults to id)
    description = Column(Text, default="")
    info        = Column(JSON, default={})              # extensible: rules, theme overrides, etc.
    # channel_id = Column(String, ForeignKey("channels.id", ondelete="SET NULL"), nullable=True)

class RoomMembership(Base):
    """Explicit membership for private rooms. Public rooms have no membership rows."""
    __tablename__ = "room_membership"
    __table_args__ = (UniqueConstraint("room_id", "username"),)
    id       = Column(Integer, primary_key=True, autoincrement=True)
    room_id  = Column(String, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    username = Column(String, nullable=False)
    role     = Column(String, default="member")         # "member" | "moderator"

class Persona(Base):
    """A display identity owned by a user. Names are unique per owner only."""
    __tablename__ = "personas"
    __table_args__ = (UniqueConstraint("owner_username", "name"),)
    id             = Column(Integer, primary_key=True, autoincrement=True)
    name           = Column(String, nullable=False)
    owner_username = Column(String, nullable=False)
    description    = Column(Text, default="")
    sprite         = Column(String, default="")         # asset path for persona icon
    ai_enabled     = Column(Boolean, default=False)
    info           = Column(JSON, default={})           # extensible: color, pronouns, etc.

class Message(Base):
    """A chat message in a room. Soft-delete via deleted flag. Edit history via edited_at."""
    __tablename__ = "messages"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    room_id      = Column(String, ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    user_name    = Column(String, nullable=False)       # actual account (for audit)
    persona_name = Column(String, nullable=False)       # display name in chat
    content      = Column(Text, nullable=False)
    created_at   = Column(DateTime, default=datetime.datetime.utcnow)
    edited_at    = Column(DateTime, default=datetime.datetime.utcnow)
    deleted      = Column(Boolean, default=False)
    info         = Column(JSON, default={})             # extensible: reactions, attachments, etc.
    story_time   = Column(DateTime, nullable=True)      # explicit story order; display uses this over created_at

class Asset(Base):
    """Tracks uploaded files (backgrounds, sprites) per user."""
    __tablename__ = "assets"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    owner      = Column(String, default="system")
    filename   = Column(String, index=True)             # relative path under asset dir
    asset_type = Column(String, default="background")   # "background" | "sprite" | "other"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

# This is probably not needed and handled by the base server
class Notification(Base):
    """Queued push notifications for PWA delivery."""
    __tablename__ = "notifications"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    username   = Column(String, nullable=False, index=True)
    endpoint   = Column(Text, nullable=False, unique=True)
    keys       = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class RoomWatch(Base):
    """Rooms watched for the purpose of notifications."""
    __tablename__ = "room_watch"
    __table_args__ = (UniqueConstraint("room_id", "username"),)
    id       = Column(Integer, primary_key=True, autoincrement=True)
    room_id  = Column(String, ForeignKey("rooms.id", ondelete="CASCADE"))
    username = Column(String, nullable=False)
    notify   = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)

def ensure_db_column(engine, table="users", column="custom_theme", ctype="JSON"):
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if table in inspector.get_table_names():
        if column not in [c['name'] for c in inspector.get_columns(table)]:
            print(f"Migration: adding '{column}' to '{table}'...")
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}"))
                conn.commit()

# Add new columns to existing tables safely (idempotent)
for table, col, ctype in [("messages", "story_time", "DATETIME"), ("rooms", "channel_id", "VARCHAR")]:
    ensure_db_column(engine, table, col, ctype)