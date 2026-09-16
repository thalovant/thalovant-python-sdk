# The hive: mesh frames and binary

A hub is a *hive*, not a star. Besides the conversation traffic `ask()` owns,
it relays frames that belong to the mesh, and it can send a client bytes rather
than text.

## Listening to the mesh

```python
stop = client.on_hive("broadcast", lambda frame: print(frame.payload))
...
stop()
```

| kind | what it is |
|---|---|
| `broadcast` | aimed down at every child of this hub |
| `propagate` | walked across the whole hive, once per node |
| `escalate` | sent up to the parent |
| `intercom` | addressed node to node |
| `rendezvous` | the mailbox peers use to find each other through NAT |

The frame arrives as the hub sent it — a `HiveMessage`, not a normalized
`ThalovantEvent` — so nothing is lost in a shape this SDK does not model yet.

`query` and `cascade` are deliberately not offered here: they are this client's
own request/response traffic and `ask()` already owns them. Asking for one
raises, rather than subscribing to something that quietly competes for the
same replies.

## Sending into the mesh

```python
client.propagate("thalovant.ping", {"n": 1})   # across the hive
client.escalate("thalovant.ping", {"n": 1})    # up to the parent
client.broadcast("thalovant.ping", {"n": 1})   # down to every child
```

!!! warning "A refusal arrives as a closed socket, not an exception"

    `broadcast` needs admin standing **and** the `can_broadcast` grant.
    `propagate` and `escalate` need their own grants, which are on by default
    but an operator can revoke. A hub does not answer a client that lacks them
    with an error — it **disconnects it for misbehaviour**.

    Nothing here can check first. A hub's HELLO carries its public key, its
    peer name and its node id, and says nothing about what this client may do.
    So the refusal shows up on the next read, not on the call.

## Binary frames

This is how a hub answers `speak:synth`: it renders the utterance and sends the
audio back, so a client with no synthesiser of its own can still speak. Files
arrive the same way.

```python
def played(frame):
    if frame.kind == "tts_audio":
        speaker.play(frame.data)          # frame.utterance, frame.lang

stop = client.on_binary(played)
```

A binary frame carries no request id, so it cannot be attributed to one
`ask()`; it is delivered by subscription instead, and `frame.utterance` is the
only thread back to a turn.

`frame.file_name` is remote text naming a remote file. It is a hint, never a
path to write to.
