import logging
import time
import os
import json
from urllib import request, parse, error
from collections import deque
from threading import Thread, Event
from datetime import datetime


class StreamHandlerWithBuffer(logging.StreamHandler):
    def __init__(self, stream=None, buffer_time: float = 0.2, buffer_lines: int = 50, debug=False):
        super().__init__(stream)
        assert buffer_time >= 0 or buffer_lines >= 0, "At least one kind of buffer must be set"

        self.buffer_time = buffer_time
        self.buffer_lines = buffer_lines
        self.buffer = []
        self.debug = debug

        self._stop_event = Event()
        if self.buffer_time:
            watcher = Thread(target=self.watcher, daemon=True)
            watcher.start()

    def close(self) -> None:
        self._stop_event.set()

    def export(self):
        """Actual writing data out to the stream"""

        if self.debug:
            self.buffer.append(f'StreamHandlerWithBuffer flush: {datetime.now()}')

        msg = self.terminator.join(self.buffer)
        stream = self.stream
        # issue 35046: merged two stream.writes into one.
        stream.write(msg + self.terminator)
        self.flush()

        self.buffer.clear()

    def emit(self, record):
        """
        Emit a record.

        If a formatter is specified, it is used to format the record.
        The record is then written to the stream with a trailing newline.  If
        exception information is present, it is formatted using
        traceback.print_exception and appended to the stream.  If the stream
        has an 'encoding' attribute, it is used to determine how to do the
        output to the stream.
        """
        try:
            msg = self.format(record)
            self.buffer.append(msg)
            if self.buffer_lines and len(self.buffer) >= self.buffer_lines:
                self.export()

        except RecursionError:  # See issue 36272
            raise
        except Exception:
            self.handleError(record)

    def watcher(self):
        """
        If buffer_time is used, this method will flush the buffer
        after every buffer_time seconds has passed.
        """
        if self.debug:
            self.buffer.append(f'StreamHandlerWithBuffer watcher starts: {datetime.now()}')
        while not self._stop_event.is_set():
            time.sleep(self.buffer_time)
            if self.buffer:
                self.acquire()
                self.export()
                self.release()


class TelegramHandler(logging.Handler):
    def __init__(self, token='', unique_ids=None, env_token_key='', env_unique_ids_key='',
                 debug=False, check_interval=600, grouping_interval=0):
        super().__init__()

        # whether to send log message immediately when received or
        # group them by grouping_interval and send later
        self.grouping_interval = max(0, int(grouping_interval))
        self.push_interval = self.grouping_interval + 4
        if self.grouping_interval and check_interval <= self.push_interval:
            raise ValueError(f'"check_interval" is too small. Should be at least {2*self.push_interval}')

        if env_token_key:
            token = os.environ.get(env_token_key, None) or token
        if env_unique_ids_key:
            unique_ids = os.environ.get(env_unique_ids_key, None) or unique_ids

        self._unique_ids = []       # type: list[str]
        self.set_unique_ids(unique_ids)

        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.feedback = {x: {} for x in self._unique_ids}
        self.cache = {x: deque(maxlen=100) for x in self._unique_ids}

        # background thread resends the log if network error previously
        self.debug = debug
        self.check_interval = check_interval
        self._stop_event = Event()
        watcher_thread = Thread(target=self.watcher, daemon=True)
        watcher_thread.start()
        if self.grouping_interval:
            pusher_thread = Thread(target=self.interval_pusher, daemon=True)
            pusher_thread.start()

        # reduce sending duplicated log
        self.last_record = None
        self.dup_count = 0

    def format(self, record):
        txt = super().format(record) + getattr(record, 'remark', '')
        return parse.quote_plus(txt)

    def close(self) -> None:
        self._stop_event.set()

    def _get_full_url(self, unique_id: str, text: str):
        # remove name/label if presence
        unique_id = unique_id.split(':')[-1]

        # add message_thread_id if presence
        if '@' in unique_id:
            # group_id and topic index is specified
            chat_id, message_thread_id = unique_id.split('@')
            url = f'{self._url}?chat_id={chat_id}&message_thread_id={message_thread_id}&text={text}'
        else:
            # just chat_id only
            url = f'{self._url}?chat_id={unique_id}&text={text}'

        return url

    def set_bot_token(self, token: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    def set_unique_ids(self, ids):
        if not ids:
            self._unique_ids = []
        elif type(ids) is str:
            # str from environment variable
            self._unique_ids = [x.strip() for x in ids.split(';')]
        elif type(ids) is int:
            # from config, one value
            self._unique_ids = [str(ids)]
        else:
            raise TypeError(f'Expected str or int but got type: {type(ids)}')

    def _request(self, _id_, full_url):
        """Return True if success or 403, otherwise False"""
        try:
            with request.urlopen(full_url) as fi:
                data = fi.read()
            self.feedback[_id_] = json.loads(data.decode())
            return True

        except json.JSONDecodeError as e:
            self.feedback[_id_] = {'error': str(e), 'data': data}
            return True

        except error.HTTPError as e:
            if e.code == 403:
                # user blocked the bot
                logging.getLogger('logger_tt').error(e)
                return True
            if e.code == 429:
                logging.getLogger('logger_tt').info(e)
                time.sleep(1)
                return False
            else:
                logging.getLogger('logger_tt').info(e)
                return False

        except ConnectionResetError as e:
            logging.getLogger('logger_tt').info(e)
            return False
        except Exception as e:
            logging.getLogger('logger_tt').exception(e)
            return False

    def send(self):
        for _id_ in self._unique_ids:
            while self.cache[_id_]:
                record = self.cache[_id_][0]
                if isinstance(record, logging.LogRecord):
                    msg_out = self.format(record)
                else:
                    msg_out = record[1]

                full_url = self._get_full_url(_id_, msg_out)
                if self._request(_id_, full_url):
                    self.cache[_id_].popleft()
                else:
                    # resend later
                    break

    def msg_grouping(self):
        for _id_ in self._unique_ids:
            group = {}
            starting = 0
            while self.cache[_id_]:
                record = self.cache[_id_].popleft()

                if isinstance(record, logging.LogRecord):
                    sec_timestamp = int(record.created)
                    msg = self.format(record)
                else:
                    sec_timestamp, msg = record
                    group[sec_timestamp] = msg
                    continue

                if starting <= sec_timestamp < starting + self.grouping_interval:
                    group[starting].append(msg)
                else:
                    starting = sec_timestamp
                    group[starting] = []
                    group[starting].append(msg)

            for grp, item in group.items():
                # parse.quote_plus('\n') == %0A
                msg_out = '%0A'.join(item)
                self.cache[_id_].append((grp, msg_out))

    def _is_duplicated_record(self, record):
        if not self.last_record:
            self.last_record = record
            self.dup_count = 0
            return False

        for attr in 'msg name levelno pathname lineno args funcName'.split():
            if getattr(record, attr) != getattr(self.last_record, attr):
                return False
        else:
            return True

    def _cache_records(self, record):
        """cache msg in case of sending failure"""

        # redirect msg to appropriate cache
        if getattr(record, 'dest_name', ''):
            dest_id = next(filter(lambda x: x.startswith(f'{record.dest_name}:'), self._unique_ids), None)
            if dest_id:
                self.cache[dest_id].append(record)
            else:
                # do nothing
                pass
        else:
            for _id_ in self._unique_ids:
                self.cache[_id_].append(record)

    def emit(self, record):
        self.acquire()

        if self._is_duplicated_record(record):
            self.dup_count += 1

        elif self.dup_count:
            # changed to new record, no longer duplicated
            # send last msg, then send this time msg
            self.last_record.remark = f'\n (Message repeated {self.dup_count} times)'
            self._cache_records(self.last_record)
            self._cache_records(record)

            self.last_record = record
            self.dup_count = 0
            if not self.grouping_interval:
                self.send()
        else:
            # last sent record is not duplicated
            self._cache_records(record)
            self.last_record = record
            if not self.grouping_interval:
                self.send()

        self.release()

    def interval_pusher(self):
        if self.debug:
            logging.getLogger().debug(f'TelegramHandler interval_pusher starts: {datetime.now()}')

        while not self._stop_event.is_set():
            time.sleep(self.push_interval)
            if any(self.cache.values()):
                self.acquire()
                self.msg_grouping()
                self.send()
                self.release()

    def watcher(self):
        """
        This method will resend the failed messages if they haven't been sent in emit
        """
        if self.debug:
            logging.getLogger().debug(f'TelegramHandler watcher starts: {datetime.now()}')

        while not self._stop_event.is_set():
            time.sleep(self.check_interval)
            if any(self.cache.values()) and not self.grouping_interval:
                if self.debug:
                    logging.getLogger().debug(f'TelegramHandler found unsent messages: {datetime.now()}')
                self.acquire()
                self.send()
                self.release()
            elif self.dup_count > 1:
                if self.debug:
                    logging.getLogger().debug(f'TelegramHandler watcher emit duplicated msg with: {datetime.now()}')
                self.acquire()
                self.last_record.remark = f'\n (Message repeated {self.dup_count} times)'
                self._cache_records(self.last_record)
                self.send()
                self.dup_count = 0
                self.release()
