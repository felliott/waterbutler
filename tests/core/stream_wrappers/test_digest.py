import hashlib

import pytest

from waterbutler.core.streams import HashStreamWriter
from waterbutler.core.stream_wrappers import StringStreamWrapper, DigestStreamWrapper


class TestDigestStreamWrapper:

    @pytest.mark.asyncio
    async def test_basic_digest(self):
        stream = StringStreamWrapper('sleepy')
        sha = 'e74ca13b4c0a61dcf7746ff71c1238e2cd1b5026838c8b3c5e147595aa627025'
        digest_stream = DigestStreamWrapper(stream, {'sha256': HashStreamWriter(hashlib.sha256)})

        assert digest_stream.size() == 6

        result = await digest_stream.read()

        assert result == b'sleepy'
        assert digest_stream.writers['sha256'].hexdigest == sha
        assert digest_stream.size() == 6  # hasn't changed

    @pytest.mark.asyncio
    async def test_chunk_digest(self):

        first_sha = '33f78340ca9ef1ed28ea0e34db93ccefbdd5a954c799febc026a1438d668c66d'
        second_sha = '0afa5bb5d5f1787945e555e91474675beb6f34dd9a490423c4d0460e4d454885'
        full_sha = 'e74ca13b4c0a61dcf7746ff71c1238e2cd1b5026838c8b3c5e147595aa627025'

        stream = StringStreamWrapper('sleepy')
        digest_stream = DigestStreamWrapper(stream, {'sha256': HashStreamWriter(hashlib.sha256)})

        assert digest_stream.size() == 6

        first_part = await digest_stream.read(3)
        assert first_part == b'sle'
        assert digest_stream.writers['sha256'].hexdigest == first_sha

        second_part = await digest_stream.read(2)
        assert second_part == b'ep'
        assert digest_stream.writers['sha256'].hexdigest == second_sha

        last_part = await digest_stream.read()
        assert last_part == b'y'
        assert digest_stream.writers['sha256'].hexdigest == full_sha

        empty_part = await digest_stream.read()
        assert empty_part == b''
        assert digest_stream.writers['sha256'].hexdigest == full_sha

        assert digest_stream.size() == 6  # hasn't changed
