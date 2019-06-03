import aiohttp

from waterbutler.server import settings as settings


class WaterButlerPayload(aiohttp.payload.AsyncIterablePayload):

    def __init__(self, value, *args, **kwargs) -> None:
        super().__init__(value.iter_chunked(settings.CHUNK_SIZE), *args, **kwargs)
