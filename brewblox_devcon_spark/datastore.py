"""
Stores service-related data associated with objects.
"""


import asyncio
import json
import warnings
from abc import abstractmethod
from contextlib import contextmanager, suppress
from functools import wraps
from typing import List

from aiohttp import web
from brewblox_service import (brewblox_logger, couchdb, features, repeater,
                              strex)

from brewblox_devcon_spark import const
from brewblox_devcon_spark.twinkeydict import TwinKeyDict, TwinKeyError

LOGGER = brewblox_logger(__name__)


FLUSH_DELAY_S = 5
DB_NAME = 'spark-service'

SYS_OBJECTS = [
    {'keys': keys, 'data': {}}
    for keys in const.SYS_OBJECT_KEYS
]

BLOCK_DOC_FMT = '{id}-blocks-db'
CONFIG_DOC_FMT = '{id}-config-db'
SERVICE_DOC_FMT = '{name}-service-db'


def non_volatile(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if self.volatile:
            return
        return await func(self, *args, **kwargs)
    return wrapper


async def check_remote(app: web.Application):
    if app['config']['volatile']:
        return
    await couchdb.check_remote(app)


class FlushedStore(repeater.RepeaterFeature):

    def __init__(self, app: web.Application):
        super().__init__(app)
        self._volatile = app['config']['volatile']

        self._changed_event: asyncio.Event = None

        self._document: str = None
        self._rev: str = None

    def __str__(self):
        return f'<{type(self).__name__} for {DB_NAME}/{self._document}>'

    @property
    def volatile(self):
        return self._volatile

    @property
    def document(self):
        return self._document

    @document.setter
    def document(self, val):
        self._document = val

    @property
    def rev(self):
        return self._rev

    @rev.setter
    def rev(self, val):
        self._rev = val

    def set_changed(self):
        if self._changed_event:
            self._changed_event.set()

    async def before_shutdown(self, app: web.Application):
        await super().before_shutdown(app)
        await self.end()
        self._changed_event = None

    async def prepare(self):
        self._changed_event = asyncio.Event()
        if self._volatile:
            LOGGER.info(f'{self} is volatile (will not read/write datastore)')
            raise repeater.RepeaterCancelled()

    async def run(self):
        try:
            await self._changed_event.wait()
            await asyncio.sleep(FLUSH_DELAY_S)
            await self.write()
            self._changed_event.clear()

        except asyncio.CancelledError:
            LOGGER.debug(f'Writing data while closing {self}')
            if self._changed_event.is_set():
                await self.write()
            raise

        except Exception as ex:
            warnings.warn(f'{self} flush error {strex(ex)}')

    @abstractmethod
    async def write(self):
        """
        Must be implemented by child classes.
        FlushedStore handles how and when write() is called.
        """


class CouchDBBlockStore(FlushedStore, TwinKeyDict):
    """
    TwinKeyDict subclass to periodically flush contained objects to CouchDB.
    """

    def __init__(self, app: web.Application, defaults: List[dict] = None):
        FlushedStore.__init__(self, app)
        TwinKeyDict.__init__(self)
        self._defaults = defaults or []
        self._ready_event: asyncio.Event = None
        self.clear()  # inserts defaults

    async def startup(self, app: web.Application):
        await FlushedStore.startup(self, app)
        self._ready_event = asyncio.Event()

    async def shutdown(self, app: web.Application):
        await FlushedStore.shutdown(self, app)
        self._ready_event = None

    async def read(self, device_id: str):
        document = BLOCK_DOC_FMT.format(id=device_id)
        data = []

        try:
            self.rev = None
            self.document = None
            self._ready_event.clear()
            if not self.volatile:
                self.rev, data = await couchdb.read(self.app, DB_NAME, document, [])
                self.document = document
                LOGGER.info(f'{self} Read {len(data)} blocks. Rev = {self.rev}')

        except asyncio.CancelledError:  # pragma: no cover
            raise

        except Exception as ex:
            warnings.warn(f'{self} read error {strex(ex)}')

        finally:
            # Clear -> load from database -> merge defaults
            TwinKeyDict.clear(self)
            for obj in data:
                TwinKeyDict.__setitem__(self, obj['keys'], obj['data'])
            for obj in self._defaults:
                with suppress(TwinKeyError):
                    if obj['keys'] not in self:
                        self.__setitem__(obj['keys'], obj['data'])

            self._ready_event.set()

    @non_volatile
    async def write(self):
        await self._ready_event.wait()
        if self.rev is None or self.document is None:
            raise RuntimeError('Document or revision unknown - did read() fail?')
        data = [
            {'keys': keys, 'data': content}
            for keys, content in self.items()
        ]
        self.rev = await couchdb.write(self.app, DB_NAME, self.document, self.rev, data)
        LOGGER.info(f'{self} Saved {len(data)} block(s). Rev = {self.rev}')

    def __setitem__(self, keys, item):
        TwinKeyDict.__setitem__(self, keys, item)
        self.set_changed()

    def __delitem__(self, keys):
        TwinKeyDict.__delitem__(self, keys)
        self.set_changed()

    def clear(self):
        TwinKeyDict.clear(self)
        for obj in self._defaults:
            self.__setitem__(obj['keys'], obj['data'])


class CouchDBConfig(FlushedStore):
    """
    Database-backed configuration
    """

    def __init__(self, app: web.Application):
        FlushedStore.__init__(self, app)
        self._config: dict = {}
        self._ready_event: asyncio.Event = None

    async def startup(self, app: web.Application):
        await FlushedStore.startup(self, app)
        self._ready_event = asyncio.Event()

    async def shutdown(self, app: web.Application):
        await FlushedStore.shutdown(self, app)
        self._ready_event = None

    @contextmanager
    def open(self):
        before = json.dumps(self._config)
        yield self._config
        after = json.dumps(self._config)
        if before != after:
            self.set_changed()

    async def read(self, document: str):
        data = {}

        try:
            self.rev = None
            self.document = None
            self._ready_event.clear()
            if not self.volatile:
                self.rev, data = await couchdb.read(self.app, DB_NAME, document, {})
                self.document = document
                LOGGER.info(f'{self} Read {len(data)} setting(s). Rev = {self.rev}')

        except asyncio.CancelledError:  # pragma: no cover
            raise

        except Exception as ex:
            warnings.warn(f'{self} read error {strex(ex)}')

        finally:
            self._config = data
            self._ready_event.set()

    @non_volatile
    async def write(self):
        await self._ready_event.wait()
        if self.rev is None or self.document is None:
            raise RuntimeError('Document or revision unknown - did read() fail?')
        self.rev = await couchdb.write(self.app, DB_NAME, self.document, self.rev, self._config)
        LOGGER.info(f'{self} Saved {len(self._config)} settings. Rev = {self.rev}')


class CouchDBServiceStore(CouchDBConfig):

    async def read(self):
        name = self.app['config']['name']
        return await super().read(SERVICE_DOC_FMT.format(name=name))


# Deprecated
class CouchDBConfigStore(CouchDBConfig):

    async def read(self, device_id: str):
        return await super().read(CONFIG_DOC_FMT.format(id=device_id))


def setup(app: web.Application):
    features.add(app, CouchDBBlockStore(app, defaults=SYS_OBJECTS))
    features.add(app, CouchDBServiceStore(app))


def get_block_store(app: web.Application) -> CouchDBBlockStore:
    return features.get(app, CouchDBBlockStore)


def get_service_store(app: web.Application) -> CouchDBServiceStore:
    return features.get(app, CouchDBServiceStore)
