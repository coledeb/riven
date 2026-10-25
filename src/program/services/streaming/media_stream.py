import trio
import trio_util
import pyfuse3
import httpx

from functools import cached_property
from contextlib import asynccontextmanager
from loguru import logger
from typing import Any, Literal
from http import HTTPStatus
from kink import di
from collections.abc import AsyncGenerator, AsyncIterator
from ordered_set import OrderedSet

from program.settings import settings_manager
from program.utils import benchmark
from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient

from .chunker import Chunk, ChunkCacheNotifier, ChunkRange, Chunker
from .config import Config
from .exceptions import (
    CacheDataNotFoundException,
    ChunksTooSlowException,
    EmptyDataException,
    FatalMediaStreamException,
    ByteLengthMismatchException,
    RecoverableMediaStreamException,
    DebridServiceClosedConnectionException,
    DebridServiceException,
    DebridServiceForbiddenException,
    DebridServiceRangeNotSatisfiableException,
    DebridServiceUnableToConnectException,
    DebridServiceRateLimitedException,
    DebridServiceRefusedRangeRequestException,
    MediaStreamException,
    MediaStreamKilledException,
    DebridServiceLinkUnavailable,
)
from .file_metadata import FileMetadata
from .recent_reads import Read, RecentReads
from .session_statistics import SessionStatistics
from .slow_streams import stream_health
from .stream_connection import StreamConnection


# Providers that require proxy connections for streaming
PROXY_REQUIRED_PROVIDERS = {"alldebrid"}

# HTTP 429 handling. A CDN that rate limits us must get a real pause, shared by
# every connection to that host (the main stream plus header/footer/scan reads),
# or the parallel retries keep it rate limited until reads fail.
# The pause grows with each 429 in a row from a host (across connections, so a
# reader retrying over and over backs off too) and resets on a good answer.
RATE_LIMIT_BACKOFFS = [2.0, 4.0, 8.0, 16.0, 30.0]
RATE_LIMIT_MAX_WAIT = 30.0
# Waits before retrying a failed link refresh (provider API hiccup); stop
# retrying once this much time has passed, since the reader is waiting
REFRESH_RETRY_BACKOFFS = [2.0, 4.0]
REFRESH_RETRY_MAX_SECONDS = 10.0


def _record_cdn_failure(host: str) -> None:
    """Tell TorBox health a CDN server failed (only TorBox CDN hosts count)."""

    from program.services.downloaders.torbox_health import health

    health.record_cdn_failure(host)


def _http_error_summary(e: httpx.HTTPStatusError) -> str:
    """'HTTP 422 from api.torbox.app/v1/api/torrents/requestdl' (no query: keys)."""

    url = e.request.url
    return f"HTTP {e.response.status_code} from {url.host}{url.path}"


def _error_summary(e: BaseException) -> str:
    """One readable line for a known stream error, with what caused it."""

    message = f"{e.__class__.__name__}: {e}"
    cause = e.__cause__

    if isinstance(cause, httpx.HTTPStatusError):
        return f"{message} ({_http_error_summary(cause)})"

    if cause is not None and not isinstance(
        cause, (DebridServiceException, MediaStreamException)
    ):
        return f"{message} ({cause.__class__.__name__}: {cause})".replace("\n", " ")

    return message
_host_paused_until: dict[str, float] = {}
# 429s in a row from each host
_host_429_streak: dict[str, int] = {}


class _CallerError(Exception):
    """Carries an error raised by the caller of establish_connection past its retry handlers."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _body_read_error(error: Exception, provider: str) -> Exception:
    """A CDN that stalls, resets or drops the connection mid-body becomes a recoverable error,
    so the stream reconnects instead of dying with every later read failing."""

    # NetworkError covers resets (ReadError) and other socket failures.
    network_errors = (
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        httpx.TimeoutException,
    )
    dropped = isinstance(error, network_errors)

    if isinstance(error, ExceptionGroup):
        matched, rest = error.split(network_errors)
        dropped = matched is not None and rest is None

    if not dropped:
        return error

    closed = DebridServiceClosedConnectionException(provider=provider)
    closed.__cause__ = error

    return closed


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After header, if present."""

    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def _pause_host(host: str, seconds: float) -> None:
    seconds = min(max(seconds, 0.0), RATE_LIMIT_MAX_WAIT)
    until = trio.current_time() + seconds
    _host_paused_until[host] = max(_host_paused_until.get(host, 0.0), until)


async def _wait_for_host(host: str) -> None:
    remaining = _host_paused_until.get(host, 0.0) - trio.current_time()
    if remaining > 0:
        await trio.sleep(min(remaining, RATE_LIMIT_MAX_WAIT))


type ReadType = Literal[
    "header_scan",
    "footer_scan",
    "general_scan",
    "body_read",
    "footer_read",
    "cache_hit",
    "unknown",
]


class MediaStream:
    """
    Represents an active streaming session for a file.

    This class manages the streaming of media content, including handling
    connections, fetching data, and managing playback.
    """

    def __init__(
        self,
        *,
        fh: pyfuse3.FileHandleT,
        file_size: int,
        path: str,
        original_filename: str,
        nursery: trio.Nursery,
        provider: str,
        initial_url: str,
    ) -> None:
        stream_settings = settings_manager.settings.stream
        fs = settings_manager.settings.filesystem

        self.fh = fh
        self.nursery = nursery
        self.provider = provider
        self.recent_reads: RecentReads = RecentReads()
        # Body reads waiting for chunks; bumped counter wakes the stream loop.
        self._waiting_reads: list[ChunkRange] = []
        self._waiting_reads_changed: trio_util.AsyncValue[int] = trio_util.AsyncValue(0)
        self.is_streaming: trio_util.AsyncBool = trio_util.AsyncBool(False)
        self.is_killed: trio_util.AsyncBool = trio_util.AsyncBool(False)
        self._stream_error: trio_util.AsyncValue[Exception | None] = (
            trio_util.AsyncValue(None)
        )
        self.enable_tracing = settings_manager.settings.enable_stream_tracing

        # Store initial URL to avoid redundant unrestrict calls
        self.target_url: trio_util.AsyncValue[str] = trio_util.AsyncValue(initial_url)
        self._pushed_url: str | None = None

        self.config = Config(
            chunk_size=stream_settings.chunk_size_mb * 1024 * 1024,
            activity_timeout_seconds=stream_settings.activity_timeout_seconds,
            chunk_wait_timeout_seconds=stream_settings.chunk_wait_timeout_seconds,
            connect_timeout_seconds=stream_settings.connect_timeout_seconds,
        )

        self.session_statistics = SessionStatistics()

        self.file_metadata = FileMetadata(
            file_size=file_size,
            path=path,
            original_filename=original_filename,
        )

        self.chunker = Chunker(
            cache_key=self.file_metadata.original_filename,
            chunk_size=self.config.chunk_size,
            header_size=self.config.header_size,
            footer_size=self.footer_size,
            file_size=file_size,
        )

        if self.enable_tracing:
            logger.log(
                "STREAM",
                self.build_log_message(
                    f"Initialized stream with chunk size {self.config.chunk_size / (1024 * 1024):.2f} MB. "
                    f"file_size={self.file_metadata.file_size} bytes",
                ),
            )

        # Validate cache size
        # Cache needs to hold 10 chunks (10MiB) to avoid thrashing with concurrent reads
        min_cache_mb = (self.config.chunk_size * 10) // (1024 * 1024)

        if fs.cache_max_size_mb < min_cache_mb:
            logger.warning(
                f"Cache size ({fs.cache_max_size_mb}MB) is too small. "
                f"Minimum recommended: {min_cache_mb}MB. "
                "Cache thrashing may occur with concurrent reads, causing poor performance."
            )

        # Use proxy client if provider requires it
        if (
            provider in PROXY_REQUIRED_PROVIDERS
            and settings_manager.settings.downloaders.proxy_url
        ):
            self.async_client = di[ProxyClient]
        else:
            self.async_client = di[AsyncClient]

    def __repr__(self) -> str:
        return (
            f"<MediaStream[{self.provider}] "
            f"fh={self.fh} "
            f"path={self.file_metadata.path} "
            f"session_statistics={self.session_statistics} "
            f"last_read_timestamp={self.recent_reads.current_read.value.timestamp if self.recent_reads.current_read.value else None} "
            f"is_timed_out={self.is_timed_out} "
            f"is_streaming={self.is_streaming.value} "
            f"file_size={self.file_metadata.file_size} "
            ">"
        )

    @cached_property
    def footer_size(self) -> int:
        """An optimal footer size for scanning based on file size."""

        # Use a percentage-based approach for requesting the footer
        # using the file size to determine an appropriate range.

        min_footer_size = 1024 * 16  # Minimum footer size of 16KB
        max_footer_size = 10 * 1024 * 1024  # Maximum footer size of 10MB
        footer_percentage = 0.002  # 0.2% of file size

        percentage_size = int(self.file_metadata.file_size * footer_percentage)

        raw_footer_size = min(max(percentage_size, min_footer_size), max_footer_size)
        aligned_footer_size = (
            -(raw_footer_size // -self.config.block_size) * self.config.block_size
        )

        return aligned_footer_size

    @property
    def is_timed_out(self) -> bool:
        if not self.recent_reads.current_read.value:
            return False

        return (
            trio.current_time() - self.recent_reads.current_read.value.timestamp
            > self.config.activity_timeout_seconds
        )

    @asynccontextmanager
    async def stream_lifecycle(self) -> AsyncGenerator[None]:
        """Context manager for managing stream lifecycle."""

        try:
            self.is_streaming.value = True

            if self.enable_tracing:
                logger.log(
                    "STREAM",
                    self.build_log_message("Starting stream lifecycle"),
                )

            yield
        finally:
            self.is_streaming.value = False

            if self.enable_tracing:
                logger.log(
                    "STREAM",
                    self.build_log_message("Stream lifecycle ended"),
                )

    @asynccontextmanager
    async def manage_connection(
        self,
        *,
        position: int,
    ) -> AsyncIterator[StreamConnection]:
        """Context manager to handle connection lifecycle."""

        try:
            async with self.connect(position=position) as connection:
                yield connection
        except (
            EmptyDataException,
            DebridServiceRateLimitedException,
            DebridServiceRefusedRangeRequestException,
            DebridServiceClosedConnectionException,
        ) as e:
            logger.warning(
                self.build_log_message(
                    f"Stream connection failed: {_error_summary(e)}"
                )
            )

            raise RecoverableMediaStreamException(e) from e
        except (
            DebridServiceUnableToConnectException,
            DebridServiceForbiddenException,
            DebridServiceRangeNotSatisfiableException,
        ) as e:
            logger.warning(
                self.build_log_message(
                    f"Stream connection failed: {_error_summary(e)}"
                )
            )

            raise FatalMediaStreamException(e) from e

    async def run(
        self,
        position: int,
        *,
        task_status: trio.TaskStatus = trio.TASK_STATUS_IGNORED,
    ) -> None:
        has_started = False

        async with self.stream_lifecycle():
            async with trio_util.move_on_when(lambda: self.is_killed.wait_value(True)):
                attempt_count = 0
                max_attempts = 4

                seek_range: ChunkRange | None = None

                while True:
                    try:
                        async with self.manage_connection(
                            position=position
                        ) as connection:
                            if not has_started:
                                task_status.started()
                                has_started = True

                            # Reset attempt count on successful connection
                            attempt_count = 0

                            async with trio_util.move_on_when(
                                lambda connection=connection: trio_util.wait_any(
                                    # Reconnect the stream if a seek is requested
                                    lambda: connection.seek_required.wait_value(True),
                                    # Reconnect the stream if the target URL has been updated
                                    # by another request (e.g. a scan that refreshed the URL).
                                    lambda: self.target_url.wait_value(
                                        lambda url: (
                                            url != connection.response.request.url
                                        )
                                    ),
                                )
                            ):

                                async def _process_chunks(
                                    chunks: OrderedSet[Chunk],
                                ) -> None:
                                    if len(chunks) == 0:
                                        if self.enable_tracing:
                                            logger.log(
                                                "STREAM",
                                                self.build_log_message(
                                                    "Received no chunks to process; skipping."
                                                ),
                                            )

                                        return

                                    if self.enable_tracing:
                                        logger.log(
                                            "STREAM",
                                            self.build_log_message(
                                                f"Received chunks to process: {chunks}"
                                            ),
                                        )

                                    chunk_range_label = (
                                        f"{chunks[0].index}"
                                        if len(chunks) == 1
                                        else f"{chunks[0].index}-{chunks[-1].index}"
                                    )

                                    start_read_position = (
                                        connection.current_read_position
                                    )

                                    with benchmark(
                                        log=lambda duration, conn=connection, start=start_read_position: (
                                            logger.log(
                                                "STREAM",
                                                self.build_log_message(
                                                    f"Stream fetched {start}-{conn.current_read_position} "
                                                    f"({conn.current_read_position - start} bytes) "
                                                    f"in {duration}s."
                                                ),
                                            )
                                            if self.enable_tracing
                                            else None
                                        )
                                    ):
                                        for chunk in chunks:
                                            chunk_label = f"[{chunk.start}-{chunk.end}]"

                                            self._switch_to_pushed_url()

                                            fetch_started = trio.current_time()

                                            with benchmark(
                                                log=lambda duration, c=chunk: (
                                                    logger.log(
                                                        "STREAM",
                                                        self.build_log_message(
                                                            f"Fetching {c} took {duration}s"
                                                        ),
                                                    )
                                                    if self.enable_tracing
                                                    else None
                                                )
                                            ):
                                                data = await anext(connection.reader)

                                            stream_health.record_chunk(
                                                self.file_metadata.original_filename,
                                                path=self.file_metadata.path,
                                                file_size=self.file_metadata.file_size,
                                                url=self.target_url.value,
                                                size=len(data),
                                                seconds=trio.current_time()
                                                - fetch_started,
                                            )

                                            if data == b"":
                                                raise EmptyDataException(
                                                    range=(chunk.start, chunk.end)
                                                )

                                            with benchmark(
                                                log=lambda duration, label=chunk_label, range_label=chunk_range_label: (
                                                    logger.log(
                                                        "STREAM",
                                                        self.build_log_message(
                                                            f"Processing chunk(s) #{range_label} {label} took {duration}s"
                                                        ),
                                                    )
                                                    if self.enable_tracing
                                                    else None
                                                )
                                            ):
                                                connection.increment_sequential_chunks()

                                                await self._cache_chunk(
                                                    start=chunk.start,
                                                    data=data,
                                                )

                                                chunk.emit_cache_signal()

                                                connection.current_read_position += len(
                                                    data
                                                )

                                                self.session_statistics.bytes_transferred += len(
                                                    data
                                                )

                                if seek_range:
                                    seek_chunks = seek_range.uncached_chunks

                                    if (
                                        len(seek_chunks) > 0
                                        and seek_chunks[0].start
                                        == connection.current_read_position
                                    ):
                                        await _process_chunks(seek_chunks)

                                    seek_range = None

                                # Serve every body read that is waiting, lowest chunk first.
                                # Samba and kernel readahead keep several reads in flight on
                                # one file handle; each must get its chunks fetched, not only
                                # the newest one.
                                while True:
                                    chunk_range = self._next_waiting_range()

                                    if chunk_range is None:
                                        seen = self._waiting_reads_changed.value

                                        await self._waiting_reads_changed.wait_value(
                                            lambda v, seen=seen: v != seen
                                        )

                                        continue

                                    uncached_chunks = chunk_range.uncached_chunks

                                    if (
                                        uncached_chunks[0].start
                                        == connection.current_read_position
                                    ):
                                        await _process_chunks(uncached_chunks)

                                        continue

                                    # Streams only read forwards and can't skip data,
                                    # so any other position needs a new connection.
                                    if self.enable_tracing:
                                        logger.log(
                                            "STREAM",
                                            self.build_log_message(
                                                f"Next waiting chunk {uncached_chunks[0].start} "
                                                f"is not at read position {connection.current_read_position} "
                                                f"for {self.file_metadata.path}. "
                                                f"Seeking to {uncached_chunks[0].start}/{self.file_metadata.file_size}."
                                            ),
                                        )

                                    connection.seek(chunk_range=chunk_range)

                                    break

                            position = connection.current_read_position
                            seek_range = connection.seek_range
                    except RecoverableMediaStreamException as e:
                        logger.warning(
                            self.build_log_message(
                                f"Recoverable error from stream: {e.original_exception}. Attempting to reconnect..."
                            )
                        )

                        should_retry = await self._retry_with_backoff(
                            attempt_count,
                            max_attempts,
                            [0.2, 0.5, 1.0],
                        )

                        if should_retry:
                            attempt_count += 1

                            continue
                        else:
                            self._stream_error.value = e.original_exception

                            break
                    except FatalMediaStreamException as e:
                        logger.error(
                            self.build_log_message(
                                f"Stream stopped: {_error_summary(e.original_exception)}"
                            )
                        )

                        self._stream_error.value = e.original_exception

                        break
                    except (DebridServiceException, MediaStreamException) as e:
                        # Known stream failures: one line, no traceback
                        logger.error(
                            self.build_log_message(
                                f"Stream stopped: {_error_summary(e)}"
                            )
                        )

                        self._stream_error.value = e

                        break
                    except Exception as e:
                        # Safely catch any other unexpected exceptions to avoid crashing the FUSE mount
                        logger.exception(
                            self.build_log_message(f"Unexpected error from stream: {e}")
                        )

                        self._stream_error.value = e

                        break

    @asynccontextmanager
    async def connect(self, *, position: int) -> AsyncGenerator[StreamConnection]:
        """Establish a streaming connection starting at the given byte offset, aligned to the closest chunk."""

        chunk_range = self.chunker.get_chunk_range(position=position)

        chunk_aligned_start = (
            chunk_range.uncached_chunks[0].start
            if len(chunk_range.uncached_chunks) > 0
            else max(self.config.header_size, chunk_range.first_chunk.start)
        )

        async with self.establish_connection(start=chunk_aligned_start) as response:
            stream_connection = StreamConnection(
                response=response,
                start_position=chunk_aligned_start,
                current_read_position=chunk_aligned_start,
                reader=response.aiter_raw(chunk_size=self.config.chunk_size),
            )

            if self.enable_tracing:
                logger.log(
                    "STREAM",
                    self.build_log_message(
                        f"{response.http_version} stream connection established "
                        f"from byte {chunk_aligned_start} / {self.file_metadata.file_size}."
                    ),
                )

            yield stream_connection

    async def close(self) -> None:
        """Immediately terminate the active stream."""

        # First wait for the stream to stop, then close the client
        if self.is_streaming.value:
            # If the file was streaming,
            # clear all chunk cache emitters to free up memory.
            di[ChunkCacheNotifier].clear_emitters(
                cache_key=self.file_metadata.original_filename
            )

            # Wait for the stream loop to close
            try:
                with trio.fail_after(5):
                    self.is_killed.value = True
                    await self.is_streaming.wait_value(False)
            except trio.TooSlowError:
                logger.warning(
                    self.build_log_message("Stream didn't stop within 5 seconds")
                )

        if self.enable_tracing:
            logger.log(
                "STREAM",
                self.build_log_message(
                    f"Ended stream for {self.file_metadata.path} fh={self.fh} "
                    f"after transferring {self.session_statistics.bytes_transferred / (1024 * 1024):.2f}MB "
                    f"in {self.session_statistics.total_session_connections} connections."
                ),
            )

    async def scan(self, read_position: int, size: int) -> bytes:
        """Fetch a one-off range of data for scanning purposes."""

        data = await self._fetch_discrete_byte_range(
            start=read_position,
            size=size,
            should_cache=False,
        )

        return data[:size]

    async def scan_header(self, read_position: int, size: int) -> bytes:
        """Scans the start of the media file for header data."""

        data = await self._fetch_discrete_byte_range(
            start=self.chunker.header_chunk.start,
            size=self.chunker.header_chunk.size,
        )

        self.chunker.header_chunk.emit_cache_signal()

        return data[read_position : read_position + size]

    async def scan_footer(self, read_position: int, size: int) -> bytes:
        """
        Scans the end of the media file for footer data.

        This "over-fetches" for the individual request,
        but multiple footer requests tend to be made to retrieve more data later,
        so this ends up being more efficient than making multiple small requests.
        """

        footer_chunk = self.chunker.footer_chunk

        data = await self._fetch_discrete_byte_range(
            start=footer_chunk.start,
            size=footer_chunk.size,
        )

        self.chunker.footer_chunk.emit_cache_signal()

        slice_offset = read_position - footer_chunk.start

        return data[slice_offset : slice_offset + size]

    @asynccontextmanager
    async def capture_stream_errors(self) -> AsyncIterator[None]:
        """Context manager to capture and log stream errors."""

        # Handle the read request whilst monitoring for stream kill signals, and errors.
        # This allows us to gracefully handle stream termination and propagate errors,
        # even during the middle of a read operation.
        async with trio_util.move_on_when(
            lambda: trio_util.wait_any(
                lambda: self.is_killed.wait_value(True),
                lambda: self._stream_error.wait_value(lambda v: v is not None),
            )
        ):
            yield

        if self.is_killed.value:
            raise MediaStreamKilledException

        if self._stream_error.value:
            raise self._stream_error.value from None

    @asynccontextmanager
    async def read_lifecycle(self, chunk_range: ChunkRange) -> AsyncIterator[ReadType]:
        """Context manager for managing read lifecycle."""

        try:
            read_type = await self._detect_read_type(
                chunk_range=chunk_range,
            )

            # Start the stream and wait for a connection before progressing with a body read.
            # This MUST be done before assigning a value to current_read,
            # or else the stream will not receive the value.
            if read_type == "body_read" and not self.is_streaming.value:
                with trio.fail_after(self.config.connect_timeout_seconds):
                    try:
                        await self.nursery.start(self.run, chunk_range.position)
                    except RuntimeError:
                        # run() stores a failed first connection in _stream_error and
                        # returns without calling started(); raise the real error.
                        if self._stream_error.value:
                            raise self._stream_error.value from None

                        # Closed while still connecting (e.g. the idle-stream
                        # cleanup during a long 429 wait)
                        if self.is_killed.value:
                            raise MediaStreamKilledException from None

                        raise

            self.recent_reads.current_read.value = Read(
                chunk_range=chunk_range,
                read_type=read_type,
            )

            yield read_type
        finally:
            self.recent_reads.previous_read.value = self.recent_reads.current_read.value

    async def read(
        self,
        *,
        request_start: int,
        request_end: int,
        request_size: int,
    ) -> bytes:
        """Handles incoming read requests from the VFS."""

        read_range = self.chunker.get_chunk_range(
            position=request_start,
            size=request_size,
        )

        async with self.capture_stream_errors():
            async with self.read_lifecycle(chunk_range=read_range) as read_type:
                if self.enable_tracing:
                    logger.log(
                        "STREAM",
                        self.build_log_message(
                            f"Performing {read_type} for [{request_start}-{request_end}]"
                        ),
                    )

                match read_type:
                    case "cache_hit":
                        return await self._read_cache(
                            start=request_start,
                            end=request_end,
                        )
                    case "header_scan":
                        return await self.scan_header(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "footer_scan" | "footer_read":
                        # Note: if the read type is footer_read, the footer cache chunk
                        # has likely expired and the player is nearing EOF.
                        # In this case, we will re-download the entire footer and serve the rest from cache.
                        #
                        # This can happen if the user's cache size is small,
                        # or during heavy scans with lots of competing streams.
                        return await self.scan_footer(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "general_scan":
                        return await self.scan(
                            read_position=request_start,
                            size=request_size,
                        )
                    case "body_read":
                        return await self.read_bytes(chunk_range=read_range)
                    case _:
                        # This should never happen due to prior validation
                        raise RuntimeError("Unknown read type")

    async def read_bytes(
        self,
        chunk_range: ChunkRange,
    ) -> bytes:
        """Read a specific number of bytes from the stream."""

        start, end = chunk_range.request_range

        self._waiting_reads.append(chunk_range)
        self._waiting_reads_changed.value += 1

        try:
            await self._wait_until_chunks_ready(chunk_range=chunk_range)
        finally:
            self._waiting_reads.remove(chunk_range)

        if self.enable_tracing:
            logger.log(
                "STREAM",
                self.build_log_message(
                    f"Found cache, attempting to read {start}-{end}"
                ),
            )

        cached_data = await self._read_cache(
            start=start,
            end=end,
        )

        if cached_data:
            if self.enable_tracing:
                logger.log(
                    "STREAM",
                    self.build_log_message(
                        f"Found data {start}-{end} ({len(cached_data)} bytes) from cache"
                    ),
                )

            return cached_data

        raise CacheDataNotFoundException(range=chunk_range.request_range)

    @asynccontextmanager
    async def establish_connection(
        self,
        start: int,
        *,
        end: int | None = None,
    ) -> AsyncGenerator[httpx.Response]:
        """Establish a streaming connection starting at the given byte offset."""

        if settings_manager.settings.enable_network_tracing:

            async def trace_log(event_name: str, info: Any):
                logger.log(
                    "NETWORK",
                    self.build_log_message(f"{event_name} - {info}"),
                )

            extensions = {"trace": trace_log}
        else:
            extensions = None

        headers = httpx.Headers(
            {
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Range": f"bytes={start}-{end or ''}",
            }
        )

        max_attempts = 4
        backoffs = [0.2, 0.5, 1.0]

        for attempt in range(max_attempts):
            # A fresh link the slow-stream check picked: use it for this
            # connection, not only once the old one starts returning data
            self._switch_to_pushed_url()

            try:
                host = httpx.URL(self.target_url.value).host
            except httpx.InvalidURL:
                host = ""  # handled as InvalidURL by the request below
            await _wait_for_host(host)

            try:
                async with self.async_client.stream(
                    method="GET",
                    url=self.target_url.value,
                    headers=headers,
                    extensions=extensions,
                ) as stream:
                    stream.raise_for_status()
                    _host_429_streak.pop(host, None)

                    content_length = stream.headers.get("Content-Length")

                    if end is not None:
                        range_bytes = end - start + 1
                    else:
                        range_bytes = self.file_metadata.file_size - start

                    if (
                        stream.status_code == HTTPStatus.OK
                        and content_length is not None
                        and int(content_length) > range_bytes
                    ):
                        # Server appears to be ignoring the range request and returning full content.
                        # This is incompatible with our stream, as it will start at the incorrect position.
                        logger.warning(
                            self.build_log_message(
                                "Server returned full content instead of range."
                            )
                        )

                        if await self._retry_with_backoff(
                            attempt,
                            max_attempts,
                            backoffs,
                        ):
                            continue

                        raise DebridServiceRefusedRangeRequestException(
                            provider=self.provider
                        )

                    self.session_statistics.total_session_connections += 1

                    try:
                        yield stream
                    except Exception as e:
                        # Raised by the caller while reading the body, not while
                        # connecting: don't retry here (a generator can't yield twice).
                        raise _CallerError(_body_read_error(e, self.provider)) from None

                    return
            except _CallerError as e:
                raise e.error from e.error.__cause__
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                logger.warning(self.build_log_message(_http_error_summary(e)))

                if status_code >= 500:
                    _record_cdn_failure(host)

                if status_code == HTTPStatus.FORBIDDEN:
                    # Forbidden - could be rate limiting or auth issue, don't refresh URL
                    logger.warning(
                        self.build_log_message(
                            f"HTTP 403 Forbidden - attempt {attempt + 1}"
                        ),
                    )

                    if await self._retry_with_backoff(
                        attempt,
                        max_attempts,
                        backoffs,
                    ):
                        continue

                    raise DebridServiceForbiddenException(provider=self.provider) from e
                elif status_code in (
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.NOT_FOUND,
                    HTTPStatus.GONE,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                ):
                    # File can't be found at this URL; try refreshing the URL once.
                    # TorBox's CDN answers an expired link with 400, not 403/410.
                    if attempt == 0:
                        has_fresh_url = await self._refresh_download_url()

                        if has_fresh_url:
                            logger.warning(
                                self.build_log_message(
                                    f"URL refresh after HTTP {status_code}"
                                )
                            )

                            if await self._retry_with_backoff(
                                attempt,
                                max_attempts,
                                backoffs,
                            ):
                                continue

                    raise DebridServiceUnableToConnectException(
                        provider=self.provider
                    ) from e
                elif status_code == HTTPStatus.RANGE_NOT_SATISFIABLE:
                    # Requested range not satisfiable; handled as EOF
                    raise DebridServiceRangeNotSatisfiableException(
                        provider=self.provider
                    ) from e
                elif status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    # Rate limited - pause every connection to this host
                    # (Retry-After if given, else 2/4/8 s), don't refresh URL
                    streak = _host_429_streak.get(host, 0) + 1
                    _host_429_streak[host] = streak
                    delay = _retry_after_seconds(e.response)
                    if delay is None:
                        delay = RATE_LIMIT_BACKOFFS[
                            min(streak - 1, len(RATE_LIMIT_BACKOFFS) - 1)
                        ]
                    _pause_host(host, delay)

                    logger.warning(
                        self.build_log_message(
                            f"HTTP 429 Rate Limited - attempt {attempt + 1}, "
                            f"pausing {host} for {min(delay, RATE_LIMIT_MAX_WAIT):.0f}s"
                        )
                    )

                    if attempt < max_attempts - 1:
                        continue

                    raise DebridServiceRateLimitedException(
                        provider=self.provider
                    ) from e
                else:
                    # Other unexpected status codes (already logged above)
                    raise DebridServiceException(
                        f"Unexpected HTTP {status_code} connecting to stream",
                        provider=self.provider,
                    ) from e
            except (
                httpx.ConnectError,
                httpx.InvalidURL,
            ) as e:
                if isinstance(e, httpx.ConnectError):
                    _record_cdn_failure(host)

                logger.warning(
                    self.build_log_message(
                        f"Encountered {e.__class__.__name__}: {e} (attempt {attempt + 1}/{max_attempts})"
                    )
                )

                if attempt == 0:
                    # On first exception, try refreshing the URL in case it's a connectivity issue
                    has_fresh_url = await self._refresh_download_url()

                    if has_fresh_url:
                        logger.warning(
                            self.build_log_message("URL refresh after timeout")
                        )

                if await self._retry_with_backoff(
                    attempt,
                    max_attempts,
                    backoffs,
                ):
                    continue

                raise DebridServiceUnableToConnectException(
                    provider=self.provider
                ) from e
            except (httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                # This can happen if the server closes the connection prematurely
                if not isinstance(e, httpx.PoolTimeout):  # ours, not the server's
                    _record_cdn_failure(host)

                logger.warning(
                    self.build_log_message(
                        f"{e.__class__.__name__} error (attempt {attempt + 1}/{max_attempts}): {e}"
                    ),
                )

                if isinstance(e, httpx.PoolTimeout):
                    # Pool timeout indicates all connections are in use
                    logger.warning(
                        self.build_log_message(
                            f"All connections are in use: {self.async_client._transport._pool}"  # type: ignore
                        )
                    )

                if await self._retry_with_backoff(
                    attempt,
                    max_attempts,
                    backoffs,
                ):
                    continue

                raise DebridServiceClosedConnectionException(
                    provider=self.provider
                ) from e
            except DebridServiceLinkUnavailable:
                raise
            except Exception as e:
                logger.exception(
                    self.build_log_message("Unexpected error connecting to stream")
                )

                raise DebridServiceException(
                    "Unexpected error connecting to stream",
                    provider=self.provider,
                ) from e

        raise DebridServiceException(
            "Unexpected error connecting to stream",
            provider=self.provider,
        )

    async def _detect_read_type(
        self,
        *,
        chunk_range: ChunkRange,
    ) -> ReadType:
        start, end = chunk_range.request_range
        size = chunk_range.size

        # First, attempt to detect if the requested range is already cached.
        # This uses a lightweight check, that just checks for existence,
        # rather than reading the actual data.
        is_request_fully_cached = await trio.to_thread.run_sync(
            lambda: self._check_cache(
                start=chunk_range.first_chunk.start,  # Align to start of chunk for cache check
                end=end,
            )
        )

        if is_request_fully_cached:
            return "cache_hit"

        if start < end <= self.config.header_size:
            return "header_scan"

        file_size = self.file_metadata.file_size

        if (
            (self.recent_reads.last_read_end or 0)
            < start - self.config.sequential_read_tolerance
        ) and file_size - self.footer_size <= start <= file_size:
            return "footer_scan"

        if (
            # This behaviour is seen during scanning
            # and captures large jumps in read position
            # generally observed when the player is reading the footer
            # for cues or metadata after initial playback start.
            #
            # Scans typically read less than a single block.
            self.recent_reads.last_read_end is not None
            and (
                abs(self.recent_reads.last_read_end - start)
                > self.config.scan_tolerance
            )
            and start != self.config.header_size
            and size < self.config.block_size
        ) or (
            # This behaviour is seen when seeking.
            # Playback has already begun, so the header has been served
            # for this file, but the scan happens on a new file handle
            # and is the first request to be made.
            start > self.config.header_size
            and self.recent_reads.last_read_end is None
        ):
            return "general_scan"

        if start < self.file_metadata.file_size - self.footer_size:
            return "body_read"

        return "footer_read"

    async def _fetch_discrete_byte_range(
        self,
        start: int,
        size: int,
        should_cache: bool = True,
    ) -> bytes:
        """
        Fetch a discrete range of data outside of the main stream.

        Used for fetching the header, footer, and one-off scans.
        """

        if start < 0:
            raise ValueError("Start must be non-negative")

        if size <= 0:
            raise ValueError("Size must be positive")

        async with self.establish_connection(
            start=start,
            end=start + size - 1,
        ) as response:
            data = await response.aread()

            self.session_statistics.bytes_transferred += len(data)

            verified_data = self._verify_scan_integrity((start, start + size), data)

            if should_cache:
                await self._cache_chunk(
                    start=start,
                    data=verified_data[:size],
                )

            return verified_data

    def _next_waiting_range(self) -> ChunkRange | None:
        """The waiting read whose first uncached chunk comes earliest in the file."""

        best: ChunkRange | None = None
        best_start = 0

        for chunk_range in self._waiting_reads:
            uncached_chunks = chunk_range.uncached_chunks

            if len(uncached_chunks) == 0:
                continue

            if best is None or uncached_chunks[0].start < best_start:
                best = chunk_range
                best_start = uncached_chunks[0].start

        return best

    async def _wait_until_chunks_ready(
        self,
        *,
        chunk_range: ChunkRange,
    ) -> None:
        """Wait until all the given chunks are cached."""

        try:
            with trio.fail_after(self.config.chunk_wait_timeout_seconds):
                await trio_util.wait_all(
                    *[
                        (lambda chunk=chunk: chunk.is_cached.wait_value(True))
                        for chunk in chunk_range.chunks
                    ]
                )
        except* trio.TooSlowError:
            if len(chunk_range.uncached_chunks) > 0:
                stream_health.record_stall(
                    self.file_metadata.original_filename,
                    path=self.file_metadata.path,
                    file_size=self.file_metadata.file_size,
                    url=self.target_url.value,
                )

                raise ChunksTooSlowException(
                    threshold=self.config.chunk_wait_timeout_seconds,
                    chunks=chunk_range.uncached_chunks,
                ) from None

    def _check_cache(self, *, start: int, end: int) -> bool:
        """Check if the given byte range is fully cached."""

        from .cache import Cache

        return di[Cache].has(
            cache_key=self.file_metadata.original_filename,
            start=start,
            end=end,
        )

    async def _read_cache(
        self,
        *,
        start: int,
        end: int,
    ) -> bytes:
        """Fetch the given byte range from the cache, if it exists."""

        from .cache import Cache

        return await di[Cache].get(
            cache_key=self.file_metadata.original_filename,
            start=start,
            end=end,
        )

    async def _cache_chunk(
        self,
        *,
        start: int,
        data: bytes,
    ) -> None:
        """Cache the given chunk of data."""

        from .cache import Cache

        await di[Cache].put(
            cache_key=self.file_metadata.original_filename,
            start=start,
            data=data,
        )

    def _switch_to_pushed_url(self) -> None:
        """Use a fresh link the slow-stream check picked for this file."""

        url = stream_health.pushed_url(self.file_metadata.original_filename)

        # Only once per pushed link: the stream may refresh it later on its own
        if not url or url == self._pushed_url:
            return

        self._pushed_url = url

        if url != self.target_url.value:
            logger.debug(self.build_log_message("Switching to a fresh download link"))
            self.target_url.value = url

    async def _refresh_download_url(self) -> bool:
        """
        Refresh download URL by unrestricting from provider.

        Updates the database with the fresh URL.

        Returns:
            True if successfully refreshed, False otherwise
        """

        from program.services.filesystem.vfs import VFSDatabase

        started = trio.current_time()

        for attempt in range(len(REFRESH_RETRY_BACKOFFS) + 1):
            if attempt:
                delay = REFRESH_RETRY_BACKOFFS[attempt - 1]

                if trio.current_time() - started + delay > REFRESH_RETRY_MAX_SECONDS:
                    break

                logger.debug(
                    self.build_log_message(
                        f"Link refresh failed, retrying in {delay:.0f}s"
                    )
                )
                await trio.sleep(delay)

            # Query database by original_filename and force unrestrict; off
            # the event loop, as the provider call can take seconds
            entry_info = await trio.to_thread.run_sync(
                lambda: di[VFSDatabase].get_entry_by_original_filename(
                    original_filename=self.file_metadata.original_filename,
                    force_resolve=True,
                )
            )

            if not entry_info:
                return False

            fresh_url = entry_info.url

            if fresh_url:
                if fresh_url == self.target_url.value:
                    return False

                if self.enable_tracing:
                    logger.log(
                        "STREAM",
                        self.build_log_message(
                            f"Refreshed URL for {self.file_metadata.original_filename}"
                        ),
                    )

                self.target_url.value = fresh_url

                return True

            if entry_info.link_gone:
                # Being re-sourced; asking again would blacklist another stream
                return False

            # Otherwise the provider call failed (API hiccup). Retries go
            # through its circuit breaker: they count as failures, and while
            # it is OPEN they fail at once without contacting the provider.

        return False

    async def _retry_with_backoff(
        self,
        attempt: int,
        max_attempts: int,
        backoffs: list[float],
    ) -> bool:
        """
        Common retry logic

        Returns:
            True if should retry, False if max attempts reached
        """
        if attempt < max_attempts - 1:
            await trio.sleep(backoffs[min(attempt, len(backoffs) - 1)])

            return True

        return False

    def _verify_scan_integrity(
        self,
        range: tuple[int, int],
        data: bytes,
    ) -> bytes:
        """
        Verify the integrity of the data read from the stream for scanning purposes.

        Args:
            range: The byte range that was requested
            data: The data read from the stream
        """

        if data == b"":
            raise EmptyDataException(range=range)

        start, end = range
        expected_length = end - start
        actual_length = len(data)

        if actual_length < expected_length:
            raise ByteLengthMismatchException(
                expected_length=expected_length,
                actual_length=actual_length,
                range=range,
            )

        return data

    def build_log_message(self, message: str) -> str:
        return (
            f"{message} [fh: {self.fh} | file={self.file_metadata.path.split('/')[-1]}]"
        )
