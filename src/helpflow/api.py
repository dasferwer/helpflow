import hmac
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from helpflow.broker import check_connection
from helpflow.config import get_settings
from helpflow.dependencies import CurrentUser, DbSession, StaffUser
from helpflow.models import (
    Comment,
    DeliveryReplay,
    DeliveryStatus,
    NotificationDelivery,
    Ticket,
    TicketStatus,
    User,
    UserRole,
)
from helpflow.schemas import (
    AssignTicketRequest,
    CommentCreate,
    CommentRead,
    LoginRequest,
    TelegramLinkResponse,
    TelegramUpdate,
    TelegramWebhookResponse,
    TicketCreate,
    TicketDetail,
    TicketList,
    TicketRead,
    TokenResponse,
    TransitionRequest,
    UserCreate,
    UserRead,
)
from helpflow.security import create_access_token, hash_password, verify_password
from helpflow.services import (
    add_comment,
    assign_ticket,
    create_ticket,
    ensure_ticket_access,
    get_ticket,
    list_tickets,
    transition_ticket,
)
from helpflow.telegram import handle_update, issue_link

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    rabbitmq: Literal["ok"]


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(db: DbSession) -> HealthResponse:
    db.execute(text("SELECT 1"))
    check_connection()
    return HealthResponse(status="ok", database="ok", rabbitmq="ok")


@router.post(
    "/api/v1/auth/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
def register(payload: UserCreate, db: DbSession) -> User:
    email = payload.email.lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already used")
    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=UserRole.CLIENT,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already used"
        ) from exc
    db.refresh(user)
    return user


@router.post("/api/v1/auth/login", response_model=TokenResponse, tags=["auth"])
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    return TokenResponse(access_token=create_access_token(user.id, str(user.role)))


@router.get("/api/v1/users/me", response_model=UserRead, tags=["users"])
def read_me(current_user: CurrentUser) -> User:
    return current_user


@router.post(
    "/api/v1/users/me/telegram/link-token", response_model=TelegramLinkResponse, tags=["users"]
)
def link_telegram(db: DbSession, current_user: CurrentUser) -> TelegramLinkResponse:
    return issue_link(db, current_user)


@router.get("/api/v1/users/staff", response_model=list[UserRead], tags=["users"])
def list_staff(db: DbSession, staff: StaffUser) -> list[User]:
    del staff
    return list(
        db.scalars(
            select(User)
            .where(User.role.in_([UserRole.OPERATOR, UserRole.ADMIN]), User.is_active.is_(True))
            .order_by(User.full_name)
        ).all()
    )


@router.post(
    "/api/v1/tickets",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tickets"],
)
def create_ticket_route(
    payload: TicketCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> Ticket:
    return create_ticket(db, payload, current_user)


@router.get("/api/v1/tickets", response_model=TicketList, tags=["tickets"])
def read_tickets(
    db: DbSession,
    current_user: CurrentUser,
    ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
    assignee_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketList:
    items, total = list_tickets(
        db,
        user=current_user,
        ticket_status=ticket_status,
        assignee_id=assignee_id,
        limit=limit,
        offset=offset,
    )
    return TicketList(items=items, total=total, limit=limit, offset=offset)


@router.get("/api/v1/tickets/{ticket_id}", response_model=TicketDetail, tags=["tickets"])
def read_ticket(ticket_id: UUID, db: DbSession, current_user: CurrentUser) -> TicketDetail:
    ticket = get_ticket(db, ticket_id, with_details=True)
    ensure_ticket_access(ticket, current_user)
    result = TicketDetail.model_validate(ticket)
    if current_user.role == UserRole.CLIENT:
        result.comments = [comment for comment in result.comments if not comment.is_internal]
        result.history = [event for event in result.history if not event.details.get("internal")]
    return result


@router.post(
    "/api/v1/tickets/{ticket_id}/assign",
    response_model=TicketRead,
    tags=["tickets"],
)
def assign(
    ticket_id: UUID,
    payload: AssignTicketRequest,
    db: DbSession,
    staff: StaffUser,
) -> Ticket:
    ticket = get_ticket(db, ticket_id)
    assignee = db.get(User, payload.assignee_id)
    if assignee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignee not found")
    return assign_ticket(db, ticket, assignee, staff, payload.version)


@router.post(
    "/api/v1/tickets/{ticket_id}/transition",
    response_model=TicketRead,
    tags=["tickets"],
)
def transition(
    ticket_id: UUID,
    payload: TransitionRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> Ticket:
    return transition_ticket(
        db,
        get_ticket(db, ticket_id),
        payload.status,
        current_user,
        payload.reason,
        payload.version,
    )


@router.post(
    "/api/v1/tickets/{ticket_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tickets"],
)
def comment(
    ticket_id: UUID,
    payload: CommentCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> Comment:
    return add_comment(db, get_ticket(db, ticket_id), payload, current_user)


@router.post(
    "/api/v1/integrations/telegram/webhook",
    response_model=TelegramWebhookResponse,
    tags=["integrations"],
)
def telegram_webhook(
    payload: TelegramUpdate,
    db: DbSession,
    secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> TelegramWebhookResponse:
    expected = get_settings().telegram_webhook_secret.get_secret_value()
    if secret is None or not hmac.compare_digest(secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret"
        )
    return handle_update(db, payload)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


@router.get("/api/v1/notifications/dead", tags=["notifications"])
def dead_notifications(
    db: DbSession, staff: StaffUser, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[dict[str, object]]:
    if staff.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    jobs = db.scalars(
        select(NotificationDelivery)
        .where(NotificationDelivery.status.in_(["dead", "failed"]))
        .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
        .limit(limit)
    )
    return [
        {
            "id": job.id,
            "event_id": job.event_id,
            "status": job.status,
            "attempts": job.attempts,
            "error": job.error,
        }
        for job in jobs
    ]


@router.post("/api/v1/notifications/{delivery_id}/retry", tags=["notifications"])
def retry_notification(
    delivery_id: UUID, payload: ReplayRequest, db: DbSession, staff: StaffUser
) -> dict[str, str]:
    if staff.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    job = db.scalar(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery_id).with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Доставка не найдена")
    if job.status != DeliveryStatus.DEAD or not job.payload:
        raise HTTPException(
            status_code=409, detail="Повтор разрешён только для новых доставок в состоянии dead"
        )
    job.status, job.attempts, job.error = DeliveryStatus.RETRY, 0, None
    job.next_attempt_at = datetime.now(UTC)
    db.add(DeliveryReplay(delivery_id=job.id, actor_id=staff.id, reason=payload.reason.strip()))
    db.commit()
    return {"status": "retry"}
