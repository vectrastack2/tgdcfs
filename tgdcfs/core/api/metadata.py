from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterable, List

from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.interface import IMetaDataRepository


class MetaDataApi:
    def __init__(self, metadata_repo: IMetaDataRepository):
        self.__metadata_repo = metadata_repo
        self.__batch_depth = 0
        self.__pending = False

    async def init(self) -> None:
        await self.__metadata_repo.init()

    def reset(self) -> None:
        self.__metadata_repo.metadata = TGFSMetadata(dir=TGFSDirectory.root_dir())

    @asynccontextmanager
    async def batch(self) -> AsyncIterator[None]:
        """Collapse the pushes of a bulk operation into a single write.

        The pinned-message backend rewrites the whole metadata blob on
        every push, so copying a folder file by file would rewrite it once
        per file. Nesting is allowed; only the outermost block writes.
        The write also happens when the block fails, because whatever part
        of the operation did succeed is real and has to be recorded.
        """
        self.__batch_depth += 1
        try:
            yield
        finally:
            self.__batch_depth -= 1
            if self.__batch_depth == 0 and self.__pending:
                self.__pending = False
                await self.__metadata_repo.push()

    async def push(self) -> None:
        if self.__batch_depth:
            self.__pending = True
            return
        await self.__metadata_repo.push()

    def begin_batch(self) -> None:
        """Collapse every push until :meth:`end_batch` into one write.

        The bracket form of :meth:`batch`, for callers that cannot wrap the
        operation in an ``async with``: an SFTP client removes a folder file
        by file, in requests of its own.
        """
        self.__metadata_repo.begin_batch()
        self.__batch_depth += 1

    async def end_batch(self) -> None:
        """Close the bracket opened by :meth:`begin_batch`; writes once.

        The write happens whatever the outcome of the operation was: the
        part that did go through has to be recorded.
        """
        if not self.__batch_depth:
            return
        self.__batch_depth -= 1
        if self.__batch_depth:
            return
        try:
            await self.__metadata_repo.end_batch()
        finally:
            if self.__pending:
                self.__pending = False
                await self.__metadata_repo.push()

    def get_root_directory(self) -> TGFSDirectory:
        return self.__metadata_repo.root()

    # -- file stats (``listing.stat_in_metadata``) -------------------------

    @property
    def push_writes_stats(self) -> bool:
        return self.__metadata_repo.push_writes_stats

    async def load_stats(self, directory: TGFSDirectory) -> bool:
        return await self.__metadata_repo.load_stats(directory)

    def stats_stored(self, directory: TGFSDirectory) -> bool:
        return self.__metadata_repo.stats_stored(directory)

    async def save_stats(
        self, directories: Iterable[TGFSDirectory], keep: bool
    ) -> None:
        await self.__metadata_repo.save_stats(directories, keep)

    def schedule_stats(self, directories: Iterable[TGFSDirectory]) -> bool:
        """Hand ``directories`` to the next metadata write instead of a
        stats write of their own, when the store can carry them along.

        Every removal marks its folder for a stats rewrite, and a stats
        commit of its own made removing a folder two commits per file.
        """
        return self.__metadata_repo.schedule_stats(directories)

    # -- metadata redundancy -------------------------------------------------

    async def heal(self) -> None:
        """Re-write the metadata stores that missed a write; a no-op while
        every store is up to date."""
        await self.__metadata_repo.heal()

    def stale_stores(self) -> List[str]:
        return self.__metadata_repo.stale_stores()

    async def close(self) -> None:
        await self.__metadata_repo.close()
