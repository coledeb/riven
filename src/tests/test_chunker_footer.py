"""Reads that run from the last content chunk into the footer."""

from unittest.mock import MagicMock

import pytest

from program.services.streaming import chunker
from program.services.streaming.chunker import Chunker

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def no_cache_notifier(monkeypatch):
    monkeypatch.setattr(chunker, "di", MagicMock())


@pytest.mark.parametrize(
    ("header", "footer", "size", "position"),
    [
        # Seen live (House S01E02): ValueError "Chunk end must be greater than
        # or equal to start" when the read reached a header's length into the footer
        (4 * MB, 128 * 1024, 154467027, 154142696),
        (1 * MB, 512 * 1024, 452723241, 452067377),
    ],
)
def test_read_into_the_footer_gets_contiguous_chunks(header, footer, size, position):
    c = Chunker(
        cache_key="k", chunk_size=MB, header_size=header, footer_size=footer, file_size=size
    )

    chunks = list(c.get_chunk_range(position=position, size=MB).chunks)

    assert chunks[0].start <= position
    assert chunks[-1].end == size - 1
    assert all(a.end + 1 == b.start for a, b in zip(chunks, chunks[1:], strict=False))
