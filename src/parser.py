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
    """Convert weird tori datetime strings to tz-aware datetime objects."""

    helsinki_tz = pytz.timezone("Europe/Helsinki")

    try:
        if date_string == "minuutti sitten":
            return datetime.now(helsinki_tz) - timedelta(minutes=1)
        elif date_string.endswith("minuuttia sitten"):
            n_minutes = int(date_string.split(" ")[0])
            return datetime.now(helsinki_tz) - timedelta(minutes=n_minutes)
        elif date_string.endswith("tunti sitten"):
            return datetime.now(helsinki_tz) - timedelta(hours=1)
        elif date_string.endswith(" tuntia sitten"):
            n_hours = int(date_string.split(" ")[0])
            return datetime.now(helsinki_tz) - timedelta(hours=n_hours)
        elif date_string.endswith("päivä sitten"):
            return datetime.now(helsinki_tz) - timedelta(days=1)
        elif date_string.endswith(" päivää sitten"):
            n_days = int(date_string.split(" ")[0])
            return datetime.now(helsinki_tz) - timedelta(days=n_days)
        elif "päästä" in date_string:
            return datetime.now(helsinki_tz)
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

    cards = soup.select("article")

    items = []
    for card in cards:
        try:
            a_tag = card.select("a")[0]
            title = a_tag.contents[1]
            link = a_tag.attrs["href"]
            uid = a_tag.attrs["href"].split("/")[-1]
            tori_date = card.select("div.text-xs")[0].contents[1].contents[0]

            datetime_ = tori_date_to_datetime(tori_date.strip())
            items.append(
                {
                    "datetime": datetime_,
                    "link": link,
                    "title": title,
                    "uid": uid,
                }
            )
        except Exception:
            logger.exception(
                "Unexpected 'article' card structure when parsing tori feed."
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

    feeds = [
        "https://www.tori.fi/recommerce/forsale/search?product_category=2.93.3215.8368"
    ]
    check_interval = 60
    check_length = 360000
    filters = ["1070"]

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
