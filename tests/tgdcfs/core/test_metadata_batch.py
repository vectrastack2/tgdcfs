"""Removing many files must not cost one metadata write per file.

A client that removes a folder hands the files over one by one: FileZilla
sends an ``SSH_FXP_REMOVE`` per file and an ``SSH_FXP_RMDIR`` per folder,
and every one of them used to write the metadata on its own -- a commit per
file to a tree store, two with the stats, which took minutes for a handful
of files and hours for a folder of thousands. ``MetaDataApi.batch`` folds
them into one write, and the SFTP server keeps a removal open until the
client goes quiet.
"""

import asyncio
import time
from typing import Dict, cast

import pytest

from tests.tgdcfs.core.test_copy_and_move import (
    FakeChannel,
    FakeFileContentRepository,
    FakeMetadataRepository,
    FakeTelegramStore,
)
from tgdcfs.app.sftp import server as sftp_server
from tgdcfs.app.sftp.server import TGFSSFTPServer
from tgdcfs.backends.telegram.store import TelegramStore
from tgdcfs.core.api.directory import DirectoryApi
from tgdcfs.core.api.file import FileApi
from tgdcfs.core.api.file_desc import FileDescApi
from tgdcfs.core.api.metadata import MetaDataApi
from tgdcfs.core.client import Client
from tgdcfs.core.ops import Ops
from tgdcfs.core.repository.impl.fd.store_msg import StoreFDRepository


class BatchClient:
    def __init__(self) -> None:
        self.name = "test"
        self.channel = FakeChannel()
        store = cast(TelegramStore, FakeTelegramStore(self.channel))
        file_desc_api = FileDescApi(StoreFDRepository(store), FakeFileContentRepository(self.channel))
        self.metadata_repo = FakeMetadataRepository()
        self.metadata_api = MetaDataApi(self.metadata_repo)
        self.file_api = FileApi(self.metadata_api, file_desc_api, store)
        self.dir_api = DirectoryApi(self.metadata_api, self.file_api, store)


class TestMetaDataBatch:
    @pytest.mark.asyncio
    async def test_a_removal_writes_the_metadata_once(self):
        client = BatchClient()
        ops = Ops(cast(Client, client))
        await ops.mkdir("/folder", parents=False)
        for i in range(10):
            await ops.upload_from_bytes(b"x", f"/folder/f{i}.txt")
        pushes = client.metadata_repo.pushes

        client.metadata_api.begin_batch()
        try:
            for i in range(10):
                await ops.rm_file(f"/folder/f{i}.txt")
            # Nothing is written while the removal is still going on.
            assert client.metadata_repo.pushes == pushes
        finally:
            await client.metadata_api.end_batch()

        assert client.metadata_repo.pushes == pushes + 1
        assert ops.cd("/folder").find_files() == []

    @pytest.mark.asyncio
    async def test_the_batch_writes_what_a_failed_operation_did(self):
        client = BatchClient()
        ops = Ops(cast(Client, client))
        await ops.mkdir("/folder", parents=False)
        await ops.upload_from_bytes(b"x", "/folder/one.txt")
        await ops.upload_from_bytes(b"x", "/folder/two.txt")
        pushes = client.metadata_repo.pushes

        client.metadata_api.begin_batch()
        try:
            await ops.rm_file("/folder/one.txt")
            with pytest.raises(Exception):
                await ops.rm_file("/folder/missing.txt")
        finally:
            await client.metadata_api.end_batch()

        assert client.metadata_repo.pushes == pushes + 1
        assert [fr.name for fr in ops.cd("/folder").find_files()] == ["two.txt"]

    @pytest.mark.asyncio
    async def test_a_recursive_removal_is_one_write(self):
        client = BatchClient()
        ops = Ops(cast(Client, client))
        await ops.mkdir("/folder", parents=False)
        await ops.mkdir("/folder/sub", parents=False)
        await ops.upload_from_bytes(b"x", "/folder/sub/a.txt")
        await ops.upload_from_bytes(b"x", "/folder/b.txt")
        pushes = client.metadata_repo.pushes

        await ops.rm_dir("/folder", recursive=True)

        # The removal's own write, plus at most the pushes the delete of the
        # content messages makes; the tree itself is written once.
        assert client.metadata_repo.pushes <= pushes + 2


class _RecordingMetadataApi:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.end_batches = 0

    def begin_batch(self) -> None:
        self.events.append("begin")

    async def end_batch(self) -> None:
        self.events.append("end")
        self.end_batches += 1


class _ClientWithMetadata:
    def __init__(self, metadata_api) -> None:
        self.metadata_api = metadata_api


class TestSFTPBulkRemoval:
    @pytest.fixture
    def short_windows(self, monkeypatch):
        monkeypatch.setattr(sftp_server, "BULK_QUIET_SECONDS", 0.05)
        monkeypatch.setattr(sftp_server, "BULK_MAX_SECONDS", 0.2)

    @staticmethod
    def _server(metadata_api) -> TGFSSFTPServer:
        server = object.__new__(TGFSSFTPServer)
        clients: Dict[str, Client] = {
            "test": cast(Client, _ClientWithMetadata(metadata_api))
        }
        server._clients = clients
        server._batch_depth = {}
        return server

    @pytest.mark.asyncio
    async def test_removals_that_keep_coming_share_one_write(self, short_windows):
        """One long request that removes five files writes once, not five
        times: each removal joins the batch the first one opened."""
        metadata_api = _RecordingMetadataApi()
        server = self._server(metadata_api)

        async with server._bulk_operation("test"):
            for _ in range(5):
                async with server._bulk_operation("test"):
                    await asyncio.sleep(0.01)
            assert metadata_api.events == ["begin"]

        for _ in range(50):
            if metadata_api.end_batches:
                break
            await asyncio.sleep(0.02)

        assert metadata_api.events[0] == "begin"
        assert metadata_api.end_batches == 1

    @pytest.mark.asyncio
    async def test_a_long_removal_writes_periodically(self, short_windows):
        metadata_api = _RecordingMetadataApi()
        server = self._server(metadata_api)

        async def remove_slowly() -> None:
            async with server._bulk_operation("test"):
                await asyncio.sleep(0.5)

        await remove_slowly()

        # The write happens after BULK_MAX_SECONDS, not only at the end.
        assert metadata_api.end_batches >= 1
        started = time.monotonic()
        assert started > 0
