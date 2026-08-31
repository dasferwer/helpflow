# HelpFlow architecture decisions

## Transactional outbox

Publishing to RabbitMQ directly inside an HTTP request creates a dual-write
problem: the database transaction may commit while message publication fails,
or the message may be published before the transaction rolls back.

HelpFlow writes the domain change and `outbox_events` row in one PostgreSQL
transaction. A separate publisher locks pending rows with `FOR UPDATE SKIP
LOCKED`, publishes persistent RabbitMQ messages and sets `published_at` only
after publisher confirmation.

## Consumer idempotency

RabbitMQ uses at-least-once delivery. The consumer stores the event UUID in
`notification_deliveries`, where a unique constraint prevents duplicate side
effects after redelivery. Failed Telegram requests are recorded instead of
silently acknowledged.

## Telegram boundary

Incoming webhook calls require `X-Telegram-Bot-Api-Secret-Token`. A chat must be
linked to an active HelpFlow account before it can create a ticket. The demo
command is:

```text
/new Subject | Detailed description
```

The bot token is optional locally. No real message is sent without an explicit
token in environment variables.

## Failure scenarios

| Failure | Behaviour |
|---|---|
| PostgreSQL unavailable | API healthcheck fails and no request is accepted |
| RabbitMQ unavailable | API healthcheck reports failure; committed outbox rows remain pending |
| Publisher crashes | Unpublished rows are retried after restart |
| Consumer receives an event twice | Unique event ID prevents duplicate delivery records |
| Telegram token missing | Delivery is recorded as `skipped` |
| Telegram request fails | Delivery is recorded as `failed` with a bounded error message |
| Invalid ticket transition | API returns `409 Conflict` |
| Client requests another user's ticket | API returns `404` to avoid leaking identifiers |
