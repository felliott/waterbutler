import logging

import aiohttp

from waterbutler.server import settings as settings

logger = logging.getLogger(__name__)


class WaterButlerPayload(aiohttp.payload.AsyncIterablePayload):

    def __init__(self, value, *args, **kwargs) -> None:
        logger.info('[][][] Gessblat! type:({}) value:({})'.format(type(value), value))
        super().__init__(value.iter_chunked(settings.CHUNK_SIZE), *args, **kwargs)
