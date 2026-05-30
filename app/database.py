from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/store.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS events (
                event_id    TEXT PRIMARY KEY,
                store_id    TEXT NOT NULL,
                camera_id   TEXT NOT NULL,
                visitor_id  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                zone_id     TEXT,
                dwell_ms    INTEGER DEFAULT 0,
                is_staff    INTEGER DEFAULT 0,
                confidence  REAL,
                metadata    TEXT
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_store_ts
            ON events(store_id, timestamp)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_visitor
            ON events(visitor_id)
        """))
        conn.commit()