# -*- coding: utf-8 -*-
#
# catalog - backend for the Commons Machinery metadata catalog
#
# Copyright 2014 Commons Machinery http://commonsmachinery.se/
#
# Authors: Artem Popov <artfwo@commonsmachinery.se>
#
# Distributed under an AGPLv3 license, please see LICENSE in the top dir.

from __future__ import absolute_import

from celery import Celery
from celery import Task
from celery.utils.dispatch import Signal
from catalog.log import SqliteLog, MongoDBLog
from catalog.store import MainStore, PublicStore

import redis
import os, time
import errno
import threading

import logging.config
import importlib

import logging
_log = logging.getLogger("catalog")

thread_local = threading.local()

APP_SETTINGS_FILENAME = "settings"
LOG_SETTINGS_FILENAME = "logging.ini"

if os.path.exists(LOG_SETTINGS_FILENAME):
    logging.config.fileConfig(LOG_SETTINGS_FILENAME)
else:
    _log.setLevel(logging.DEBUG)
    _log.addHandler(logging.StreamHandler())
    _log.warning('no %s, using default logging configuration', LOG_SETTINGS_FILENAME)


# Default configuration if there is no settings.py
class DefaultConfig:
    # Infrastructure paths and URLS
    BROKER_URL = os.getenv('CATALOG_BROKER_URL', 'amqp://guest@localhost:5672//')
    MONGODB_URL = os.getenv('CATALOG_MONGODB_URL', 'mongodb://localhost:27017/')
    REDIS_URL = os.getenv('CATALOG_REDIS_URL', 'localhost')

    # Used for sqlite and Redland local storage, typically only used in devevelopment
    DATA_DIR = os.getenv('CATALOG_DATA_DIR', './data')

    # Event log type: sqlite or mongodb
    EVENT_LOG_TYPE = os.getenv('CATALOG_EVENT_LOG_TYPE', 'sqlite')

    # Name of event log DB (when using MongoDB)
    EVENT_LOG_DB = 'events'

    # backend store type: hashes, postgres or memory
    BACKEND_STORE_TYPE = 'hashes'

    # hash store options
    BACKEND_STORE_HASH_TYPE = os.getenv('CATALOG_BACKEND_STORE_HASH_TYPE', 'bdb')
    BACKEND_STORE_DIR = os.getenv('CATALOG_BACKEND_STORE_DIR', 'data')

    # postgres store options
    BACKEND_STORE_DB_HOST = os.getenv('CATALOG_BACKEND_STORE_DB_HOST', 'localhost')
    BACKEND_STORE_DB_PORT = os.getenv('CATALOG_BACKEND_STORE_DB_PORT', '5432')
    BACKEND_STORE_DB_NAME = os.getenv('CATALOG_BACKEND_STORE_DB_NAME', 'catalog')
    BACKEND_STORE_DB_USER = os.getenv('CATALOG_BACKEND_STORE_DB_USER', 'postgres')
    BACKEND_STORE_DB_PASSWORD = os.getenv('CATALOG_BACKEND_STORE_DB_PASSWORD', '')

app = Celery('catalog', include=['catalog.tasks'])

# Attempt to read configuration from settings module
try:
    config = importlib.import_module(APP_SETTINGS_FILENAME)
except ImportError as e:
    _log.warning('no %s.py module, using default configuration', APP_SETTINGS_FILENAME)
    config = DefaultConfig

for _key, _value in config.__dict__.items():
    if not _key.startswith('_') and _key != 'os':
        _log.debug('Setting %s = %s', _key, _value)

# Use configuration provided by user
app.config_from_object(config)

# And set some technical stuff that the user shouldn't be allowed to touch
app.conf.update(
    CELERY_TASK_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT = ['json'],
    CELERY_RESULT_SERIALIZER='json',
    CELERY_RESULT_BACKEND = 'amqp',
    CELERY_TASK_RESULT_EXPIRES = 30,
    CELERY_TASK_RESULT_DURABLE = False,
    CELERY_DISABLE_RATE_LIMITS = True,
)

on_create_work          = Signal(providing_args=('timestamp', 'user_uri', 'work_uri', 'work_data'))
on_update_work          = Signal(providing_args=('timestamp', 'user_uri', 'work_uri', 'work_data'))
on_delete_work          = Signal(providing_args=('timestamp', 'user_uri', 'work_uri'))
on_create_work_source   = Signal(providing_args=('timestamp', 'user_uri', 'work_uri', 'source_uri', 'source_data'))
on_create_stock_source  = Signal(providing_args=('timestamp', 'user_uri', 'source_uri', 'source_data'))
on_update_source        = Signal(providing_args=('timestamp', 'user_uri', 'source_uri', 'source_data'))
on_delete_source        = Signal(providing_args=('timestamp', 'user_uri', 'source_uri'))
on_create_post          = Signal(providing_args=('timestamp', 'user_uri', 'work_uri', 'post_uri', 'post_data'))
on_delete_post          = Signal(providing_args=('timestamp', 'user_uri', 'post_uri'))


class LockedError(Exception):
    pass

class LockTimeoutError(Exception):
    pass


class FileLock(object):
    def __init__(self, id, timeout=15, lockdir = '.'):
        self._filename = os.path.join(lockdir, 'lock-%s' % id)
        self._timeout = timeout
        self._locked = False

    def __enter__(self):
        assert not self._locked

        pid = str(os.getpid())
        timeout = self._timeout

        # Attempt locking by creating a symlink.  This is an atomic
        # operation that will succeed only if the link doesn't already
        # exist.  Use the PID as the "target file" to provide info
        # about who holds the link

        while True:
            try:
                os.symlink(pid, self._filename)
            except OSError, e:
                if e.errno == errno.EEXIST:
                    if timeout > 0:
                        time.sleep(1)
                        timeout -= 1
                    else:
                        raise LockTimeoutError("Timeout error while trying to lock access to work")
                else:
                    raise
            else:
                self._locked = True
                return

    def __exit__(self, *args):
        if self._locked:
            try:
                os.remove(self._filename)
            except OSError, e:
                if e.errno == errno.ENOENT:
                    _log.warning('warning: lock file unexpectedly removed')
                else:
                    raise
            self._locked = False


class RedisLock(object):
    def __init__(self, lock_db, id):
        self._key = "lock." + id
        self._conn = lock_db
        self._locked = False

    def __enter__(self):
        assert not self._locked

        pid = str(os.getpid())

        #  let locks expire after 60 seconds to avoid leaving stuff locked if things crash.
        if self._conn.setex(self._key, pid, 60):
            self._locked = True
            return
        else:
            raise LockedError(self._key)

    def __exit__(self, *args):
        if self._locked:
            result = self._conn.delete(self._key)
            if not result:
                _log.warning('warning: lock unexpectedly removed')
            self._locked = False


thread_local.main_store = MainStore("works", config)
thread_local.public_store = PublicStore("public", config)
thread_local.lock_db = redis.Redis(config.REDIS_URL)
if config.EVENT_LOG_TYPE == 'sqlite':
    thread_local.log = SqliteLog(config.DATA_DIR)
elif config.EVENT_LOG_TYPE == 'mongodb':
    thread_local.log = MongoDBLog(config.MONGODB_URL, config.EVENT_LOG_DB)
else:
    raise RuntimeError('invalid event log configuration: %s' % config.EVENT_LOG_TYPE)


class StoreTask(app.Task):
    abstract = True
    max_retries = 5
    _log = None

    @property
    def main_store(self):
        return thread_local.main_store

    @property
    def public_store(self):
        return thread_local.public_store

    @property
    def lock_db(self):
        return thread_local.lock_db

    @property
    def log(self):
        return thread_local.log


if __name__ == '__main__':
    app.start()