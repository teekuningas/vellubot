import os
import requests
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from irc.bot import SingleServerIRCBot
from threading import Thread

import logging

logging.basicConfig(level=logging.DEBUG)


FEEDS = [
    "https://bbs.io-tech.fi/forums/naeytoenohjaimet.74/index.rss",
    "https://bbs.io-tech.fi/forums/prosessorit-emolevyt-ja-muistit.73/index.rss",
]
CHECK_INTERVAL = 600
FILTERS = ["4070", "4080", "3090"]


def parse_rfc822(date_string):
    try:
        return datetime.strptime(date_string, "%a, %d %b %Y %H:%M:%S %Z")
    except ValueError:
        try:
            return datetime.strptime(date_string, "%d %b %Y %H:%M:%S %Z")
        except ValueError:
            return datetime.strptime(date_string, "%a, %d %b %Y %H:%M:%S %z")


def check_feed(feeds, filters, last_checked_time, seen):
    new_items = []
    for feed in feeds:
        response = requests.get(feed)
        soup = BeautifulSoup(response.content, "xml")
        items = soup.find_all("item")
        for item in items:
            # we are only interested in previously unseen items
            if item.guid.string in seen:
                continue

            # If filters present, check if the current item is ok
            if filters:
                passes = False
                for filter_str in filters:
                    if filter_str in item.title.string:
                        passes = True
                        break
                if not passes:
                    continue

            # only look at the recently updated posts ( the creation date was not available )
            if parse_rfc822(item.pubDate.string) > last_checked_time:
                seen.append(item.guid.string)
                new_items.append(item)

    return new_items, datetime.now(timezone.utc), seen


class MyBot(SingleServerIRCBot):
    def __init__(self, channel, nickname, server, port=6667):
        SingleServerIRCBot.__init__(self, [(server, port)], nickname, nickname)
        self.channel = channel

        self.feeds = FEEDS
        self.check_interval = CHECK_INTERVAL
        self.filters = FILTERS

        self.seen = []
        self.last_checked_time = datetime.now(timezone.utc) - timedelta(
            seconds=self.check_interval
        )

    def on_nicknameinuse(self, c, e):
        c.nick(c.get_nickname() + "_")

    def on_welcome(self, c, e):
        c.join(self.channel)

        # start the main loop
        self.start_check_feed()

    def on_pubmsg(self, c, e):
        """Handles interactive parts."""
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

    def start_check_feed(self):
        """The periodical main loop."""

        def loop_check():
            while True:
                # check if new interesting items
                new_items, self.last_checked_time, self.seen = check_feed(
                    self.feeds, self.filters, self.last_checked_time, self.seen
                )

                # if yes, msg to channel
                for item in new_items:
                    self.connection.privmsg(
                        self.channel, f"New item: {item.link.string}"
                    )

                time.sleep(self.check_interval)

        Thread(target=loop_check).start()


def main_bot(channel, nickname, server, port):
    """Starts the ircbot."""
    bot = MyBot(channel, nickname, server, port)
    bot.start()


def main_test():
    """Wraps the check_feed functionality for testing without the irc part."""

    feeds = FEEDS
    check_interval = CHECK_INTERVAL
    filters = FILTERS

    seen = []
    last_checked_time = datetime.now(timezone.utc) - timedelta(seconds=check_interval)

    while True:
        print("Checking at: " + str(datetime.now()))

        new_items, last_checked_time, seen = check_feed(
            feeds, filters, last_checked_time, seen
        )

        for item in new_items:
            print(f"New item: {item.link.string}")

        time.sleep(check_interval)


if __name__ == "__main__":
    channel = os.environ['BOT_CHANNEL']
    nickname = os.environ['BOT_NICKNAME']
    server = os.environ['BOT_SERVER']
    port = int(os.environ['BOT_PORT'])
    main_bot(channel, nickname, server, port)
