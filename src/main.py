import irc
import logging
import os
import queue
import time
import json
from concurrent.futures import ThreadPoolExecutor
from irc.bot import SingleServerIRCBot
from typing import List, Optional
from src.parser import check_feeds
from src.agent import AgentState


log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


logger = logging.getLogger("main")


# default values, overridable by interactive commands
FEEDS = [
    "https://bbs.io-tech.fi/forums/naeytoenohjaimet.74/index.rss",
    "https://bbs.io-tech.fi/forums/prosessorit-emolevyt-ja-muistit.73/index.rss",
    "https://www.tori.fi/recommerce/forsale/search?product_category=2.93.3215.8368&sort=PUBLISHED_DESC",
]
CHECK_INTERVAL = 60
CHECK_LENGTH = 36000
FILTERS = ["4070"]


def split_message(msg, max_length=256):
    """The IRC protocal has a max length of 512 bytes / msg, so safely split before that happens..
    Note that 512 bytes does not mean 512 characters."""
    while msg:
        chunk, msg = msg[:max_length], msg[max_length:]
        yield chunk


class Settings:
    def __init__(self, fname=None):
        self.fname = fname

        # first initialize with default values
        self.settings = {
            "feeds": FEEDS,
            "check_interval": CHECK_INTERVAL,
            "check_length": CHECK_LENGTH,
            "filters": FILTERS,
            "chat_enabled": True,
        }

        # then if possible, overwrite from file
        if fname:
            try:
                self.settings = self.load()
            except Exception:
                logger.exception("Could not intialize settings from file")

    def set(self, key, value):
        self.settings[key] = value

        # persist to file
        if self.fname:
            try:
                self.save()
            except Exception:
                logger.exception("Could not save settings to file")

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def save(self):
        with open(self.fname, "w") as f:
            f.write(json.dumps(self.settings, indent=4))

    def load(self):
        with open(self.fname, "r") as f:
            self.settings = json.load(f)

        return self.settings


class MyBot(SingleServerIRCBot):
    """The stateful bot, inheriting from irc.SingleServerIRCBot."""

    def __init__(
        self,
        channel: str,
        nickname: str,
        server: str,
        port: int = 6667,
        sasl_password: Optional[str] = None,
        settings_fname: Optional[str] = None,
        memory_fname: Optional[str] = None,
        history_fname: Optional[str] = None,
        seen_fname: Optional[str] = None,
    ) -> None:
        """The constructor."""

        # sasl authentication may be needed for example
        # when connecting to libera from a cloud
        if sasl_password:
            SingleServerIRCBot.__init__(
                self,
                [(server, port, sasl_password)],
                nickname,
                nickname,
                sasl_login=nickname,
            )
        else:
            SingleServerIRCBot.__init__(self, [(server, port)], nickname, nickname)

        self.settings = Settings(settings_fname)

        self.channel = channel
        self.nickname = nickname
        self.seen_fname = seen_fname
        self.seen: List[str] = self._load_seen()
        self._seen_dirty: bool = False
        self.agent = AgentState(
            nickname, memory_fname=memory_fname, history_fname=history_fname
        )
        self._outbox: queue.Queue = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._feed_busy = False

    def _load_seen(self) -> List[str]:
        if not self.seen_fname:
            return []
        try:
            with open(self.seen_fname, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data]
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to load seen from %s", self.seen_fname)
        return []

    def _save_seen(self) -> None:
        if not self.seen_fname:
            return
        try:
            with open(self.seen_fname, "w") as f:
                json.dump(self.seen, f, ensure_ascii=False)
        except Exception:
            logger.exception("Failed to save seen to %s", self.seen_fname)

    def on_nicknameinuse(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """If nickname is in use on join, try a different name."""
        new_name = c.get_nickname() + "_"
        c.nick(new_name)
        self.nickname = new_name
        self.agent.bot_name = new_name

    def on_welcome(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """On welcome to the server, join the channel and start the main loop."""
        c.join(self.channel)
        self.reactor.scheduler.execute_every(0.5, self._drain_outbox)
        self.reactor.scheduler.execute_every(30, self._tick_save)
        self.reactor.scheduler.execute_after(0, self._tick_feeds)
        self.reactor.scheduler.execute_after(0, self._tick_agent)

    def on_pubmsg(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """Handle public messages: feed/filter commands and agent triggering."""
        msg = e.arguments[0]

        try:
            username = e.source.split("!")[0]
        except Exception:
            username = "unknown"

        commands = []

        commands.append(("!filters", "Show all filters"))
        if msg == "!filters":
            self.send_message(
                "Filters: "
                + ", ".join(
                    [
                        str(idx) + ": " + fltr
                        for idx, fltr in enumerate(self.settings.get("filters"))
                    ]
                )
            )

        commands.append(("!nofilters", "Clear all filters"))
        if msg == "!nofilters":
            self.send_message("Clearing filters.")
            self.settings.set("filters", [])

        commands.append(("!filter <regexp>", "Add new filter"))
        if msg.startswith("!filter") and len(msg.split(" ")) > 1:
            value = " ".join(msg.split(" ")[1:])
            self.send_message("Adding new filter: " + value)
            self.settings.set("filters", self.settings.get("filters") + [value])

        commands.append(("!delfilter <idx>", "Remove a specific filter"))
        if msg.startswith("!delfilter") and len(msg.split(" ")) > 1:
            filters = self.settings.get("filters")
            try:
                idx = int(msg.split(" ")[1])
                assert idx < len(filters) and idx >= 0

                self.send_message("Removing filter: " + filters[idx])
                del filters[idx]
                self.settings.set("filters", filters)

            except Exception:
                self.send_message("Seems you provided an invalid index.")

        commands.append(("!feeds", "Show all feeds"))
        if msg == "!feeds":
            self.send_message(
                "Feeds: "
                + ", ".join(
                    [
                        str(idx) + ": " + feed
                        for idx, feed in enumerate(self.settings.get("feeds"))
                    ]
                )
            )

        commands.append(("!nofeeds", "Clear all feeds"))
        if msg == "!nofeeds":
            self.send_message("Clearing feeds.")
            self.settings.set("feeds", [])

        commands.append(("!feed <url>", "Add new feed"))
        if msg.startswith("!feed") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Adding new feed: " + value)
            self.settings.set("feeds", self.settings.get("feeds") + [value])

        commands.append(("!delfeed <idx>", "Remove a specific feed"))
        if msg.startswith("!delfeed") and len(msg.split(" ")) > 1:
            feeds = self.settings.get("feeds")
            try:
                idx = int(msg.split(" ")[1])
                assert idx < len(feeds) and idx >= 0

                self.send_message("Removing feed: " + feeds[idx])
                del feeds[idx]
                self.settings.set("feeds", feeds)
            except Exception:
                self.send_message("Seems you provided an invalid index.")

        commands.append(("!check_interval", "Show check interval"))
        if msg == "!check_interval":
            self.send_message(
                "Check interval: " + str(self.settings.get("check_interval"))
            )

        commands.append(("!check_interval <int>", "Set check interval"))
        if msg.startswith("!check_interval") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Setting check interval to: " + value)
            try:
                self.settings.set("check_interval", int(value))
            except ValueError:
                pass

        commands.append(("!check_length", "Show check length"))
        if msg == "!check_length":
            self.send_message("Check length: " + str(self.settings.get("check_length")))

        commands.append(("!check_length <int>", "Set check length"))
        if msg.startswith("!check_length") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Setting check length to: " + value)
            try:
                self.settings.set("check_length", int(value))
            except ValueError:
                pass

        commands.append(("!chat_enabled", "Toggle autonomous chat on/off"))
        if msg == "!chat_enabled":
            enabled = self.settings.get("chat_enabled", True)
            self.settings.set("chat_enabled", not enabled)
            if enabled:
                # was on, now off — reset urge so it doesn't fire immediately on re-enable
                self.agent.reset_urge()
                self.send_message("Chat disabled.")
            else:
                self.send_message("Chat enabled.")

        commands.append(("!commands", "Show this message"))
        if msg == "!commands":
            self.send_message("All commands: ")
            padding = max(len(command[0]) for command in commands) + 2
            for command, description in commands:
                time.sleep(1.0)
                self.send_message(command.ljust(padding) + description)

        # record message and trigger agent if urge reached
        triggered = self.agent.add_message(username, msg)
        if triggered and self.settings.get("chat_enabled", True):
            self._submit_agent_run()

    def _drain_outbox(self) -> None:
        """Drain queued outgoing messages — called by reactor every 0.5s on main thread."""
        while True:
            try:
                msg = self._outbox.get_nowait()
                for chunk in split_message(msg):
                    self.connection.privmsg(self.channel, chunk)
            except queue.Empty:
                break

    def _submit_agent_run(self) -> None:
        """Snapshot channel users (safe: main thread) and submit agent worker to executor."""
        channel_users = None
        if self.channel in self.channels:
            channel_users = list(self.channels[self.channel].users())
        self._executor.submit(self._agent_worker, channel_users)

    def _agent_worker(self, channel_users: Optional[List[str]]) -> None:
        """Run the agent LLM call in the thread pool. Puts response in outbox."""
        try:
            response = self.agent.run(channel_users=channel_users)
            if response:
                self._outbox.put(response)
        except Exception:
            logger.exception("Agent worker failed")

    def _tick_feeds(self) -> None:
        """Reactor-scheduled feed tick — runs on main thread, self-reschedules."""
        try:
            if not self._feed_busy:
                self._feed_busy = True
                self._executor.submit(self._feed_worker)
        except Exception:
            logger.exception("Feed tick failed")
        finally:
            interval = self.settings.get("check_interval", CHECK_INTERVAL)
            self.reactor.scheduler.execute_after(interval, self._tick_feeds)

    def _feed_worker(self) -> None:
        """Fetch feeds in the thread pool. Puts new-item messages in outbox."""
        try:
            new_items, self.seen = check_feeds(
                self.settings.get("feeds", []),
                self.settings.get("filters", []),
                self.settings.get("check_length", CHECK_LENGTH),
                self.seen,
            )
            for item in new_items:
                feed_msg = f"New item: {item['link']} | {item['title']}"
                self._outbox.put(feed_msg)
                self.agent.add_message(self.nickname, feed_msg)
            if new_items:
                self._seen_dirty = True
        except Exception:
            logger.exception("Exception while checking the feeds:")
            self._outbox.put("Checking the feeds failed.")
        finally:
            self._feed_busy = False

    def _tick_agent(self) -> None:
        """Reactor-scheduled agent tick — runs on main thread, self-reschedules."""
        try:
            if self.settings.get("chat_enabled", True) and self.agent.tick():
                self._submit_agent_run()
        except Exception:
            logger.exception("Agent tick failed")
        finally:
            interval = self.settings.get("check_interval", CHECK_INTERVAL)
            self.reactor.scheduler.execute_after(interval, self._tick_agent)

    def _tick_save(self) -> None:
        """Flush dirty state to disk — called by reactor every 30s on main thread."""
        try:
            self.agent.save_if_dirty()
            if self._seen_dirty:
                self._seen_dirty = False
                self._save_seen()
        except Exception:
            logger.exception("Save tick failed")

    def send_message(self, msg: str) -> None:
        """Send a message directly — only call from the main (reactor) thread."""
        for chunk in split_message(msg):
            self.connection.privmsg(self.channel, chunk)


def main_bot(
    channel: str,
    nickname: str,
    server: str,
    port: int,
    sasl_password: Optional[str],
    settings_fname: Optional[str],
    memory_fname: Optional[str],
    history_fname: Optional[str] = None,
    seen_fname: Optional[str] = None,
) -> None:
    """Start the ircbot."""
    bot = MyBot(
        channel,
        nickname,
        server,
        port,
        sasl_password,
        settings_fname,
        memory_fname,
        history_fname,
        seen_fname,
    )
    bot.start()


def main():
    channel = os.environ.get("BOT_CHANNEL", "#vellumotest")
    nickname = os.environ.get("BOT_NICKNAME", "vellubot")
    server = os.environ.get("BOT_SERVER", "irc.libera.chat")
    port = int(os.environ.get("BOT_PORT", "6667"))
    sasl_password = os.environ.get("BOT_SASL_PASSWORD", None)
    settings_fname = os.environ.get("SETTINGS_FNAME", None)
    memory_fname = os.environ.get("AGENT_MEMORY_FNAME", None)
    history_fname = os.environ.get("AGENT_HISTORY_FNAME", None)
    seen_fname = os.environ.get("PARSER_SEEN_FNAME", None)
    main_bot(
        channel,
        nickname,
        server,
        port,
        sasl_password=sasl_password,
        settings_fname=settings_fname,
        memory_fname=memory_fname,
        history_fname=history_fname,
        seen_fname=seen_fname,
    )


if __name__ == "__main__":
    main()
