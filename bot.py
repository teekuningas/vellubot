import irc
import logging
import os
import pytz
import requests
import sys
import time
import traceback
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from irc.bot import SingleServerIRCBot
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple


logging.basicConfig(level=logging.DEBUG)


# default values, overridable by interactive commands
FEEDS = [
    "https://bbs.io-tech.fi/forums/naeytoenohjaimet.74/index.rss",
    "https://bbs.io-tech.fi/forums/prosessorit-emolevyt-ja-muistit.73/index.rss",
    "https://www.tori.fi/koko_suomi/tietokoneet_ja_lisalaitteet/komponentit?ca=18&cg=5030&c=5038&w=3&st=s&st=k&st=u&st=h&st=g&st=b&com=graphic_card",
]
CHECK_INTERVAL = 600
FILTERS = ["4070", "4080", "3090"]


def rfc822_to_datetime(date_string: str) -> datetime:
    """Convert rfc822 strings to tz-aware datetime objects."""
    try:
        return datetime.strptime(date_string, "%a, %d %b %Y %H:%M:%S %Z")
    except ValueError:
        try:
            return datetime.strptime(date_string, "%d %b %Y %H:%M:%S %Z")
        except ValueError:
            return datetime.strptime(date_string, "%a, %d %b %Y %H:%M:%S %z")


def tori_date_to_datetime(date_string: str) -> datetime:
    """Convert rfc822 strings to tz-aware datetime objects."""

    helsinki_tz = pytz.timezone("Europe/Helsinki")

    try:
        if date_string.startswith("tänään"):
            parsed = date_string.split("tänään ")[1]
            obj = datetime.strptime(parsed, "%H:%M")
            date = datetime.now(helsinki_tz).date()
            return helsinki_tz.localize(datetime.combine(date, obj.time()))
        elif date_string.startswith("eilen"):
            parsed = date_string.split("eilen ")[1]
            obj = datetime.strptime(parsed, "%H:%M")
            date = datetime.now(helsinki_tz).date() - timedelta(days=1)
            return helsinki_tz.localize(datetime.combine(date, obj.time()))
        else:
            # all the rest are treated being equally far away in the past, as they are difficult to parse
            return (datetime.now(helsinki_tz) - timedelta(days=2)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
    except Exception as exc:
        # on exceptions, also use the past
        return (datetime.now(helsinki_tz) - timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )


def parse_tori(feed: str) -> List[Dict[str, Any]]:
    """Return a list of standardized items given a url to tori.fi.

    Should be of format [
        {
            'uid': 'abcd',
            'title': 'ab cd',
            'datetime': <datetime obj>,
            'link': 'https://cat.cat'
        },
        ...
    ]
    """
    response = requests.get(feed)
    soup = BeautifulSoup(response.content, "lxml")
    a_tags = soup.select("a.item_row_flex")

    items = []
    for a in a_tags:
        title = a.select("div.li-title")[0].string
        link = a.get("href")
        uid = a.get("id")
        datetime_ = tori_date_to_datetime(
            a.select("div.date_image")[0]
            .string.strip()
            .replace("\n", "")
            .replace("\t", "")
        )
        items.append(
            {
                "datetime": datetime_,
                "link": link,
                "title": title,
                "uid": uid,
            }
        )
    return items


def parse_rss(feed: str) -> List[Dict[str, Any]]:
    """Return a list of standardized items given a url to .rss.

    Should be of format [
        {
            'uid': 'abcd',
            'title': 'ab cd',
            'datetime': <datetime obj>,
            'link': 'https://cat.cat'
        },
        ...
    ]
    """
    response = requests.get(feed)
    soup = BeautifulSoup(response.content, "xml")
    rss_items = soup.find_all("item")

    items = []
    for item in rss_items:
        items.append(
            {
                "datetime": rfc822_to_datetime(item.pubDate.string),
                "link": item.link.string,
                "title": item.title.string,
                "uid": item.guid.string,
            }
        )
    return items


def check_feeds(
    feeds: List[str],
    filters: List[str],
    last_checked_time: datetime,
    seen: List[str],
) -> Tuple[List[Dict[str, Any]], datetime, List[str]]:
    """Check all the feed urls for new items."""

    new_items = []

    for feed in feeds:
        # checks if the feed matches any of our parsers
        if "tori.fi" in feed:
            items = parse_tori(feed)
        elif feed.endswith(".rss"):
            items = parse_rss(feed)
        else:
            continue

        for item in items:
            # we are only interested in previously unseen items
            if item["uid"] in seen:
                continue

            # If filters present, check if the current item is ok
            if filters:
                for filter_str in filters:
                    if filter_str in item["title"]:
                        break
                else:
                    continue

            # only look at the recently updated posts
            if item["datetime"] > last_checked_time:
                seen.append(item["uid"])
                new_items.append(item)

    return new_items, datetime.now(timezone.utc), seen


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
        self.filters = FILTERS

        self.seen: List[str] = []
        self.last_checked_time = datetime.now(timezone.utc) - timedelta(
            seconds=self.check_interval
        )

    def on_nicknameinuse(
        self, c: irc.client.SimpleIRCClient, e: irc.client.Event
    ) -> None:
        """If nickname is in use on join, try a different name."""
        c.nick(c.get_nickname() + "_")

    def on_welcome(self, c: irc.client.SimpleIRCClient, e: irc.client.Event) -> None:
        """On welcome to the server, join the channel and start the main loop."""
        c.join(self.channel)

        # start the main loop
        self.start_main_loop()

    def on_pubmsg(self, c: irc.client.SimpleIRCClient, e: irc.client.Event) -> None:
        """Handle interactive parts."""
        msg = e.arguments[0]

        if msg == "!filters":
            self.connection.privmsg(self.channel, "Filters: " + ", ".join(self.filters))

        if msg == "!nofilters":
            self.connection.privmsg(self.channel, "Clearing filters.")
            self.filters = []

        if msg == "!feeds":
            self.connection.privmsg(self.channel, "Feeds: " + ", ".join(self.feeds))

        if msg == "!nofeeds":
            self.connection.privmsg(self.channel, "Clearing feeds.")
            self.feeds = []

        if msg == "!check_interval":
            self.connection.privmsg(
                self.channel, "Check interval: " + str(self.check_interval)
            )

        if msg.startswith("!check_interval") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.connection.privmsg(self.channel, "Setting check interval to: " + value)
            try:
                self.check_interval = int(value)
            except ValueError:
                pass

        if msg.startswith("!filter") and len(msg.split(" ")) > 1:
            value = " ".join(msg.split(" ")[1:])
            self.connection.privmsg(self.channel, "Adding new filter: " + value)
            self.filters.append(value)

        if msg.startswith("!feed") and len(msg.split(" ")) == 2:
            value = msg.split(" ")[1]
            self.connection.privmsg(self.channel, "Adding new feed: " + value)
            self.feeds.append(value)

    def start_main_loop(self) -> None:
        """Start the periodical main loop."""

        def loop_check() -> None:
            """Run the main loop."""
            while True:
                # check if new interesting items
                try:
                    new_items, self.last_checked_time, self.seen = check_feeds(
                        self.feeds, self.filters, self.last_checked_time, self.seen
                    )
                    # if yes, msg to channel
                    for item in new_items:
                        self.connection.privmsg(
                            self.channel, f"New item: {item['link']}"
                        )
                except Exception as exc:
                    self.connection.privmsg(self.channel, f"Checking the feeds failed.")
                    traceback.print_exc()

                time.sleep(self.check_interval)

        Thread(target=loop_check).start()


def main_bot(
    channel: str, nickname: str, server: str, port: int, sasl_password: Optional[str]
) -> None:
    """Start the ircbot."""
    bot = MyBot(channel, nickname, server, port, sasl_password)
    bot.start()


def main_parsers() -> None:
    """Run without the irc part."""

    feeds = FEEDS
    check_interval = CHECK_INTERVAL
    filters = FILTERS

    seen: List[str] = []
    last_checked_time = datetime.now(timezone.utc) - timedelta(seconds=check_interval)

    while True:
        print("Checking at: " + str(datetime.now()))

        try:
            new_items, last_checked_time, seen = check_feeds(
                feeds, filters, last_checked_time, seen
            )
            for item in new_items:
                print(f"New item: {item['link']}")
        except Exception as exc:
            print("Checking the feeds failed.")
            traceback.print_exc()

        time.sleep(check_interval)


if __name__ == "__main__":
    # run in parsers-only mode
    if len(sys.argv) == 2 and sys.argv[1] == "parsers":
        main_parsers()
        exit(0)

    # otherwise, run in full mode
    channel = os.environ.get("BOT_CHANNEL", "#vellumotest")
    nickname = os.environ.get("BOT_NICKNAME", "vellubot")
    server = os.environ.get("BOT_SERVER", "irc.libera.chat")
    port = int(os.environ.get("BOT_PORT", "6667"))
    sasl_password = os.environ.get("BOT_SASL_PASSWORD", None)
    main_bot(channel, nickname, server, port, sasl_password)
