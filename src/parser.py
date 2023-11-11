import logging
import re
import pytz
import requests
import time
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple


logger = logging.getLogger("app")


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
    check_length: int,
    seen: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
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
                    # case-insensitive regexp filter
                    try:
                        if re.compile(filter_str).search(item["title"], re.IGNORECASE):
                            break
                    except Exception as exc:
                        logger.exception("Regular expression filter failed:")
                else:
                    continue

            # only look at the recently updated posts
            if item["datetime"] > datetime.now(timezone.utc) - timedelta(
                seconds=check_length
            ):
                seen.append(item["uid"])
                new_items.append(item)

    return new_items, seen


def main_parsers() -> None:
    """Run parser test app."""

    feeds = ["https://bbs.io-tech.fi/forums/naeytoenohjaimet.74/index.rss"]
    check_interval = 60
    check_length = 360000
    filters = ["3080 ?ti"]

    seen: List[str] = []

    while True:
        logger.info("Checking at: " + str(datetime.now()))

        try:
            new_items, seen = check_feeds(feeds, filters, check_length, seen)
            for item in new_items:
                logger.info(f"New item: {item['link']}")
        except Exception as exc:
            logger.exception("Checking the feeds failed.")

        time.sleep(check_interval)


if __name__ == "__main__":
    # run a parser test app
    main_parsers()
