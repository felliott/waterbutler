import abc
import asyncio


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

    def __init__(self, readable: SimpleStreamWrapper, writers=None: dict):
        super().__init__()
        assert readable.hasattr('read')
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

    def __init__(self, *readables: List<SimpleStreamWrappers>):
        super().__init__()

        self._size = 0
        self._size += sum(x.size for x in readables)

        self._readables = []
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

    def __init__(self, readable: SimpleStreamWrapper, cutoff):
        super().__init__()

        assert readable.hasattr('read')
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


class StringStream(SimpleStreamWrapper):
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

        chunk = data[0:n-1]
        data = data[n:]
        if len(data) == 0:
            self._at_eof = True

        return chunk


class EmptyStream(SimpleStreamWrapper):
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
