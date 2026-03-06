# Agentic IRC Bot — Design Reference

## Goal

Transform the IRC bot from a reactive `!chat` command responder into an autonomous, shy, human-like channel presence. The bot observes chat, decides on its own when to speak (or stay silent), and maintains persistent memories about users and topics across restarts.

---

## Architecture

```
src/
  main.py     IRC glue: events, commands, reactor scheduling, outbox queue
  agent.py    All agent logic: urge, history, memory, LLM message building
  parser.py   Feed parsing (RSS, tori.fi) — standalone, no bot/agent knowledge
```

`parser.py` has zero knowledge of the agent. `agent.py` has zero knowledge of IRC. `main.py` wires them together using the IRC library's reactor for scheduling and a `queue.Queue` outbox for thread-safe message delivery.

### Threading model — Pure workers

The main thread (IRC reactor) is the **sole owner of all mutable state**. Thread pool workers are pure: they receive copies of inputs and return results. They never read or write shared state.

```
Main thread (IRC reactor event loop)
  ├── Owns all state: history, memories, seen, urge, settings
  ├── IRC events (on_pubmsg, on_welcome, etc.)
  ├── Prepares inputs, dispatches work to thread pool
  ├── _drain_outbox() — processes structured results from workers
  │     ├── AgentResult → apply memory updates, record bot message, save, send IRC
  │     ├── FeedResult  → update seen, add feed messages to history, save, send IRC
  │     └── str         → send as IRC message (errors, etc.)
  ├── _tick_feeds()  — submits feed worker with copied inputs
  ├── _tick_agent()  — checks urge, submits agent worker with prepared LLM messages
  └── _tick_save()   — periodic unconditional save of all state

ThreadPoolExecutor (max_workers=2)
  ├── _feed_worker(feeds, filters, check_length, seen)
  │     Pure: HTTP fetch + parse → puts FeedResult in outbox
  └── _agent_worker(client, model, messages)
        Pure: LLM API call + JSON parse → puts AgentResult in outbox
```

Key properties:
- **No shared mutable state** — workers receive copies, return results via queue.
- **No locks** — `AgentState` has no `threading.Lock`. All reads/writes happen on main thread.
- **No dirty flags** — persistence is unconditional and periodic (every 30s). Or event-driven: save after processing a result that changes state.
- **Easy to reason about** — at any point in the code, you can ask "who owns this data?" and the answer is always "the main thread."

### What goes in the thread pool (and why)

Only blocking I/O that would freeze the IRC reactor:
1. **LLM API calls** — can take 5–30 seconds
2. **Feed HTTP fetches** — can take 1–30 seconds

Everything else runs on the main thread: state mutation, urge logic, persistence, IRC sends.

---

## 1. The Urge Accumulator

The core mechanism that decides *when* the bot speaks.

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

When urge fires, the main thread prepares LLM messages and submits a pure worker to the thread pool. The worker makes the API call and puts the parsed result in the outbox. The main thread then applies the result.

### Flow

```
Main thread:
  1. agent.build_llm_messages(channel_users) → messages
  2. _agent_running = True
  3. executor.submit(_agent_worker, client, model, messages)

Worker thread (pure):
  4. call LLM API with messages
  5. parse JSON response
  6. outbox.put(AgentResult(parsed_result))

Main thread (_drain_outbox):
  7. agent.apply_llm_result(result) → message_to_send
  8. _agent_running = False
  9. if message_to_send → send to IRC
  10. save memories + history
```

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

- `internal_monologue` — chain-of-thought, filled first.
- `should_speak` — the bot can decide to stay silent.
- `message_to_send` — single message. Code strips own name prefix and takes only the first line.
- `memory_updates` — sparse slot updates.

### API details

- `response_format={"type": "json_object"}` — works on both OpenAI and Azure OpenAI
- `max_tokens=MAX_TOKENS_OUT` (default 1024) — enough for monologue + message + memory updates
- `timeout=30` seconds
- On any failure: logged, bot stays silent, `_agent_running` cleared.

---

## 3. Memory: The Slot-Based Notepad

10 numbered slots (0–9), each holding a short text or `null`. Persisted to a JSON file (`AGENT_MEMORY_FNAME` env var).

The LLM returns only the specific changes it wants to make — if `memory_updates` is absent or malformed, all slots remain untouched. Each update is individually validated.

```json
["sipsu tykkää mekaanisista näppäimistöistä", null, "Zairex katsoo näytönohjaintarjouksia", null, ...]
```

Always exactly 10 entries. Loaded on startup, saved periodically and after agent results.

---

## 4. Persistence

Four files, all optional (enabled by setting the corresponding env var):

| File | Env var | Contents | When modified |
|---|---|---|---|
| Settings | `SETTINGS_FNAME` | feeds, filters, intervals, chat_enabled | On `!command` (main thread, immediate save) |
| Agent memories | `AGENT_MEMORY_FNAME` | 10 memory slots (JSON array) | After agent LLM result is applied |
| Chat history | `AGENT_HISTORY_FNAME` | Up to 1000 `[timestamp, user, msg]` entries | On every message (pubmsg, feed, bot reply) |
| Parser seen | `PARSER_SEEN_FNAME` | List of feed item UIDs | After feed results with new items |

### Save strategy

- **Settings**: immediate save on every `set()` call. Already on main thread. No change needed.
- **Everything else**: unconditional periodic save every 30s from main thread. No dirty flags, no diffing.

This is intentionally simple. Writing 3 small JSON files every 30s is negligible I/O. Losing up to 30s of state on crash is acceptable.

### Chat history detail

The history buffer holds up to 1000 messages (`HISTORY_CAP`). The LLM only sees the most recent 30 (`CONTEXT_MESSAGES`). Persisting the full buffer means the bot has rich context after restarts.

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
| `AGENT_MEMORY_FNAME` | None | Memory slots JSON file path |
| `AGENT_HISTORY_FNAME` | None | Chat history JSON file path |
| `PARSER_SEEN_FNAME` | None | Parser seen-list JSON file path |
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

- **Feed checking fails** → worker puts error message in outbox, main thread clears `_feed_busy`, next tick retries.
- **LLM call fails** → worker puts error result in outbox, main thread clears `_agent_running`, bot stays silent.
- **Malformed JSON from LLM** → memories untouched, no message sent.
- **Concurrent triggers** → `_agent_running` flag prevents double LLM calls (checked on main thread, no lock needed).
- **Bot name in response** → stripped code-side. Multi-line response → only first line taken.
- **Settings file missing keys** → `Settings.get()` uses defaults.
- **HTTP timeouts** → `requests.get(timeout=30)` in `parser.py`.
- **Crash during save** → at most 30s of state lost. Acceptable.

---

## Refactoring Plan: Pure Workers

### Current state (what's wrong)

Thread pool workers directly mutate shared state:

1. `_agent_worker` → calls `self.agent.run()` which reads/writes `history`, `memories`, `_running` under a `threading.Lock`
2. `_feed_worker` → directly assigns `self.seen`, calls `self.agent.add_message()` which writes `history` under the lock
3. `AgentState` has a `threading.Lock`, dirty flags, and `save_if_dirty()` — all complexity caused by shared mutable state

### Target state

Workers are pure functions. Main thread owns all state. No locks, no dirty flags.

### Step-by-step

#### Step 1: Extract pure LLM call function

Create a module-level function in `agent.py`:

```python
def call_llm(client, model, messages):
    """Pure function: makes LLM call, returns parsed result dict or None."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=MAX_TOKENS_OUT,
        timeout=30,
    )
    raw = response.choices[0].message.content or "{}"
    return json.loads(raw)
```

This is the slow part that justifies a thread. Everything else stays on main thread.

#### Step 2: Split `AgentState.run()` into prepare + apply

Replace the monolithic `run()` with two main-thread methods:

```python
def build_llm_messages(self, channel_users) -> list:
    """Prepare the messages list for the LLM. Called from main thread."""
    # Same logic as current _build_messages, but no lock needed

def apply_llm_result(self, result) -> Optional[str]:
    """Apply LLM result to state. Returns message to send. Called from main thread."""
    # Apply memory updates to self.memories
    # Clean up message (strip name prefix, first line)
    # Record bot's own message in self.history
    # Return the message (or None)
```

Delete the `run()` method entirely.

#### Step 3: Remove `threading.Lock` and dirty flags from `AgentState`

Delete from `__init__`:
- `self._lock = threading.Lock()`
- `self._memories_dirty`
- `self._history_dirty`
- `self._running` (moves to `MyBot`)

Remove `import threading`.

Delete `save_if_dirty()`. Make `save_memories()` and `save_history()` simple direct-write methods (no lock, no dirty check).

Remove lock acquisitions from `add_message()`, `tick()`, `reset_urge()`, `_build_messages()`, `_save_memories()`, `_save_history()`.

#### Step 4: Make `_agent_worker` pure

```python
def _agent_worker(self, client, model, messages):
    """Pure worker: LLM call only. Puts result in outbox."""
    try:
        result = call_llm(client, model, messages)
        self._outbox.put(("agent", result))
    except Exception:
        logger.exception("Agent LLM call failed")
        self._outbox.put(("agent", None))
```

Worker receives immutable inputs (client, model, message list). Returns result via outbox as a tagged tuple. Never touches `self.agent`.

Update `_submit_agent_run`:
```python
def _submit_agent_run(self):
    if self._agent_running:
        return
    self._agent_running = True
    channel_users = list(self.channels[self.channel].users()) if self.channel in self.channels else None
    messages = self.agent.build_llm_messages(channel_users)
    self._executor.submit(self._agent_worker, self.agent._client, self.agent._model, messages)
```

All state reading happens on main thread. Worker gets copies.

#### Step 5: Make `_feed_worker` pure

```python
def _feed_worker(self, feeds, filters, check_length, seen):
    """Pure worker: HTTP fetch only. Puts result in outbox."""
    try:
        new_items, updated_seen = check_feeds(feeds, filters, check_length, seen)
        self._outbox.put(("feed", new_items, updated_seen))
    except Exception:
        logger.exception("Exception while checking the feeds:")
        self._outbox.put(("feed_error", "Checking the feeds failed."))
```

`check_feeds` is already a pure function. The fix is in the wiring — pass copies in, get results back.

Update `_tick_feeds`:
```python
def _tick_feeds(self):
    try:
        if not self._feed_busy:
            self._feed_busy = True
            self._executor.submit(
                self._feed_worker,
                list(self.settings.get("feeds", [])),
                list(self.settings.get("filters", [])),
                self.settings.get("check_length", CHECK_LENGTH),
                list(self.seen),
            )
    ...
```

Main thread copies all inputs before dispatch.

#### Step 6: Restructure `_drain_outbox` to process structured results

```python
def _drain_outbox(self):
    while True:
        try:
            item = self._outbox.get_nowait()
        except queue.Empty:
            break

        if isinstance(item, str):
            # Plain IRC message (e.g. error text)
            for chunk in split_message(item):
                self.connection.privmsg(self.channel, chunk)

        elif item[0] == "agent":
            result = item[1]
            self._agent_running = False
            if result:
                msg = self.agent.apply_llm_result(result)
                if msg:
                    for chunk in split_message(msg):
                        self.connection.privmsg(self.channel, chunk)

        elif item[0] == "feed":
            _, new_items, updated_seen = item
            self.seen = updated_seen
            for feed_item in new_items:
                feed_msg = f"New item: {feed_item['link']} | {feed_item['title']}"
                for chunk in split_message(feed_msg):
                    self.connection.privmsg(self.channel, feed_msg)
                self.agent.add_message(self.nickname, feed_msg)
            self._feed_busy = False

        elif item[0] == "feed_error":
            self._feed_busy = False
            for chunk in split_message(item[1]):
                self.connection.privmsg(self.channel, chunk)
```

This is the heart of the refactor. All state mutation happens here, on the main thread.

#### Step 7: Simplify persistence — unconditional periodic save

Replace `_tick_save` (which checked dirty flags) with:

```python
def _tick_save(self):
    """Unconditionally save all state to disk — every 30s on main thread."""
    try:
        self.agent.save_memories()
        self.agent.save_history()
        self._save_seen()
    except Exception:
        logger.exception("Save tick failed")
```

No dirty flags. No conditionals. Just save. Three small JSON files every 30s is nothing.

#### Step 8: Remove `_seen_dirty`, `_feed_busy` cleanup

`_feed_busy` is now cleared in `_drain_outbox` (main thread) instead of in the worker's `finally` block. This means even if the worker crashes, the main thread still clears the flag when it processes the error result.

`_seen_dirty` is deleted — no longer needed.

### Summary of what gets deleted

- `threading.Lock` in `AgentState`
- `_memories_dirty`, `_history_dirty` in `AgentState`
- `_seen_dirty` in `MyBot`
- `_running` in `AgentState` (replaced by `_agent_running` in `MyBot`)
- `save_if_dirty()` in `AgentState`
- `run()` in `AgentState` (replaced by `build_llm_messages` + `apply_llm_result`)
- `import threading` in `agent.py`

### Summary of what gets added

- `call_llm()` pure function in `agent.py`
- `build_llm_messages()` method on `AgentState` (extracted from `_build_messages`)
- `apply_llm_result()` method on `AgentState` (extracted from `run()`)
- Structured result handling in `_drain_outbox`
- `_agent_running` flag on `MyBot` (plain bool, main thread only)
