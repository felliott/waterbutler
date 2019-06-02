import abc
import uuid
import base64
from typing import List


class SimpleStreamWrapper(metaclass=abc.ABCMeta):
    """A wrapper class around an existing stream that supports teeing to multiple reader and writer
    objects.  Though it inherits from `asyncio.StreamReader` it does not implement/augment all of
    its methods.  Only ``read()`` implements the teeing behavior; ``readexactly``, ``readline``,
    and ``readuntil`` do not.

    Classes that inherit from `BaseStream` must implement a ``_read()`` method that reads ``size``
    bytes from its source and returns it.
    """

    def __init__(self):
        super().__init__()
        self._at_eof = False

    def __aiter__(self):
        return self

    # TODO: Add more note on `AsyncIterablePayload` and its `write()` method in aiohttp3
    # TODO: Improve the BaseStream with `aiohttp.streams.AsyncStreamReaderMixin`
    async def __anext__(self):
        try:
            chunk = await self.read()
        except EOFError:
            raise StopAsyncIteration
        if chunk == b'':
            raise StopAsyncIteration
        return chunk

    @abc.abstractproperty
    def size(self):
        pass

    def at_eof(self):
        return self._at_eof

    @abc.abstractproperty
    async def read(self):
        pass


class DigestStreamWrapper(SimpleStreamWrapper):

    def __init__(self, readable: SimpleStreamWrapper, writers: dict=None) -> None:
        super().__init__()
        assert hasattr(readable, 'read')
        self._readable = readable
        self.writers = writers

    def size(self):
        return self._readable.size()

    async def read(self, size=-1):
        data = await super()._readable.read(size)
        if data is not None:
            for writer in self.writers.values():
                writer.write(data)
        return data


class MultiStreamWrapper(SimpleStreamWrapper):
    """Concatenate a series of `SimpleStreamWrapper` objects into a single stream. Reads from the
    current stream until exhausted, then continues to the next, etc. Used to build streaming form
    data for <whutch?>.
    """

    def __init__(self, *readables: SimpleStreamWrapper) -> None:
        super().__init__()

        self._size = 0
        self._size += sum(x.size for x in readables)

        self._readables = []  # type: List[SimpleStreamWrapper]
        self._readables.extend(readables)

        self._current_readable = None
        if self._current_readable is None:
            self._cycle()

    def _cycle(self):
        try:
            self._current_readable = self._readables.pop(0)
        except IndexError:
            pass

    @property
    def size(self):
        return self._size

    async def read(self, n=-1):
        chunk = b''

        while self._current_readable and (len(chunk) < n or n == -1):
            if n == -1:
                chunk += await self._current_readable.read(-1)
            else:
                chunk += await self._current_readable.read(n - len(chunk))

            if self._current_readable.at_eof():
                self._cycle()

        return chunk


class CutoffStream(SimpleStreamWrapper):
    """A wrapper around an existing stream that terminates after pulling off the specified number
    of bytes.  Useful for segmenting an existing stream into parts suitable for chunked upload
    interfaces.

    This class only subclasses `asyncio.StreamReader` to take advantage of the `isinstance`-based
    stream-reading interface of aiohttp v0.18.2. It implements a ``read()`` method with the same
    signature as `StreamReader` that does the bookkeeping to know how many bytes to request from
    the stream attribute.

    :param stream: a stream object to wrap
    :param int cutoff: number of bytes to read before stopping
    """

    def __init__(self, readable: SimpleStreamWrapper, cutoff) -> None:
        super().__init__()

        assert hasattr(readable, 'read')
        self._readable = readable

        self._cutoff = cutoff
        self._thus_far = 0
        self._size = min(cutoff, readable.size)

    @property
    def size(self):
        """The lesser of the wrapped stream's size or the cutoff."""
        return self._size

    async def read(self, n=-1):
        """Read ``n`` bytes from the stream. ``n`` is a chunk size, not the full size of the
        stream.  If ``n`` is -1, read ``cutoff`` bytes.  If ``n`` is a positive integer, read
        that many bytes as long as the total number of bytes read so far does not exceed
        ``cutoff``.
        """
        if n < 0:
            return await self._readable.read(self._cutoff)

        n = min(n, self._cutoff - self._thus_far)

        chunk = b''
        while self.stream and (len(chunk) < n):
            subchunk = await self._readable.read(n - len(chunk))
            chunk += subchunk
            self._thus_far += len(subchunk)

        return chunk


class StringStreamWrapper(SimpleStreamWrapper):
    """A StringWrapper class for short(!) strings.  PLEASE DON'T USE THIS FOR LARGE STRINGS!
    A StringStream.read(-1) will read the *entire* string into memory.  All of the data passed in
    the constructor is saved in the class.
    """

    def __init__(self, data):
        if isinstance(data, str):
            data = data.encode('UTF-8')
        elif not isinstance(data, bytes):
            raise TypeError('Data must be either str or bytes, found {!r}'.format(type(data)))

        self._size = len(data)
        self._data = data
        self._at_eof = False

    @property
    def size(self):
        return self._size

    def at_eof(self):
        return self._at_eof

    async def read(self, n=-1):

        if n == -1:
            n = self.size

        # TODO: this kinda sucks, b/c it's mutating internal data. Is that okay?
        chunk = self._data[0:n - 1]
        self._data = self._data[n:]
        if len(self._data) == 0:
            self._at_eof = True

        return chunk


class EmptyStreamWrapper(SimpleStreamWrapper):
    """An empty stream with size 0 that returns nothing when read. Useful for representing
    empty folders when building zipfiles.
    """
    def __init__(self):
        self._at_eof = False
        pass

    @property
    def size(self):
        return 0

    def at_eof(self):
        return self._at_eof

    async def read(self, n=-1):
        self._at_eof = True
        return bytearray()

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


class Base64EncodeStream(SimpleStreamWrapper):

    @staticmethod
    def calculate_encoded_size(size):
        size = 4 * size / 3
        if size % 4:
            size += 4 - size % 4
        return int(size)

    def __init__(self, stream, **kwargs):
        self.extra = b''
        self.stream = stream
        if stream.size is None:
            self._size = None
        else:
            self._size = Base64EncodeStream.calculate_encoded_size(stream.size)

        super().__init__(**kwargs)

    @property
    def size(self):
        return self._size

    async def read(self, n=-1):
        if n < 0:
            return await super().read(n)

        nog = n
        padding = n % 3
        if padding:
            n += (3 - padding)

        chunk = self.extra + base64.b64encode(await self.stream.read(n))

        if len(chunk) <= nog:
            self.extra = b''
            return chunk

        chunk, self.extra = chunk[:nog], chunk[nog:]

        return chunk

    def at_eof(self):
        return len(self.extra) == 0 and self.stream.at_eof()


def make_multistream_from_json(data):
    """Takes a `dict` where the values are all strings or Streams and turns it into a MultiString
    of StringStreams for literal values and passes through the Streams.

    This class is very naive.  It's only used by the GitHub provider to turn a simple dict with the form:

    ```
    { 'encoding': 'base64', 'content': Base64EncodeStream}

    ```

    into::

      [
        StringStream('{'),
        StringStream('"encoding":"'),
        StringStream('base64'),
        StringStream('",'),
        StringStream('"content":"'),
        Base64EncodeStream,
        StringStream('",'),
        StringStream('"}'),
      ]

    """

    streams = [StringStreamWrapper('{')]
    for key, value in data.items():
        if not isinstance(value, SimpleStreamWrapper):
            value = StringStreamWrapper(value)
        streams.extend([StringStreamWrapper('"{}":"'.format(key)),
                        value,
                        StringStreamWrapper('",')])

    streams.extend([StringStreamWrapper('"}')])
    return MultiStreamWrapper(streams)


def make_formdata_multistream(data):
    """Make a MultiStream that represents the form-data encoded representation of the given data.

    child of MultiStream used to create stream friendly multipart form data requests.

    Usage::

        >>> stream = make_formdata_multistream(
        >>>     key1='value1',
        >>>     fieldName=(FileStream(...), 'fileName', 'Mime', 'encoding')
        >>> )

    Auto generates boundaries and properly concatenates them

    Passes
    Use FormDataStream.headers to get the proper headers to be included with requests
    Namely Content-Length, Content-Type

    :return: a `MultiStream` object
    """

    def _start_boundary(boundary):
        return StringStreamWrapper('--{}\r\n'.format(boundary))

    def _end_boundary(boundary):
        return StringStreamWrapper('--{}--\r\n'.format(boundary))

    def _make_header(name, disposition='form-data', additional_headers=None, **extra):
        additional_headers = additional_headers or {}
        header = 'Content-Disposition: {}; name="{}"'.format(disposition, name)

        header += ''.join([
            '; {}="{}"'.format(key, value)
            for key, value in extra.items() if value is not None
        ])

        additional = '\r\n'.join([
            '{}: {}'.format(key, value)
            for key, value in additional_headers.items() if value is not None
        ])

        header += '\r\n'

        if additional:
            header += additional
            header += '\r\n'

        return header + '\r\n'

    def _make_headers(boundary):
        """The headers required to make a proper multipart form request."""
        return {
            # 'Content-Length': str(size),
            'Content-Type': 'multipart/form-data; boundary={}'.format(boundary)
        }

    boundary = uuid.uuid4().hex

    streams = []
    for key, value in data.items():
        streams.extend([_start_boundary(boundary)])

        if isinstance(value, dict):
            header = _make_header(
                key,
                filename=value['name'],
                disposition=value.get('disposition', 'file'),
                additional_headers={
                    'Content-Type': value.get('mime', 'application/octet-stream'),
                    'Content-Transfer-Encoding': value.get('transcoding', 'binary'),
                }
            )
            streams.extend([
                StringStreamWrapper(header),
                value['stream'],
                StringStreamWrapper('\r\n')
            ])
        else:
            streams.extend([
                StringStreamWrapper(_make_header(key) + value + '\r\n')
            ])

    streams.extend([_end_boundary(boundary)])

    return MultiStreamWrapper(streams, headers=_make_headers(boundary))
