# Agentic IRC Bot — Implementation Plan

## Goal

Transform the IRC bot from a reactive `!chat` command responder into an autonomous, shy, human-like channel presence. The bot observes chat, decides on its own when to speak (or stay silent), and maintains persistent memories about users and topics across restarts.

---

## Architecture: Three Decoupled Parts

```
src/
  main.py     IRC glue: events, commands, background loop
  agent.py    All agent logic: urge, history, memory, LLM calls
  parser.py   Feed parsing (RSS, tori.fi) — completely untouched
```

`parser.py` has zero knowledge of the agent. `agent.py` has zero knowledge of IRC. `main.py` wires them together. Feed checking and agent ticking run in the same background loop but are wrapped in independent `try/except` blocks — one can fail without killing the other.

---

## 1. The Urge Accumulator

The core mechanism that decides *when* the bot "checks IRC."

### Design

A single `_urge` float accumulates continuously. Both time and messages add to it incrementally; there is no separate recomputation from stored counters.

```
# on each incoming message (from others):
_urge += dt_hours / 50.0      # time since last message/tick
_urge += 1.0 / 20.0           # the message itself

# on background tick:
_urge += dt_hours / 50.0      # time since last message/tick

# if bot's name appears anywhere in message:
_urge += 1.0                  # mention boost (URGE_MENTION_BOOST)
```

Time is accumulated via `_last_tick`, updated on every `add_message` and `tick` call, so time is never double-counted between the two paths.

### Trigger

Evaluated on every incoming message (`on_pubmsg`) and every background tick (every `check_interval` seconds, default 60). When `_urge >= threshold`, the bot calls the LLM — **unless a call is already in progress**, in which case urge keeps accumulating untouched until the running call finishes.

### Gaussian threshold

Each cycle draws a new threshold from `gauss(1.0, 0.2)` floored at 0.5. This makes the bot's rhythm slightly unpredictable — sometimes it checks after 15 messages, sometimes after 25; sometimes after 35 hours of silence, sometimes after 60.

### When urge goes UP

| Event | Effect |
|---|---|
| Someone else sends a message | `+1/20` (≈ 0.05) |
| Bot's name mentioned in message | `+1.0` on top of the message increment |
| Time passes silently | Time component grows continuously |

### When urge goes DOWN (partial reset)

| Event | Effect |
|---|---|
| Urge crosses threshold → LLM called | `urge = max(0, urge − 1.0)`, new threshold drawn |
| `!chat_enabled` toggled off | Full reset to 0 |

Urge is **not** zeroed on trigger — it is reduced by `URGE_TRIGGER_COST = 1.0`. This means the bot can sustain a short back-and-forth when there is active conversation (high urge carries over), while still requiring the full ~50 hours or ~20 messages to speak again from a cold state.

### What does NOT affect urge

| Event | Why |
|---|---|
| Bot's own messages (including feed announcements) | Prevents self-hype loops |
| Whether the bot speaks or stays silent after LLM call | Urge decrements on CHECK, not on SPEAK |
| Incoming messages while LLM is running | Urge accumulates safely; no spurious trigger or urge drain |

### Name mention vs. direct address

Any message containing the bot's name (case-insensitive) adds `URGE_MENTION_BOOST = 1.0`. There is no longer a separate "direct address" instant-trigger path — the mention boost plus the message increment (~1.05 total from zero) will almost always cross the threshold on the same message.

---

## 2. The Agent Loop

When urge fires, `AgentState.run()` is called in a background thread (guarded against concurrent runs with a `_running` flag).

### LLM call structure

Two messages sent to the API:

1. **System prompt** (in Finnish) — personality, timestamp awareness, memory instructions, JSON schema
2. **User message** — current memory slots + last 30 chat messages with timestamps

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

- `internal_monologue` must be filled in FIRST — this is the bot's chain-of-thought. It considers what's happening, how long since the last message, whether it has something genuine to say.
- `should_speak` — the bot can decide to stay silent, exactly like a human lurking.
- `message_to_send` — single message. Code-side guards strip the bot's own name prefix and take only the first line.
- `memory_updates` — sparse slot updates (see Memory section below).

### API details

- `response_format={"type": "json_object"}` — works identically on both OpenAI and Azure OpenAI
- `max_tokens=1024` — enough for monologue + message + memory updates
- `timeout=30` seconds
- On any failure (network, bad JSON, timeout): logged, bot stays silent, `_running` flag cleared. Next urge cycle tries again.

---

## 3. Memory: The Slot-Based Notepad

### Design

10 numbered slots (0–9), each holding a short text or `null`. Persisted to a JSON file (`AGENT_MEMORY_FILE` env var).

### Why slots instead of full-list replacement

The original plan had the LLM return a complete replacement memory list on every call. This was fragile: a truncated JSON response or confused LLM could silently wipe all memories. The slot system is failure-proof — the LLM returns only the specific changes it wants to make:

```json
"memory_updates": [
  {"slot": 2, "content": "new content"},
  {"slot": 7, "content": null}
]
```

- If `memory_updates` is absent, empty, or malformed → **all 10 slots remain untouched**
- Each update is individually validated (slot must be 0–9)
- Clearing a slot: set `content` to `null`

### Prompt instructions (Finnish, summarized)

- Preserve existing memories — only change a slot if clearly outdated or a new observation is more important
- If nothing noteworthy happened, return `memory_updates: []`
- Prioritize per-user observations: interests, humor, projects, recurring topics
- Use memories to quietly color interactions, but never quote them back at people

### File format

```json
[
  "sipsu tykkää mekaanisista näppäimistöistä",
  null,
  "Zairex katsoo usein näytönohjaintarjouksia",
  null,
  null,
  null,
  null,
  null,
  null,
  null
]
```

Always exactly 10 entries. Loaded on startup, saved after each update.

---

## 4. Chat History

- In-memory only (lost on restart — this is intentional)
- Rolling buffer of 1000 entries, each: `(timestamp: float, username: str, message: str)`
- LLM receives the most recent 30 messages, formatted with ISO timestamps:

```
[2026-03-02 16:30] sipsu: moi mitä kuuluu
[2026-03-02 16:31] Zairex: ihan hyvää, katselin just uusia näyttiksiä
[2026-03-02 16:45] vellubot: onko jotain hyvää tarjouksessa?
```

- Feed announcements are also recorded in history (as bot's own messages) so the agent is aware of them
- Current time is injected into the system prompt so the LLM can calculate how long ago the last message was

---

## 5. System Prompt (Finnish)

The prompt establishes:

- **Personality**: quiet, shy, observant lurker
- **Language**: Finnish by default, English only if directly addressed in English
- **Brevity**: one sentence is often enough
- **Time awareness**: current time provided, timestamps in chat for comparison
- **Memory discipline**: preserve existing, update only when genuinely needed
- **Self-control**: never prefix own name, never continue others' lines, one message only

---

## 6. IRC Commands

### Kept from before (feed/filter management)
`!filters`, `!nofilters`, `!filter <regexp>`, `!delfilter <idx>`, `!feeds`, `!nofeeds`, `!feed <url>`, `!delfeed <idx>`, `!check_interval [<int>]`, `!check_length [<int>]`, `!commands`

### Added
`!chat_enabled` — toggles autonomous chat on/off. Resets urge on disable.

### Removed
`!chat`, `!inst`, `!definst` — no more manual chatting or prompt injection. The bot decides when to speak.

### Direct address
Mentioning the bot's name anywhere in a message (not just `botname: ...` prefix) adds `URGE_MENTION_BOOST = 1.0` to urge. This usually triggers an immediate LLM call, enabling natural back-and-forth conversation.

---

## 7. Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `BOT_CHANNEL` | IRC channel | `#vellumotest` |
| `BOT_NICKNAME` | Bot name | `vellubot` |
| `BOT_SERVER` | IRC server | `irc.libera.chat` |
| `BOT_PORT` | IRC port | `6667` |
| `BOT_SASL_PASSWORD` | SASL auth password | None |
| `SETTINGS_FNAME` | Settings JSON file path | None |
| `AGENT_MEMORY_FILE` | Memory slots JSON file path | None |
| `OPENAI_API_KEY` | OpenAI API key (if using OpenAI) | None |
| `OPENAI_ORGANIZATION_ID` | OpenAI organization ID (optional) | None |
| `OPENAI_BASE_URL` | Custom OpenAI-compatible endpoint (optional) | None |
| `AZURE_OPENAI_KEY` | Azure OpenAI key (if using Azure) | None |
| `AZURE_ENDPOINT` | Azure endpoint URL | None |
| `AZURE_API_VERSION` | Azure API version | None |
| `OPENAI_MODEL` | LLM model name | `gpt-4o-mini` |

---

## 8. Constants (in `agent.py`)

| Constant | Value | Meaning |
|---|---|---|
| `HISTORY_CAP` | 1000 | Messages kept in rolling buffer |
| `CONTEXT_MESSAGES` | 30 | Messages sent to LLM per call |
| `MEMORY_SLOTS` | 10 | Fixed memory slot count |
| `URGE_TIME_DIVISOR` | 50.0 | Hours of silence to accumulate 1.0 urge |
| `URGE_MSG_DIVISOR` | 20.0 | Messages to accumulate 1.0 urge |
| `URGE_MENTION_BOOST` | 2.0 | Urge added when bot's name is mentioned |
| `URGE_TRIGGER_COST` | 1.0 | Urge subtracted per LLM call |
| `URGE_THRESHOLD_MU` | 1.0 | Mean of gaussian threshold |
| `URGE_THRESHOLD_SIGMA` | 0.2 | Stddev — variability per cycle |
| `MAX_TOKENS_OUT` | 1024 | Max output tokens from LLM |

---

## 9. Emergent Behaviors

- **Emergent conversation**: After a mention, urge partially carries over after the trigger. The next mention or a few messages can trigger again — enabling a natural short back-and-forth without brute-forcing.
- **Natural silence**: Bot checks IRC and decides not to speak. Exactly like a human lurking.
- **Silence breaking**: After ~50 hours of quiet, bot wakes up and might say something to an empty channel.
- **Self-monologue**: Bot sees its own previous message as last activity. Might follow up naturally.
- **Memory curation**: Over time the notepad converges on the most important per-user observations, with the LLM merging and pruning as needed.
- **Organic rhythm**: Gaussian threshold means no two cycles feel identical.

---

## 10. Robustness

- **Feed checking fails** → caught, logged, agent continues. Agent tick runs independently.
- **Agent tick fails** → caught, logged, feed checking continues.
- **LLM call fails** → caught, logged, `_running` flag cleared. Bot stays silent.
- **Truncated/malformed JSON from LLM** → memories untouched (slot updates are sparse), no message sent.
- **Settings file missing keys** → `Settings.get()` uses defaults, no KeyError.
- **Concurrent triggers** → `_running` flag prevents double LLM calls and prevents urge drain while a call is in progress.
- **Bot name prefix in response** → stripped code-side. Multi-line responses → only first line taken.

---

## 11. Changes from original codebase

| File | Change |
|---|---|
| `src/chat.py` | **Deleted** — replaced by `agent.py` |
| `src/agent.py` | **New** — `AgentState` class with all agent logic; uses `logger = logging.getLogger("agent")` |
| `src/main.py` | Refactored: removed `!chat`/`!inst`/`!definst`, added `!chat_enabled`, wired agent into events and background loop, added `memory_fname` parameter; uses `logger = logging.getLogger("main")`; fixed `Settings.__init__` bug (`self.load()` instead of `self.load(fname)`) |
| `src/parser.py` | Logger renamed to `logging.getLogger("parser")` — otherwise untouched |
| `flake.nix` | Removed `tiktoken` dependency (no longer needed) |

---

## 12. Testing Plan

Before deploying to the real channel, test thoroughly in `#vellumotest` (or similar test channel).

### Urge tuning for testing

Temporarily lower the divisors so triggers happen in seconds/messages rather than hours:

```python
URGE_TIME_DIVISOR = 0.01   # ~36 seconds of silence reaches urge 1.0
URGE_MSG_DIVISOR = 3.0     # 3 messages reaches urge 1.0
```

Remember to restore production values (`50.0` / `20.0`) before deploying.

### IRC debug announcements

Add a `!debug` mode where the bot announces its internal state out loud after every tick and trigger — current urge, threshold, whether it spoke or stayed silent, and memory slot count. Crucially, **these debug announcements must not be recorded in the agent's own history** (add them to the channel via `send_message` without calling `agent.add_message`), so the agent never sees its own debug output and it doesn't pollute the context.

### Things to verify

- Bot stays silent during normal low-activity chat (urge climbs slowly, LLM chooses silence)
- Mentioning bot name reliably triggers a response
- Bot can sustain 2–3 message exchange when actively spoken to
- Bot breaks silence after configured idle time
- Memory slots are updated and persisted correctly across restarts
- `!chat_enabled` toggle works cleanly (urge resets, no immediate fire on re-enable)

### System prompt review

Go through the Finnish system prompt carefully before deploying:
- Is the personality description accurate and consistent?
- Are the memory instructions clear and unambiguous?
- Does the JSON schema example match the actual expected format?
- Is the language/brevity guidance strong enough to prevent verbose responses?

---

## Appendix: Discarded Ideas

- **Social Battery**: Speaking depletes energy, fed into prompt. Discarded — "double-dipping" risk.
- **Hidden Target Silence**: Gaussian random future time to break silence. Discarded — accumulator handles it natively.
- **Emotional State Vectors**: `happiness`, `boredom`, etc. Postponed — keep it simple.
- **Full memory replacement**: LLM returns entire memory list each call. Replaced with slot-based updates for robustness.
- **Multiplicative urge model**: Urge multiplied each tick to converge toward 1. Discarded — multiplicative models need an additive baseline to escape zero, at which point they're just additive models with extra complexity. The additive accumulator with partial reset achieves the desired conversation-sustaining behavior more transparently.