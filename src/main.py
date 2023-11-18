import irc
import logging
import os
import time
from irc.bot import SingleServerIRCBot
from threading import Thread
from typing import List, Optional, Tuple
from src.parser import check_feeds
from src.chat import chat


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


logger = logging.getLogger("app")


# default values, overridable by interactive commands
FEEDS = [
    "https://bbs.io-tech.fi/forums/naeytoenohjaimet.74/index.rss",
    "https://bbs.io-tech.fi/forums/prosessorit-emolevyt-ja-muistit.73/index.rss",
    "https://www.tori.fi/koko_suomi/tietokoneet_ja_lisalaitteet/komponentit?ca=18&cg=5030&c=5038&st=s&st=k&st=u&st=h&st=g&st=b&w=3&o=2",
    "https://www.tori.fi/koko_suomi/tietokoneet_ja_lisalaitteet/komponentit?ca=18&cg=5030&c=5038&w=3&st=s&st=k&st=u&st=h&st=g&st=b",
]
CHECK_INTERVAL = 60
CHECK_LENGTH = 3600
FILTERS = ["4070", "4080", "3090", "3080", "980 ?ti", "12900", "13700", "7800x3d"]


def split_message(msg, max_length=256):
    """The IRC protocal has a max length of 512 bytes / msg, so safely split before that happens..
    Note that 512 bytes does not mean 512 characters."""
    while msg:
        chunk, msg = msg[:max_length], msg[max_length:]
        yield chunk


class MyBot(SingleServerIRCBot):
    """The stateful bot, inheriting from irc.SingleServerIRCBot."""

    def __init__(
        self,
        channel: str,
        nickname: str,
        server: str,
        port: int = 6667,
        sasl_password: Optional[str] = None,
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

        self.channel = channel

        self.feeds = FEEDS
        self.check_interval = CHECK_INTERVAL
        self.check_length = CHECK_LENGTH
        self.filters = FILTERS

        self.seen: List[str] = []

        self.nickname = nickname

        self.history: List[Tuple[str, str]] = []
        self.instruction: Optional[str] = None

    def on_nicknameinuse(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """If nickname is in use on join, try a different name."""
        new_name = c.get_nickname() + "_"
        c.nick(new_name)
        self.nickname = new_name

    def on_welcome(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """On welcome to the server, join the channel and start the main loop."""
        c.join(self.channel)

        # start the main loop
        self.start_main_loop()

    def on_pubmsg(self, c: irc.client.Connection, e: irc.client.Event) -> None:
        """Handle interactive parts."""
        msg = e.arguments[0]

        try:
            username = e.source.split("!")[0]
        except Exception as exc:
            username = "unknown"

        commands = []

        commands.append(("!filters", "Show all filters"))
        if msg == "!filters":
            self.send_message("Filters: " + ", ".join(self.filters))

        commands.append(("!nofilters", "Clear all filters"))
        if msg == "!nofilters":
            self.send_message("Clearing filters.")
            self.filters = []

        commands.append(("!filter <regexp>", "Add new filter"))
        if msg.startswith("!filter") and len(msg.split(" ")) > 1:
            value = " ".join(msg.split(" ")[1:])
            self.send_message("Adding new filter: " + value)
            self.filters.append(value)

        commands.append(("!feeds", "Show all feeds"))
        if msg == "!feeds":
            self.send_message("Feeds: " + ", ".join(self.feeds))

        commands.append(("!nofeeds", "Clear all feeds"))
        if msg == "!nofeeds":
            self.send_message("Clearing feeds.")
            self.feeds = []

        commands.append(("!feed <url>", "Add new feed"))
        if msg.startswith("!feed") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Adding new feed: " + value)
            self.feeds.append(value)

        commands.append(("!inst <instruction>", "Set new system instruction"))
        if msg.startswith("!inst"):
            if len(msg.split(" ")) > 1:
                self.instruction = " ".join(msg.split(" ")[1:])
            else:
                self.instruction = ""

            self.send_message("Setting new instruction: " + str(self.instruction))

        commands.append(("!definst", "Set default system instruction"))
        if msg == "!definst":
            self.instruction = None
            self.send_message("Using default instruction.")

        commands.append(("!check_interval", "Show check interval"))
        if msg == "!check_interval":
            self.send_message("Check interval: " + str(self.check_interval))

        commands.append(("!check_interval <int>", "Set check interval"))
        if msg.startswith("!check_interval") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Setting check interval to: " + value)
            try:
                self.check_interval = int(value)
            except ValueError:
                pass

        commands.append(("!check_length", "Show check length"))
        if msg == "!check_length":
            self.send_message("Check length: " + str(self.check_length))

        commands.append(("!check_length <int>", "Set check length"))
        if msg.startswith("!check_length") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.send_message("Setting check length to: " + value)
            try:
                self.check_length = int(value)
            except ValueError:
                pass

        commands.append((f"!chat <msg> (or `{self.nickname}: <msg>`)", "Chat with me!"))
        if (msg.startswith("!chat") or msg.startswith(f"{self.nickname}: ")) and len(
            msg.split(" ")
        ) > 1:
            value = " ".join(msg.split(" ")[1:])

            # get response from openai
            try:
                new_history = chat(
                    self.history + [(username, value)], self.nickname, self.instruction
                )
            except Exception as exc:
                new_history = [(self.nickname, "Something went wrong.. :(")]
                logger.exception("Something went wrong when talking to openai:")

            # send the response as messages
            for item in new_history:
                time.sleep(1.0)
                self.send_message(f"{item[1]}")

            # update history with old history, current msg and openai responses
            self.history = self.history + [(username, msg)] + new_history

        else:
            # update history also when not explicitly chatting
            self.history = self.history + [(username, msg)]

        commands.append(("!commands", "Show this message"))
        if msg == "!commands":
            self.send_message("All commands: ")
            padding = max(len(command[0]) for command in commands) + 2
            for command, description in commands:
                time.sleep(1.0)
                self.send_message(command.ljust(padding) + description)

    def send_message(self, msg):
        """Helper to send messages."""
        for chunk in split_message(msg):
            self.connection.privmsg(self.channel, chunk)

    def start_main_loop(self) -> None:
        """Start the periodical main loop."""

        def loop_check() -> None:
            """Run the main loop."""
            while True:
                # check if new interesting items
                try:
                    new_items, self.seen = check_feeds(
                        self.feeds, self.filters, self.check_length, self.seen
                    )
                    # if yes, msg to channel
                    for item in new_items:
                        self.send_message(f"New item: {item['link']}")
                except Exception as exc:
                    self.send_message(f"Checking the feeds failed.")
                    logger.exception("Exception while checking the feeds:")

                time.sleep(self.check_interval)

        Thread(target=loop_check).start()


def main_bot(
    channel: str, nickname: str, server: str, port: int, sasl_password: Optional[str]
) -> None:
    """Start the ircbot."""
    bot = MyBot(channel, nickname, server, port, sasl_password)
    bot.start()


def main():
    channel = os.environ.get("BOT_CHANNEL", "#vellumotest")
    nickname = os.environ.get("BOT_NICKNAME", "vellubot")
    server = os.environ.get("BOT_SERVER", "irc.libera.chat")
    port = int(os.environ.get("BOT_PORT", "6667"))
    sasl_password = os.environ.get("BOT_SASL_PASSWORD", None)
    main_bot(channel, nickname, server, port, sasl_password)


if __name__ == "__main__":
    main()
