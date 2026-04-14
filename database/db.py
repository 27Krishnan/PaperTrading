from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from database.models import Base
from loguru import logger

DATABASE_URL = "sqlite:///./papertrading.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    from database.db_migrate import run_migrations
    run_migrations(engine)
    logger.info("Database initialized")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session() -> Session:
    return SessionLocal()
