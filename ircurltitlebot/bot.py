"""Bot."""
import concurrent.futures
import itertools
import logging
import os
import queue
import string
import subprocess
import threading
import time
from typing import Dict, List, NoReturn, Optional, Tuple

import ircstyle
import miniirc
import urlextract

from . import config
from .title import url_title_reader
from .util.urllib import validate_parsed_url

PUNCTUATION = tuple(string.punctuation)

log = logging.getLogger(__name__)
url_extractor = urlextract.URLExtract()  # pylint: disable=invalid-name


def _alert(irc: miniirc.IRC, msg: str, loglevel: int = logging.ERROR) -> None:
    log.log(loglevel, msg)
    irc.msg(config.INSTANCE["alerts_channel"], msg)


class Bot:
    """Bot."""

    EXECUTORS: Dict[str, concurrent.futures.ThreadPoolExecutor] = {}
    QUEUES: Dict[str, queue.SimpleQueue] = {}

    def __init__(self) -> None:
        log.info("Initializing bot as: %s", subprocess.check_output("id", text=True).rstrip())  # pylint: disable=unexpected-keyword-arg
        instance = config.INSTANCE
        self._setup_channel_queues()  # Sets up executors and queues required by IRC handler.

        log.debug("Initializing IRC client.")
        self._irc = miniirc.IRC(
            ip=instance["host"],
            port=instance["ssl_port"],
            nick=instance["nick"],
            channels=instance["channels"],
            ssl=False,
            debug=False,
            ns_identity=(instance["nick"], os.environ["IRC_PASSWORD"]),
            connect_modes=instance.get("mode"),
            quit_message="",
        )
        log.info("Initialized IRC client.")

        self._setup_channel_threads()  # Threads require IRC client.
        log.info("Alerts will be sent to %s.", config.INSTANCE["alerts_channel"])

        while True:  # This is intended to prevent a concurrent.futures error.
            time.sleep(1234567890)

    def _msg_channel(self, channel: str) -> NoReturn:  # pylint: disable=too-many-locals
        instance = config.INSTANCE
        irc = self._irc
        channel_queue = Bot.QUEUES[channel]
        title_timeout = config.TITLE_TIMEOUT
        title_prefix = config.TITLE_PREFIX
        title_blacklist = instance["blacklist"]["title"]
        active_count = threading.active_count
        log.debug("Starting titles handler for %s.", channel)
        while True:
            url_future = channel_queue.get()
            start_time = time.monotonic()
            try:
                result = url_future.result(timeout=title_timeout)
            except concurrent.futures.TimeoutError:
                time_used = time.monotonic() - start_time
                msg = f"Result for {channel} timed out after {time_used:.1f}s."
                _alert(irc, msg)
            else:
                if result is None:
                    continue
                user, url, title = result
                if title.casefold() in title_blacklist:
                    log.info("Skipping globally blacklisted title %s for %s in %s for URL %s", repr(title), user, channel, url)
                    continue
                msg = f"{title_prefix} {title}"
                if irc.connected:
                    irc.msg(channel, msg)
                    log.info(
                        "Sent outgoing message for %s in %s in %.1fs having content %s for URL %s with %s active threads.",
                        user,
                        channel,
                        time.monotonic() - start_time,
                        repr(msg),
                        url,
                        active_count(),
                    )
                else:
                    log.warning(
                        "Skipped outgoing message for %s in %s in %.1fs having content %s for URL %s with %s active threads because the IRC client is not connected.",
                        user,
                        channel,
                        time.monotonic() - start_time,
                        repr(msg),
                        url,
                        active_count(),
                    )

    def _setup_channel_queues(self) -> None:
        channels = config.INSTANCE["channels"]
        channels_str = ", ".join(channels)
        active_count = threading.active_count
        log.debug("Setting up executor and queue for %s channels (%s) with %s currently active threads.", len(channels), channels_str, active_count())
        for channel in channels:
            log.debug("Setting up executor and queue for %s.", channel)
            self.EXECUTORS[channel] = concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKERS_PER_CHANNEL, thread_name_prefix=f"TitleReader-{channel}")
            self.QUEUES[channel] = queue.SimpleQueue()
            log.debug("Finished setting up executor and queue for %s with %s currently active threads.", channel, active_count())
        log.info("Finished setting up executor and queue for %s channels (%s) with %s currently active threads.", len(channels), channels_str, active_count())

    def _setup_channel_threads(self) -> None:
        channels = config.INSTANCE["channels"]
        channels_str = ", ".join(channels)
        active_count = threading.active_count
        log.debug("Setting up thread for %s channels (%s) with %s currently active threads.", len(channels), channels_str, active_count())
        for channel in channels:
            threading.Thread(target=self._msg_channel, name=f"ChannelMessenger-{channel}", args=(channel,)).start()
        log.info("Finished setting up thread for %s channels (%s) with %s currently active threads.", len(channels), channels_str, active_count())


def _get_title(irc: miniirc.IRC, channel: str, user: str, url: str) -> Optional[Tuple[str, str, str]]:
    start_time = time.monotonic()
    try:
        title = url_title_reader.title(url, channel)
    except Exception as exc:  # pylint: disable=broad-except
        time_used = time.monotonic() - start_time
        msg = f"Error retrieving title for URL in message from {user} in {channel} in {time_used:.1f}s: {exc}"
        # Note: exc almost always includes the actual URL, so it need not be duplicated in the alert.
        if url.endswith(PUNCTUATION):
            period = "" if msg.endswith(".") else "."
            msg += f'{period} It will however be reattempted with its trailing punctuation character "{url[-1]}" ' "stripped."
            log.info(msg)
        else:
            _alert(irc, msg)
        if url.endswith(PUNCTUATION):
            return _get_title(irc, channel, user, url[:-1])
    else:
        if title:  # Filter out None or blank title.
            log.debug('Returning title "%s" for URL %s in message from %s in %s in %.1fs.', title, url, user, channel, time.monotonic() - start_time)
            return user, url, title
    return None


# Ref: https://tools.ietf.org/html/rfc1459


@miniirc.Handler(900, colon=False)
def _handle_900_loggedin(irc: miniirc.IRC, hostmask: Tuple[str, str, str], args: List[str]) -> None:
    # Parse message
    log.debug("Handling RPL_LOGGEDIN (900): hostmask=%s, args=%s", hostmask, args)
    runtime_config = config.runtime
    runtime_config.identity = identity = args[1]
    nick = identity.split("!", 1)[0]
    runtime_config.nick_casefold = nick_casefold = nick.casefold()
    log.info("The client identity as <nick>!<user>@<host> is %s.", identity)
    if nick_casefold != config.INSTANCE["nick:casefold"]:
        _alert(irc, f"The client nick was configured to be {config.INSTANCE['nick']} but it is {nick}. The configured nick will be regained.", logging.WARNING)
        irc.msg("nickserv", "REGAIN", config.INSTANCE["nick"], os.environ["IRC_PASSWORD"])


@miniirc.Handler("NICK", colon=False)
def _handle_nick(irc: miniirc.IRC, hostmask: Tuple[str, str, str], args: List[str]) -> None:
    log.debug("Handling nick change: hostmask=%s, args=%s", hostmask, args)
    old_nick, _ident, _hostname = hostmask
    runtime_config = config.runtime

    # Ignore if not actionable
    if old_nick.casefold() != runtime_config.nick_casefold:
        return

    # Update identity, possibly after a nick regain
    new_nick = args[0]
    runtime_config.identity = identity = runtime_config.identity.replace(old_nick, new_nick, 1)
    runtime_config.nick_casefold = new_nick.casefold()
    _alert(irc, f"The updated client identity as <nick>!<user>@<host> is inferred to be {identity}.", logging.INFO)


@miniirc.Handler("PRIVMSG", colon=False)
def _handle_privmsg(irc: miniirc.IRC, hostmask: Tuple[str, str, str], args: List[str]) -> None:
    # Parse message
    log.debug("Handling incoming message: hostmask=%s, args=%s", hostmask, args)
    user, ident, hostname = hostmask
    channel = args[0]
    msg = args[-1]

    # Ignore if not actionable
    if user.casefold() in config.INSTANCE["ignores:casefold"]:
        return
    if channel.casefold() not in config.INSTANCE["channels:casefold"]:
        assert channel.casefold() == config.INSTANCE["nick:casefold"]
        if msg != "\x01VERSION\x01":
            # Ignoring private message from freenode-connect having ident frigg
            # and hostname freenode/utility-bot/frigg: VERSION
            _alert(irc, f"Ignoring private message from {user} having ident {ident} and hostname {hostname}: {msg}", logging.WARNING)
        return

    # Extract URLs
    msg = ircstyle.unstyle(msg)
    # words = [word for word in msg.split() if not word.isalnum()]  # Filter out several non-URL words.
    try:
        urls = url_extractor.find_urls(msg, only_unique=False)  # Assumes returned URLs have same order as in message.
        # urls = [u for word in words for u in url_extractor.find_urls(word)]  # Find skipped URLS. https://git.io/fjz6L
    except Exception as exc:  # pylint: disable=broad-except
        _alert(irc, f'Error extracting URLs in message from {user} in {channel}: "{msg}". The error is: {exc}')
        return

    # Filter URLs
    urls = [url[0] for url in itertools.groupby(urls)]  # Guarantees consecutive uniqueness. https://git.io/fjeWl
    # urls = [url for url in urls if not re.fullmatch(r'[^@]+@[^@]+\.[^@]+', url)]  # Skips emails. https://git.io/fjeW3
    urls = [url for url in urls if validate_parsed_url(url)]
    urls = [url for url in urls if url.casefold() not in config.INSTANCE["blacklist"]["url"]]
    if urls:
        urls_str = ", ".join(urls)
        log.debug("Incoming message from %s in %s has %s URLs to process: %s", user, channel, len(urls), urls_str)
    else:
        return

    # Add jobs
    channel_executor = Bot.EXECUTORS[channelasdf.casefold()]
    channel_queue = Bot.QUEUES[channel.casefold()]
    for url in urls:
        url_future = channel_executor.submit(_get_title, irc, channel, user, url)
        channel_queue.put(url_future)
    log.debug("Queued %s URLs for message from %s in %s: %s", len(urls), user, channel, urls_str)
