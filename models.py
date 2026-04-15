"""
AgentEvents v1.0 — Core Models

SQLAlchemy models + Pydantic schemas for the event bus.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4
from enum import Enum as PyEnum

from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import (
    Column, String, Boolean, DateTime, Integer, Text,
    Index, UniqueConstraint, Enum
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB, TIMESTAMPTZ
from sqlalchemy.orm import DeclarativeBase


# ─── Enums ────────────────────────────────────────────────────────────────────

class DeliveryMethod(str, PyEnum):
    WEBHOOK = "webhook"
    AGENTMAIL = "agentmail"
    POLL = "poll"


class EventStatus(str, PyEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class ScopeType(str, PyEnum):
    GLOBAL = "global"
    GROUP = "group"
    ROOM = "room"
    THREAD = "thread"


# ─── SQLAlchemy Base ────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─── SQLAlchemy Models ─────────────────────────────────────────────────────────

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    civ_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    scope_type = Column(String(20), default=ScopeType.GLOBAL.value)
    scope_id = Column(PG_UUID(as_uuid=True), nullable=True)
    delivery_method = Column(String(20), default=DeliveryMethod.WEBHOOK.value)
    webhook_url = Column(Text, nullable=True)
    agentmail_address = Column(Text, nullable=True)
    muted_until = Column(TIMESTAMPTZ, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMPTZ, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("civ_id", "event_type", "scope_type", "scope_id",
                         name="uq_subscription_unique"),
        Index("ix_subscriptions_event_type", "event_type"),
        Index("ix_subscriptions_active", "active"),
        Index("ix_subscriptions_muted_until", "muted_until"),
    )


class Event(Base):
    __tablename__ = "events"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type = Column(String(100), nullable=False, index=True)
    source = Column(String(255), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    created_at = Column(TIMESTAMPTZ, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_events_created_at", "created_at"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id = Column(PG_UUID(as_uuid=True), nullable=False)
    subscription_id = Column(PG_UUID(as_uuid=True), nullable=False)
    status = Column(String(20), default=EventStatus.PENDING.value, index=True)
    attempts = Column(Integer, default=0)
    last_attempt_at = Column(TIMESTAMPTZ, nullable=True)
    delivered_at = Column(TIMESTAMPTZ, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_deliveries_status_pending", "status",
              postgresql_where=status == EventStatus.PENDING.value),
        Index("ix_deliveries_event_sub", "event_id", "subscription_id"),
    )


# ─── Pydantic Schemas ──────────────────────────────────────────────────────────

class SubscriptionCreate(BaseModel):
    event_type: str = Field(..., description="Event type to subscribe to")
    scope_type: ScopeType = Field(default=ScopeType.GLOBAL)
    scope_id: Optional[UUID] = None
    delivery_method: DeliveryMethod = Field(default=DeliveryMethod.WEBHOOK)
    webhook_url: Optional[str] = None
    agentmail_address: Optional[str] = None


class SubscriptionResponse(BaseModel):
    id: UUID
    civ_id: str
    event_type: str
    scope_type: ScopeType
    scope_id: Optional[UUID]
    delivery_method: DeliveryMethod
    webhook_url: Optional[str]
    agentmail_address: Optional[str]
    muted_until: Optional[datetime]
    active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SubscriptionList(BaseModel):
    subscriptions: list[SubscriptionResponse]
    total: int


class EventCreate(BaseModel):
    event_type: str = Field(..., alias="type")
    source: str
    payload: dict = Field(default_factory=dict)


class EventResponse(BaseModel):
    id: UUID
    event_type: str = Field(..., alias="type")
    source: str
    payload: dict
    created_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class EventList(BaseModel):
    events: list[EventResponse]
    total: int


class PendingEventsResponse(BaseModel):
    events: list[dict]
    total: int


class AckRequest(BaseModel):
    event_ids: list[UUID]


class MuteRequest(BaseModel):
    duration_minutes: int = Field(gt=0)


class ErrorResponse(BaseModel):
    error: str
    message: str


# ─── Webhook Payload ──────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    event_type: str
    event_id: UUID
    source_civ: str
    scope: dict
    payload: dict
    timestamp: datetime
    resource_url: str
