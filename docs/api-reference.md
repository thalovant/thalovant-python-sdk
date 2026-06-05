# API Reference

This is a concise reference for the public SDK surface. For generated API docs,
open the reference pages under **Generated Reference** in the navigation.

## Clients

### `ThalovantClient`

Synchronous client.

Constructors:

- `ThalovantClient(identity, **options)`
- `ThalovantClient.from_identity_file(path, **options)`
- `ThalovantClient.from_env(**options)`

Common methods:

- `connect()`
- `close()` / `disconnect()`
- `healthcheck() -> ThalovantHealth`
- `doctor() -> ThalovantDoctorReport`
- `ask(text, timeout=12.0, lang="en-us", context=None, session_id=None, request_id=None) -> ThalovantReply`
- `send_utterance(text, lang="en-us", context=None, session_id=None, request_id=None)`
- `conversation(session_id=None, lang="en-us", context=None) -> ThalovantConversation`
- `on(event_name, handler, context=None, session_id=None, request_id=None, predicate=None) -> ThalovantSubscription`
- `wait_for_event(event_name, timeout=12.0, predicate=None, context=None, session_id=None, request_id=None) -> ThalovantEvent`
- `listen(event_name, timeout=None, max_events=None, predicate=None, context=None, session_id=None, request_id=None)`
- `emit(event_type, data=None, context=None)`

### `AsyncThalovantClient`

Async wrapper for asyncio applications. It mirrors the synchronous client with
`async` methods where the operation can block.

## Conversations

### `ThalovantConversation`

Created with `client.conversation(...)`.

Methods:

- `ask(...)`
- `send_utterance(...)`
- `emit(...)`
- `on(...)`
- `wait_for_event(...)`
- `listen(...)`

The conversation owns a stable `session_id` and default `lang`.

### `AsyncThalovantConversation`

Created with `async_client.conversation(...)`. It mirrors the synchronous
conversation with async methods.

## Agents

### `ThalovantAgent`

Long-running synchronous runner.

Constructors:

- `ThalovantAgent(identity, **client_options)`
- `ThalovantAgent.from_identity_file(path, **client_options)`
- `ThalovantAgent.from_env(**client_options)`

Methods:

- `on(event_name, handler=None)`
- `on_speak(handler=None)`
- `run_forever(poll_interval=0.25)`
- `stop()`
- `close()`
- `ask(...)`
- `send_utterance(...)`
- `conversation(...)`

Use `agent.on(event_name)` as a decorator before `run_forever`.

### `AsyncThalovantAgent`

Asyncio runner with async `run_forever`, `ask`, `send_utterance`, and `close`.

## Data Models

### `ThalovantEvent`

Fields:

- `name`
- `data`
- `context`
- `raw`

Helpers:

- `text`
- `utterances`
- `session_id`
- `site_id`
- `request_id`
- `lang`
- `is_failure`
- `is_policy_denied`
- `matches_context(...)`
- `as_dict()`

### `ThalovantReply`

Fields and helpers:

- `text`
- `utterances`
- `handled`
- `ok`
- `session_id`
- `request_id`
- `events`
- `failure_event`
- `as_dict()`

### `ThalovantHealth`

Transport health snapshot:

- `connected`
- `handshake_complete`
- `transport_alive`
- `last_error`
- `ok`
- `as_dict()`

### `ThalovantDoctorReport`

Diagnostic report:

- `identity`
- `checks`
- `ok`
- `as_dict()`
- `format()`

## Event Constants

- `EVENT_RECOGNIZER_LOOP_UTTERANCE`
- `EVENT_SPEAK`
- `EVENT_UTTERANCE_HANDLED`
- `EVENT_INTENT_FAILURE`
- `EVENT_POLICY_DENIED`

## Exceptions

- `ThalovantError`
- `ThalovantIdentityError`
- `ThalovantConnectionError`
- `ThalovantTimeoutError`
- `ThalovantRuntimeError`
