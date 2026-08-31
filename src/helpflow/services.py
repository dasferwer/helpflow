from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from helpflow.config import get_settings
from helpflow.models import (
    Comment,
    OutboxEvent,
    Ticket,
    TicketHistory,
    TicketStatus,
    User,
    UserRole,
)
from helpflow.schemas import CommentCreate, TicketCreate

STAFF_ROLES = {UserRole.OPERATOR, UserRole.ADMIN}
STAFF_TRANSITIONS = {
    TicketStatus.NEW: {TicketStatus.IN_PROGRESS},
    TicketStatus.IN_PROGRESS: {TicketStatus.WAITING_CUSTOMER, TicketStatus.RESOLVED},
    TicketStatus.WAITING_CUSTOMER: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED},
    TicketStatus.RESOLVED: {TicketStatus.IN_PROGRESS, TicketStatus.CLOSED},
    TicketStatus.CLOSED: set(),
}
CLIENT_TRANSITIONS = {
    TicketStatus.RESOLVED: {TicketStatus.IN_PROGRESS, TicketStatus.CLOSED},
}


def _event_payload(ticket: Ticket) -> dict[str, object]:
    settings = get_settings()
    return {
        "ticket_id": str(ticket.id),
        "ticket_number": ticket.number,
        "subject": ticket.subject,
        "status": str(ticket.status),
        "client_chat_id": ticket.client.telegram_chat_id,
        "operator_chat_id": settings.operator_telegram_chat_id or None,
    }


def add_outbox_event(db: Session, event_type: str, ticket: Ticket) -> None:
    db.add(OutboxEvent(event_type=event_type, payload=_event_payload(ticket)))


def add_history(
    db: Session,
    *,
    ticket: Ticket,
    actor: User | None,
    action: str,
    from_status: TicketStatus | None = None,
    to_status: TicketStatus | None = None,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        TicketHistory(
            ticket_id=ticket.id,
            actor_id=actor.id if actor else None,
            action=action,
            from_status=str(from_status) if from_status else None,
            to_status=str(to_status) if to_status else None,
            details=details or {},
        )
    )


def get_ticket(db: Session, ticket_id: UUID, *, with_details: bool = False) -> Ticket:
    query = select(Ticket).where(Ticket.id == ticket_id)
    if with_details:
        query = query.options(selectinload(Ticket.comments), selectinload(Ticket.history))
    ticket = db.scalar(query)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def ensure_ticket_access(ticket: Ticket, user: User) -> None:
    if user.role not in STAFF_ROLES and ticket.client_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")


def create_ticket(db: Session, payload: TicketCreate, client: User) -> Ticket:
    ticket = Ticket(
        subject=payload.subject.strip(),
        description=payload.description.strip(),
        priority=payload.priority,
        status=TicketStatus.NEW,
        client_id=client.id,
    )
    db.add(ticket)
    db.flush()
    ticket.client = client
    add_history(db, ticket=ticket, actor=client, action="ticket.created")
    add_outbox_event(db, "ticket.created", ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def list_tickets(
    db: Session,
    *,
    user: User,
    ticket_status: TicketStatus | None,
    assignee_id: UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[Ticket], int]:
    filters = []
    if user.role not in STAFF_ROLES:
        filters.append(Ticket.client_id == user.id)
    if ticket_status is not None:
        filters.append(Ticket.status == ticket_status)
    if assignee_id is not None and user.role in STAFF_ROLES:
        filters.append(Ticket.assignee_id == assignee_id)
    query: Select[tuple[Ticket]] = select(Ticket).where(*filters)
    count_query = select(func.count()).select_from(Ticket).where(*filters)
    items = list(
        db.scalars(query.order_by(Ticket.created_at.desc()).limit(limit).offset(offset)).all()
    )
    return items, db.scalar(count_query) or 0


def assign_ticket(db: Session, ticket: Ticket, assignee: User, actor: User) -> Ticket:
    if assignee.role not in STAFF_ROLES or not assignee.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Assignee must be an active operator or administrator",
        )
    ticket.assignee_id = assignee.id
    previous = TicketStatus(ticket.status)
    if ticket.status == TicketStatus.NEW:
        ticket.status = TicketStatus.IN_PROGRESS
    add_history(
        db,
        ticket=ticket,
        actor=actor,
        action="ticket.assigned",
        from_status=previous,
        to_status=TicketStatus(ticket.status),
        details={"assignee_id": str(assignee.id)},
    )
    add_outbox_event(db, "ticket.assigned", ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def transition_ticket(
    db: Session,
    ticket: Ticket,
    target: TicketStatus,
    actor: User,
    reason: str | None,
) -> Ticket:
    ensure_ticket_access(ticket, actor)
    current = TicketStatus(ticket.status)
    transitions = STAFF_TRANSITIONS if actor.role in STAFF_ROLES else CLIENT_TRANSITIONS
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transition from {current} to {target} is not allowed for this role",
        )
    ticket.status = target
    ticket.resolved_at = datetime.now(UTC) if target == TicketStatus.RESOLVED else None
    add_history(
        db,
        ticket=ticket,
        actor=actor,
        action="ticket.status_changed",
        from_status=current,
        to_status=target,
        details={"reason": reason} if reason else {},
    )
    add_outbox_event(db, "ticket.status_changed", ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def add_comment(db: Session, ticket: Ticket, payload: CommentCreate, author: User) -> Comment:
    ensure_ticket_access(ticket, author)
    if payload.is_internal and author.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only staff can create internal comments",
        )
    comment = Comment(
        ticket_id=ticket.id,
        author_id=author.id,
        body=payload.body.strip(),
        is_internal=payload.is_internal,
    )
    db.add(comment)
    db.flush()
    add_history(
        db,
        ticket=ticket,
        actor=author,
        action="ticket.comment_added",
        details={"comment_id": str(comment.id), "internal": payload.is_internal},
    )
    add_outbox_event(db, "ticket.comment_added", ticket)
    db.commit()
    db.refresh(comment)
    return comment


def link_telegram_chat(db: Session, user: User, chat_id: str) -> User:
    user.telegram_chat_id = chat_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram chat is already linked to another user",
        ) from exc
    db.refresh(user)
    return user
