from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from helpflow.models import TicketPriority, TicketStatus, UserRole


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    telegram_chat_id: str | None
    is_active: bool
    created_at: datetime


class TelegramLinkRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64)


class TicketCreate(BaseModel):
    subject: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10, max_length=5000)
    priority: TicketPriority = TicketPriority.NORMAL


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: int
    subject: str
    description: str
    priority: TicketPriority
    status: TicketStatus
    client_id: UUID
    assignee_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    is_internal: bool = False


class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    author_id: UUID
    body: str
    is_internal: bool
    created_at: datetime


class HistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: UUID | None
    action: str
    from_status: str | None
    to_status: str | None
    details: dict[str, object]
    created_at: datetime


class TicketDetail(TicketRead):
    comments: list[CommentRead]
    history: list[HistoryRead]


class TicketList(BaseModel):
    items: list[TicketRead]
    total: int
    limit: int
    offset: int


class AssignTicketRequest(BaseModel):
    assignee_id: UUID


class TransitionRequest(BaseModel):
    status: TicketStatus
    reason: str | None = Field(default=None, max_length=500)


class TelegramChat(BaseModel):
    id: int | str


class TelegramMessage(BaseModel):
    text: str | None = None
    chat: TelegramChat


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None


class TelegramWebhookResponse(BaseModel):
    accepted: bool
    ticket_number: int | None = None
    reason: str | None = None
