import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("kudridely_site_api")


def normalize_database_url(database_url: str):
    """Convert Neon/libpq PostgreSQL URLs to SQLAlchemy's asyncpg URL format."""
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    url = make_url(database_url)
    if url.drivername in {"postgresql", "postgres"}:
        query = dict(url.query)
        if "sslmode" in query and "ssl" not in query:
            query["ssl"] = query.pop("sslmode")
        else:
            query.pop("sslmode", None)
        query.pop("channel_binding", None)
        url = url.set(drivername="postgresql+asyncpg", query=query)
    return url


class Base(DeclarativeBase):
    pass


class WebsiteInquiry(Base):
    """Must match app.db.WebsiteInquiry in the Telegram bot project."""
    __tablename__ = "website_inquiries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_name: Mapped[str] = mapped_column(String(150))
    contact: Mapped[str] = mapped_column(String(200))
    service_name: Mapped[str] = mapped_column(String(200), default="")
    desired_date: Mapped[str] = mapped_column(String(30), default="")
    desired_time: Mapped[str] = mapped_column(String(30), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class InquiryPayload(BaseModel):
    client_name: str = Field(min_length=1, max_length=150)
    contact: str = Field(min_length=3, max_length=200)
    service_name: str = Field(default="", max_length=200)
    desired_date: str = Field(default="", max_length=30)
    desired_time: str = Field(default="", max_length=30)
    comment: str = Field(default="", max_length=2000)

    @field_validator("client_name", "contact")
    @classmethod
    def required_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не должно быть пустым")
        return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.getenv("DATABASE_URL", "").strip()
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_ids = {
        int(part.strip())
        for part in os.getenv("ADMIN_IDS", "").split(",")
        if part.strip().isdigit()
    }

    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not admin_ids:
        raise RuntimeError("ADMIN_IDS must contain at least one Telegram numeric ID")

    engine = create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    except Exception:
        await engine.dispose()
        logger.exception("Could not connect to the database or create website_inquiries")
        raise

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.admin_ids = admin_ids
    app.state.bot = Bot(
        bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Kudridely website API started")
    try:
        yield
    finally:
        await app.state.bot.session.close()
        await engine.dispose()


app = FastAPI(title="Kudridely Website API", version="1.0.0", lifespan=lifespan)

# Keep permissive for initial testing from a local HTML file. Restrict this to
# the published website's origin after its final URL is known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
async def health():
    return {"ok": True, "service": "kudridely-site-api"}


@app.post("/api/inquiries")
async def create_inquiry(payload: InquiryPayload):
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="API is not ready")

    inquiry = WebsiteInquiry(
        client_name=payload.client_name.strip()[:150],
        contact=payload.contact.strip()[:200],
        service_name=payload.service_name.strip()[:200],
        desired_date=payload.desired_date.strip()[:30],
        desired_time=payload.desired_time.strip()[:30],
        comment=payload.comment.strip()[:2000],
        status="new",
    )

    async with session_factory() as session:
        session.add(inquiry)
        await session.commit()
        await session.refresh(inquiry)
        inquiry_id = inquiry.id

    lines = [
        f"🆕 <b>Новая заявка с сайта «Кудриделы» №{inquiry_id}</b>",
        f"👤 Имя: {escape(payload.client_name.strip())}",
        f"📞 Контакт: {escape(payload.contact.strip())}",
    ]
    if payload.service_name.strip():
        lines.append(f"✂️ Услуга: {escape(payload.service_name.strip())}")
    if payload.desired_date.strip():
        lines.append(f"📅 Желаемая дата: {escape(payload.desired_date.strip())}")
    if payload.desired_time.strip():
        lines.append(f"🕒 Время: {escape(payload.desired_time.strip())}")
    if payload.comment.strip():
        lines.append(f"💬 Комментарий: {escape(payload.comment.strip())}")
    message = "\n".join(lines)

    async def notify_admin(admin_id: int) -> bool:
        try:
            # Bound Telegram I/O so a slow Telegram connection can never keep
            # the website's submit button spinning indefinitely.
            await asyncio.wait_for(
                app.state.bot.send_message(admin_id, message),
                timeout=8.0,
            )
            return True
        except asyncio.TimeoutError:
            logger.error("Telegram notification timed out for inquiry %s", inquiry_id)
            return False
        except Exception:
            logger.exception("Inquiry %s was saved but notification to admin %s failed", inquiry_id, admin_id)
            return False

    results = await asyncio.gather(
        *(notify_admin(admin_id) for admin_id in app.state.admin_ids),
        return_exceptions=True,
    )
    notified = bool(results) and all(result is True for result in results)

    # Return the saved inquiry even if Telegram is temporarily unavailable.
    return {"ok": True, "id": inquiry_id, "notified": notified}
