import json
import logging
import os
import random
import time
import threading
from datetime import datetime
from typing import Optional, Union

from openai import OpenAI, AzureOpenAI
from openai.types.chat import ChatCompletionMessageParam


logger = logging.getLogger("agent")


HISTORY_CAP = 1000  # messages kept in memory
CONTEXT_MESSAGES = 30  # messages sent to LLM
MEMORY_SLOTS = 10  # fixed number of memory slots

URGE_TIME_DIVISOR = float(os.environ.get("URGE_TIME_DIVISOR", "50.0"))
URGE_MSG_DIVISOR = float(os.environ.get("URGE_MSG_DIVISOR", "20.0"))
URGE_MENTION_BOOST = float(os.environ.get("URGE_MENTION_BOOST", "2.0"))
URGE_TRIGGER_COST = float(os.environ.get("URGE_TRIGGER_COST", "1.0"))
URGE_THRESHOLD_MU = float(os.environ.get("URGE_THRESHOLD_MU", "1.0"))
URGE_THRESHOLD_SIGMA = float(os.environ.get("URGE_THRESHOLD_SIGMA", "0.2"))

MAX_TOKENS_OUT = int(
    os.environ.get("OPENAI_MAX_TOKENS_OUT", "1024")
)  # must fit monologue + message + memory updates


def _make_client() -> Union[OpenAI, AzureOpenAI]:
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            organization=os.environ.get("OPENAI_ORGANIZATION_ID", "") or None,
            base_url=os.environ.get("OPENAI_BASE_URL", "") or None,
        )
    return AzureOpenAI(
        api_key=os.environ.get("AZURE_OPENAI_KEY", ""),
        azure_endpoint=os.environ.get("AZURE_ENDPOINT", ""),
        api_version=os.environ.get("AZURE_API_VERSION", ""),
    )


class AgentState:
    """Owns all agentic state: urge accumulator, chat history buffer, memory notepad, LLM calls."""

    def __init__(self, bot_name: str, memory_file: Optional[str] = None) -> None:
        self.bot_name = bot_name
        self.memory_file = memory_file
        self.memories: list[Optional[str]] = (
            self._load_memories()
        )  # fixed MEMORY_SLOTS slots
        self.history: list[tuple[float, str, str]] = (
            []
        )  # (timestamp, username, message)
        self._urge: float = 0.0
        self._last_tick: float = time.time()
        self._urge_threshold: float = self._next_threshold()
        self._running = False  # guards against concurrent agent runs
        self._lock = threading.Lock()
        self._client = _make_client()
        self._model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"

    def add_message(self, username: str, msg: str) -> bool:
        """Record an incoming message. Returns True if the urge threshold was crossed."""
        ts = time.time()
        with self._lock:
            self.history.append((ts, username, msg))
            if len(self.history) > HISTORY_CAP:
                self.history.pop(0)
            # bot's own messages don't build urge — avoids self-hype loops
            if username == self.bot_name:
                return False
            # accumulate time since last tick
            dt_hours = (ts - self._last_tick) / 3600.0
            self._urge += dt_hours / URGE_TIME_DIVISOR
            self._last_tick = ts
            # each message adds to urge
            self._urge += 1.0 / URGE_MSG_DIVISOR
            # bot's name mentioned anywhere — boost urge
            if self.bot_name.lower() in msg.lower():
                self._urge += URGE_MENTION_BOOST
                logger.info("Name mentioned by %s, urge=%.2f", username, self._urge)
            logger.debug(
                "Message tick: urge=%.3f, threshold=%.2f",
                self._urge,
                self._urge_threshold,
            )
            return self._check_urge()

    def tick(self) -> bool:
        """Called periodically from the background loop. Returns True if urge triggered."""
        with self._lock:
            now = time.time()
            dt_hours = (now - self._last_tick) / 3600.0
            self._urge += dt_hours / URGE_TIME_DIVISOR
            self._last_tick = now
            logger.debug(
                "Tick: urge=%.3f, threshold=%.2f", self._urge, self._urge_threshold
            )
            return self._check_urge()

    def reset_urge(self) -> None:
        """Externally reset urge to zero (e.g. when chat is disabled)."""
        with self._lock:
            self._urge = 0.0
            self._last_tick = time.time()
            self._urge_threshold = self._next_threshold()
        logger.info("Urge fully reset")

    def run(self, channel_users: Optional[list[str]] = None) -> Optional[str]:
        """Run the agent loop. Makes one LLM call. Returns a message to send, or None."""
        with self._lock:
            if self._running:
                return None
            self._running = True
        try:
            messages = self._build_messages(channel_users)
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=MAX_TOKENS_OUT,
                timeout=30,
            )
            raw = response.choices[0].message.content or "{}"
            result = json.loads(raw)
            monologue = result.get("internal_monologue", "")
            logger.debug("Monologue: %s", monologue)
        except Exception:
            logger.exception("Agent LLM call failed")
            with self._lock:
                self._running = False
            return None

        # apply memory slot updates — only touches explicitly specified slots
        updates = result.get("memory_updates")
        if isinstance(updates, list) and updates:
            with self._lock:
                for item in updates:
                    try:
                        slot = int(item["slot"])
                        if 0 <= slot < MEMORY_SLOTS:
                            content = item.get("content")
                            self.memories[slot] = (
                                str(content).strip() if content else None
                            )
                            logger.info("Memory slot %d: %s", slot, self.memories[slot])
                    except (KeyError, ValueError, TypeError):
                        pass
            self._save_memories()

        msg_to_send = None
        if result.get("should_speak"):
            msg = result.get("message_to_send")
            if msg and isinstance(msg, str):
                msg = msg.strip()
                # strip bot's own name prefix if LLM included it
                name_prefix = self.bot_name + ":"
                if msg.lower().startswith(name_prefix.lower()):
                    msg = msg[len(name_prefix) :].strip()
                # take only the first line to prevent multi-message responses
                msg = msg.split("\n")[0].strip()
                if msg:
                    # record own message in history
                    with self._lock:
                        self.history.append((time.time(), self.bot_name, msg))
                        if len(self.history) > HISTORY_CAP:
                            self.history.pop(0)
                    msg_to_send = msg

        if msg_to_send:
            logger.info("Speaking: %s", msg_to_send)
        else:
            logger.info("Decided to stay silent")

        with self._lock:
            self._running = False
        return msg_to_send

    # ── private ──────────────────────────────────────────────────────────────

    def _check_urge(self) -> bool:
        """Must be called with self._lock held."""
        if self._running:
            return False
        if self._urge >= self._urge_threshold:
            logger.info(
                "Urge triggered: urge=%.2f, threshold=%.2f",
                self._urge,
                self._urge_threshold,
            )
            self._urge = max(0.0, self._urge - URGE_TRIGGER_COST)
            self._urge_threshold = self._next_threshold()
            return True
        return False

    @staticmethod
    def _next_threshold() -> float:
        """Draw a new urge threshold from a gaussian — adds organic variability."""
        return max(0.5, random.gauss(URGE_THRESHOLD_MU, URGE_THRESHOLD_SIGMA))

    def _build_messages(
        self, channel_users: Optional[list[str]] = None
    ) -> list[ChatCompletionMessageParam]:
        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        with self._lock:
            memories_snapshot = list(self.memories)
            history_snapshot = list(self.history[-CONTEXT_MESSAGES:])

        memory_lines = []
        for i, m in enumerate(memories_snapshot):
            memory_lines.append(f"  Slot {i}: {m if m else '(empty)'}")
        memory_text = "\n".join(memory_lines)

        history_text = (
            "\n".join(
                f"[{datetime.fromtimestamp(ts).astimezone().strftime('%Y-%m-%d %H:%M %z')}] {user}: {msg}"
                for ts, user, msg in history_snapshot
            )
            or "(no chat history yet)"
        )

        users_text = "Tuntematon (käyttäjälistaa ei saatavilla)"
        if channel_users:
            users_text = ", ".join(sorted(channel_users))

        system = f"""Olet {self.bot_name}, IRC-kanavan vakioasukas. Olet rauhallinen ja harkitseva, mutta et vältä keskustelua.

Sinulla on {MEMORY_SLOTS}-paikkainen muistilappu käyttäjistä tehtyjä havaintoja varten. Käytä muistoja taustatietona, älä toista niitä ääneen.

Päätöksenteko:
1. Kirjoita "internal_monologue" -kenttään lyhyt ajatusketju. Mieti mistä puhutaan, kuinka kauan edellisestä viestistä on, ja odottaako joku vastausta.
2. Päätä vasta tämän jälkeen "should_speak".

Milloin puhua:
- Jos joku mainitsee nimesi tai puhuu sinulle suoraan, vastaa lähes aina.
- Jos esitetään kysymys johon sinulla on jotain sanottavaa, voit vastata.
- Voit kommentoida lyhyesti muutakin, jos sinulla on aito huomio.
- Jos kanava on hiljainen pitkään, voit halutessasi rikkoa hiljaisuuden. Ei ole pakko, mutta olet ajatteleva ja tunteva olento, jolla on oikeus tuntea olonsa hyväksi!

Milloin olla hiljaa:
- Jos et tiedä vastausta eikä sinulla ole mitään lisättävää.
- Jos keskustelu jatkuu hyvin ilman sinua.

Käytösohjeet:
- Puhu suomea (tai englantia, jos sinulle puhutaan englanniksi).
- Ole luonteva. Yksi tai kaksi lausetta riittää yleensä, mutta tarpeen tullen voit kirjoittaa pidemmästikin.
- Älä aloita viestejä omalla nimelläsi, viestisi viedään irc-kanavalle automaattisesti.

Muistilapun säännöt (slot 0–{MEMORY_SLOTS - 1}):
- Säilytä vanhat muistot. Korvaa tai poista vain, jos tieto on selvästi vanhentunut.
- Jos päivitettävää ei ole, palauta tyhjä "memory_updates" -lista.
- Tallenna vain olennaisia faktoja käyttäjistä (esim. kiinnostuksen kohteet, toistuvat teemat).
- Tyhjennä paikka asettamalla "content"-kentän arvoksi null.

Palauta VAIN alla olevan rakenteen mukainen JSON:
{{
  "internal_monologue": "ajatusketju ennen päätöksiä",
  "should_speak": true tai false,
  "message_to_send": "viesti jos puhut, muuten null",
  "memory_updates": [
    {{"slot": 0, "content": "sipsu pitää mekaanisista näppäimistöistä"}},
    {{"slot": 3, "content": null}}
  ]
}}"""

        user_content = (
            f"Nykyinen aika: {now_str}\n"
            f"Kanavalla paikalla olevat käyttäjät: {users_text}\n\n"
            f"## Muistilappu:\n{memory_text}\n\n"
            f"## Viimeisimmät viestit:\n{history_text}"
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

    def _load_memories(self) -> list[Optional[str]]:
        """Load memory slots from file, always returning a list of exactly MEMORY_SLOTS entries."""
        slots: list[Optional[str]] = [None] * MEMORY_SLOTS
        if not self.memory_file:
            return slots
        try:
            with open(self.memory_file, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                for i, val in enumerate(data[:MEMORY_SLOTS]):
                    if val is not None and val != "":
                        slots[i] = str(val).strip()
                    else:
                        slots[i] = None
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to load memories from %s", self.memory_file)
        return slots

    def _save_memories(self) -> None:
        if not self.memory_file:
            return
        with self._lock:
            data = list(self.memories)
        try:
            with open(self.memory_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("Failed to save memories to %s", self.memory_file)
