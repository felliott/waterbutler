import asyncio
import logging

from waterbutler.core.streams.base import SimpleStreamWrapper

logger = logging.getLogger(__name__)


class ResponseStreamReader(SimpleStreamWrapper):

    def __init__(self, response, size=None, name=None):
        logger.info('??? making a RESPONSEstreamreader')
        # import traceback
        # traceback.print_stack()
        super().__init__()
        if 'Content-Length' in response.headers:
            self._size = int(response.headers['Content-Length'])
        else:
            self._size = size
        self._name = name
        self.response = response

    @property
    def partial(self):
        return self.response.status == 206

    @property
    def content_type(self):
        return self.response.headers.get('Content-Type', 'application/octet-stream')

    @property
    def content_range(self):
        return self.response.headers['Content-Range']

    @property
    def name(self):
        return self._name

    @property
    def size(self):
        return self._size

    async def read(self, size):
        logger.info('================================================================================')
        logger.info('???-S->>> _read, size:({})'.format(size))
        # import traceback
        # traceback.print_stack()
        chunk = await self.response.content.read(size)
        logger.info('???-S->>> _read, process chunk')
        if not chunk:
            logger.info('???-S->>> _read, finish,cleanup')
            self._at_eof = True
            await self.response.release()
            logger.info('???-S->>> _read, done cleaning')

        logger.info('???-S->>> _read, return chunk')
        return chunk


class RequestStreamReader(SimpleStreamWrapper):

    def __init__(self, request, inner):
        logger.info('???-Q->>> making a REQUESTstreamreader')
        logger.info('???-Q->>>   request.isa:({})  inner.isa:({})'.format(type(request), type(inner)))
        super().__init__()
        self.inner = inner
        self.request = request

    @property
    def size(self):
        return int(self.request.headers.get('Content-Length'))

    def at_eof(self):
        return self.inner.at_eof()

    async def read(self, size):
        logger.info('???-Q->>> ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
        # import traceback
        # traceback.print_stack()
        logger.info('???-Q->>> _read, size:({})'.format(size))
        if self.inner.at_eof():
            logger.info('???-Q->>> at_eof! all done, return empty byte')
            return b''

        if size < 0:
            logger.info('???-Q->>> size le zero. dispatch to inner.read')
            logger.info('???-Q->>> inner.read for negative size, isa:({})'.format(type(self.inner)))
            shma = await self.inner.read(size)
            logger.info('???-Q->>> shma is size:({})'.format(len(shma)))
            return shma

        try:
            logger.info('???-Q->>> inner.read for fixed size, isa:({})'.format(type(self.inner)))
            return await self.inner.read(size)
        except asyncio.IncompleteReadError as e:
            logger.info('???-Q->>> fuckoffsuperdone')
            return e.partial

        logger.info('???-Q->>> jfc actuallyfingreturingnotcool')
