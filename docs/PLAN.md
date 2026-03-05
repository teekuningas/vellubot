# Agentic IRC Bot — Design Reference

## Goal

Transform the IRC bot from a reactive `!chat` command responder into an autonomous, shy, human-like channel presence. The bot observes chat, decides on its own when to speak (or stay silent), and maintains persistent memories about users and topics across restarts.

---

## Architecture

```
src/
  main.py     IRC glue: events, commands, reactor scheduling, outbox queue
  agent.py    All agent logic: urge, history, memory, LLM calls
  parser.py   Feed parsing (RSS, tori.fi) — standalone, no bot/agent knowledge
```

`parser.py` has zero knowledge of the agent. `agent.py` has zero knowledge of IRC. `main.py` wires them together using the IRC library's reactor for scheduling and a `queue.Queue` outbox for thread-safe message delivery.

### Threading model

All IRC socket operations (sending messages) happen on the **main thread** only. Blocking work (HTTP feed checks, LLM API calls) is dispatched to a `ThreadPoolExecutor`. Background workers communicate results back via an outbox queue, which the main thread drains periodically.

```
Main thread (IRC reactor event loop)
  ├── IRC events (on_pubmsg, on_welcome, etc.)
  ├── reactor.scheduler.execute_after → _tick_feeds()   [submits work to executor]
  ├── reactor.scheduler.execute_after → _tick_agent()   [submits work to executor]
  └── reactor.scheduler.execute_every → _drain_outbox() [sends queued messages via privmsg]

ThreadPoolExecutor (max_workers=2)
  ├── _feed_worker()   → puts results in outbox queue
  └── _agent_worker()  → puts results in outbox queue

queue.Queue (outbox)
  └── background threads put messages here → main thread sends them
```

Key properties:
- **No daemon threads** — reactor callbacks self-reschedule via `execute_after` in a `finally` block, so they always recover from errors and respect runtime `check_interval` changes.
- **Thread-safe IRC sends** — all `privmsg` calls happen from the main thread. No locks needed on the socket.
- **Bounded concurrency** — `ThreadPoolExecutor(max_workers=2)` manages worker threads. No unbounded `Thread()` spawning.
- **Supervision** — if a worker raises an exception, it's logged and the next scheduled tick retries. No silent failures.

---

## 1. The Urge Accumulator

The core mechanism that decides *when* the bot "checks IRC."

### Design

A single `_urge` float accumulates continuously. Both time and messages add to it incrementally.

```
# on each incoming message (from others):
_urge += dt_hours / URGE_TIME_DIVISOR   # time since last message/tick
_urge += 1.0 / URGE_MSG_DIVISOR         # the message itself

# on background tick:
_urge += dt_hours / URGE_TIME_DIVISOR   # time since last message/tick

# if bot's name appears anywhere in message:
_urge += URGE_MENTION_BOOST             # mention boost
```

### Trigger

Evaluated on every incoming message (`on_pubmsg`) and every background tick (every `check_interval` seconds). When `_urge >= threshold`, the bot calls the LLM — **unless a call is already in progress**, in which case urge keeps accumulating until the running call finishes.

### Gaussian threshold

Each cycle draws a new threshold from `gauss(URGE_THRESHOLD_MU, URGE_THRESHOLD_SIGMA)` floored at 0.5. This makes the bot's rhythm slightly unpredictable.

### When urge goes UP / DOWN

| Event | Effect |
|---|---|
| Someone sends a message | `+1/URGE_MSG_DIVISOR` |
| Bot's name mentioned | `+URGE_MENTION_BOOST` on top |
| Time passes silently | grows continuously |
| Urge crosses threshold → LLM called | `urge = max(0, urge − URGE_TRIGGER_COST)`, new threshold drawn |
| `!chat_enabled` toggled off | full reset to 0 |
| Bot's own messages | no effect (prevents self-hype loops) |

Urge is **not** zeroed on trigger — it is reduced by `URGE_TRIGGER_COST`. This allows sustaining short back-and-forth conversation.

---

## 2. The Agent Loop

When urge fires, `AgentState.run()` is submitted to the thread pool executor (guarded against concurrent runs with a `_running` flag). The response, if any, is placed in the outbox queue for the main thread to send.

### LLM call structure

Two messages sent to the API:

1. **System prompt** (static Finnish text) — personality, memory instructions, JSON schema
2. **User message** (dynamic) — current time, channel users, memory slots, last 30 chat messages

### Expected JSON response

```json
{
  "internal_monologue": "chain-of-thought reasoning before deciding",
  "should_speak": true,
  "message_to_send": "moi, mitä kuuluu",
  "memory_updates": [
    {"slot": 2, "content": "sipsu tykkää mekaanisista näppäimistöistä"},
    {"slot": 5, "content": null}
  ]
}
```

- `internal_monologue` is the bot's chain-of-thought — filled first.
- `should_speak` — the bot can decide to stay silent.
- `message_to_send` — single message. Code strips own name prefix and takes only the first line.
- `memory_updates` — sparse slot updates.

### API details

- `response_format={"type": "json_object"}` — works on both OpenAI and Azure OpenAI
- `max_tokens=MAX_TOKENS_OUT` (default 1024) — enough for monologue + message + memory updates
- `timeout=30` seconds
- On any failure: logged, bot stays silent, `_running` flag cleared.

---

## 3. Memory: The Slot-Based Notepad

10 numbered slots (0–9), each holding a short text or `null`. Persisted to a JSON file (`AGENT_MEMORY_FILE` env var).

The LLM returns only the specific changes it wants to make — if `memory_updates` is absent or malformed, all slots remain untouched. Each update is individually validated.

```json
["sipsu tykkää mekaanisista näppäimistöistä", null, "Zairex katsoo näytönohjaintarjouksia", null, ...]
```

Always exactly 10 entries. Loaded on startup, saved after each update.

---

## 4. Chat History

- In-memory only (lost on restart — intentional)
- Rolling buffer of 1000 entries: `(timestamp: float, username: str, message: str)`
- LLM receives the most recent 30 messages with ISO timestamps
- Feed announcements recorded in history so the agent is aware of them
- Bot's own spoken messages also recorded in history

---

## 5. System Prompt

Static Finnish text establishing:

- **Personality**: calm and thoughtful regular channel presence
- **Direct address rule**: if someone mentions the bot's name, respond almost always
- **Language**: Finnish by default, English if addressed in English
- **Brevity**: one or two sentences usually enough
- **Memory discipline**: preserve existing, update only when genuinely needed
- **Self-control**: never prefix own name, never continue others' lines

---

## 6. IRC Commands

### Feed/filter management
`!filters`, `!nofilters`, `!filter <regexp>`, `!delfilter <idx>`, `!feeds`, `!nofeeds`, `!feed <url>`, `!delfeed <idx>`, `!check_interval [<int>]`, `!check_length [<int>]`, `!commands`

### Chat control
`!chat_enabled` — toggles autonomous chat on/off. Resets urge on disable.

### Direct address
Mentioning the bot's name anywhere in a message adds `URGE_MENTION_BOOST` to urge, usually triggering an immediate LLM call.

---

## 7. Environment Variables

### Connection & Storage

| Variable | Default | Purpose |
|---|---|---|
| `BOT_CHANNEL` | `#vellumotest` | IRC channel |
| `BOT_NICKNAME` | `vellubot` | Bot nickname |
| `BOT_SERVER` | `irc.libera.chat` | IRC server |
| `BOT_PORT` | `6667` | IRC port |
| `BOT_SASL_PASSWORD` | None | SASL auth password |
| `SETTINGS_FNAME` | None | Settings JSON file path |
| `AGENT_MEMORY_FILE` | None | Memory slots JSON file path |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### LLM Provider

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | None | OpenAI API key |
| `OPENAI_ORGANIZATION_ID` | None | OpenAI organization ID |
| `OPENAI_BASE_URL` | None | Custom OpenAI-compatible endpoint |
| `AZURE_OPENAI_KEY` | None | Azure OpenAI key |
| `AZURE_ENDPOINT` | None | Azure endpoint URL |
| `AZURE_API_VERSION` | None | Azure API version |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM model name |
| `OPENAI_MAX_TOKENS_OUT` | `1024` | Max output tokens |

### Urge Tuning

| Variable | Default | Effect |
|---|---|---|
| `URGE_TIME_DIVISOR` | `50.0` | Hours of silence to accumulate 1.0 urge. Lower = speaks sooner. |
| `URGE_MSG_DIVISOR` | `20.0` | Messages needed for 1.0 urge. Lower = speaks after fewer messages. |
| `URGE_MENTION_BOOST` | `2.0` | Urge added when bot's name is mentioned. |
| `URGE_TRIGGER_COST` | `1.0` | Urge subtracted per LLM call. Lower = sustains conversation longer. |
| `URGE_THRESHOLD_MU` | `1.0` | Mean of gaussian trigger threshold. Higher = needs more urge to trigger. |
| `URGE_THRESHOLD_SIGMA` | `0.2` | Stddev of gaussian threshold. Higher = more unpredictable timing. |

**Testing tip**: `URGE_TIME_DIVISOR=0.01 URGE_MSG_DIVISOR=3.0` triggers in seconds/messages rather than hours.

---

## 8. Robustness

- **Feed checking fails** → logged in `_feed_worker`, error message to outbox, `_feed_busy` cleared, next tick retries.
- **LLM call fails** → logged in `AgentState.run()`, `_running` cleared, bot stays silent, next urge cycle retries.
- **Malformed JSON from LLM** → memories untouched, no message sent.
- **Concurrent triggers** → `_running` flag prevents double LLM calls.
- **Bot name in response** → stripped code-side. Multi-line response → only first line taken.
- **Settings file missing keys** → `Settings.get()` uses defaults.
- **HTTP timeouts** → `requests.get(timeout=30)` in `parser.py`.
