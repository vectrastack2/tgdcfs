from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional

from tgdcfs.core.model import (
    TGFSDirectory,
    TGFSFileDesc,
    TGFSFileRef,
    TGFSFileVersion,
    TGFSMetadata,
)
from tgdcfs.errors import MetadataNotInitialized
from tgdcfs.reqres import FileContent, SentFileMessage, UploadableFileMessage


@dataclass
class FDRepositoryResp:
    message_id: int
    fd: TGFSFileDesc
    # Mirror store key -> message id of the FD copy in that store.
    # Empty when redundancy is off.
    mirrors: Dict[str, int] = field(default_factory=dict)
    # Key of the store ``message_id`` was written to.
    store: Optional[str] = None


class IFileContentRepository(metaclass=ABCMeta):
    @abstractmethod
    async def save(self, file_msg: UploadableFileMessage) -> List[SentFileMessage]:
        pass

    def storage_name(self, name: str) -> str:
        """Name used on remote documents, including during replication."""
        return name

    @abstractmethod
    async def get(
        self,
        fv: TGFSFileVersion,
        begin: int,
        end: int,
        name: str,
        *,
        admit: bool = True,
    ) -> FileContent:
        """Bytes ``[begin, end]`` of the version, both ends inclusive.

        ``admit=False`` asks for the range without letting it open a new
        read-cache entry. An internal probe reads a handful of bytes and
        abandons the stream; admitting it would reserve the whole version
        and leave an entry behind that never receives a block. An entry
        that already exists is still read from and filled.
        """
        pass

    @abstractmethod
    async def update(self, message_id: int, buffer: bytes, name: str) -> int:
        pass

    async def stage(
        self, file_msg: UploadableFileMessage, version_id: str
    ) -> Optional[int]:
        """Take the whole upload into the local cache without touching a
        store (``write_ack: cache``); returns the bytes on disk, or
        ``None`` when the cache cannot take it and the caller has to
        upload the way it always did. Repositories without a cache
        return ``None``.
        """
        return None

    async def forget(self, version_ids: Iterable[str]) -> None:
        """The versions were deleted: drop whatever the repository kept
        for them locally (cache entries). Nothing to do by default."""
        return

    async def content_length(self, fv: TGFSFileVersion) -> int:
        """Logical size of the file as seen by the caller.

        Defaults to the stored on-wire size. The encryption decorator overrides
        this to subtract its per-chunk overhead and the file header so WebDAV
        clients see the plaintext length.
        """
        return fv.size


class IFDRepository(metaclass=ABCMeta):
    @abstractmethod
    async def save(
        self, fd: TGFSFileDesc, fr: Optional[TGFSFileRef] = None
    ) -> FDRepositoryResp:
        pass

    @abstractmethod
    async def get(
        self,
        fr: TGFSFileRef,
        include_all_versions: bool = False,
        validate: bool = True,
    ) -> TGFSFileDesc:
        """The descriptor of ``fr``. ``validate=False`` skips the check that
        its content messages still exist and trusts the recorded sizes."""
        pass


class IMetaDataRepository(metaclass=ABCMeta):
    def __init__(self):
        self.metadata: Optional[TGFSMetadata] = None
        # Whether the loaded tree carries stats; worked out on first ask.
        self._tree_has_stats: Optional[bool] = None

    async def init(self):
        self.metadata = await self.get()

    @abstractmethod
    async def push(self) -> None:
        pass

    async def push_async(self) -> None:
        """Write the tree without holding up the caller.

        Used for the write of a bulk operation, where the caller is a
        client waiting for its answer: a store that is slow (a hanging
        mirror, a rate-limited remote) must not stall the whole run. The
        default is the plain write, which is what a local or in-memory
        store already is.
        """
        await self.push()

    async def delete_file_refs(self, refs: Iterable[TGFSFileRef]) -> None:
        """Remove the references of a whole folder in one write.

        ``rm`` drops a single ref and writes; a recursive delete used to
        pay that per file, which is a commit per file on a tree store. The
        default keeps the per-ref call for a store that has nothing to
        fold.
        """
        for fr in refs:
            await fr.delete_async()

    async def push_full(self) -> None:
        """Write the whole current tree to this store, however much of it
        is already there.

        For a blob store this is what ``push`` already does. For a store
        that writes incrementally (``github_repo``) it re-syncs everything
        the incremental writes missed -- which is how a store that was
        unreachable for a while gets healed. The replicated layer uses it
        for mirrors and for healing; the default delegates to ``push``.
        """
        await self.push()

    @abstractmethod
    async def get(self) -> TGFSMetadata:
        pass

    def begin_batch(self) -> None:
        """Start a bulk operation; the ``push`` calls until ``end_batch``
        are folded into a single write where the store can do that.

        A client removing a folder hands its files over one by one; with
        one write per ``push`` a tree store commits once per file and a
        document store rewrites its whole document once per file. The
        default has nothing to fold.
        """

    async def end_batch(self) -> None:
        """End the bulk operation started by ``begin_batch``: write what
        is pending in a single write, also when the operation failed
        halfway -- whatever did change is real and has to be recorded."""

    async def heal(self) -> None:
        """Re-sync every store this repository knows to be behind.

        Only the replicated layer has several stores to reconcile; the
        default has nothing to do.
        """
        return

    def stale_stores(self) -> List[str]:
        """Keys of the stores that missed a write and await healing."""
        return []

    async def close(self) -> None:
        """Release backend resources; the default has nothing to release."""
        return

    def root(self) -> TGFSDirectory:
        if not self.metadata:
            raise MetadataNotInitialized
        return self.metadata.dir

    # -- file stats (``listing.stat_in_metadata``) -------------------------
    #
    # The defaults fit a backend that serializes the whole tree on every
    # push: the stats ride along on the file refs. A backend that keeps
    # them apart from the tree overrides all of this.

    # Whether ``push`` writes the stats of every file ref.
    push_writes_stats: bool = True

    async def load_stats(self, directory: TGFSDirectory) -> bool:
        """Put the stored stats of ``directory``'s files on their refs.

        Returns False when they could not be read; the caller must then
        not write that directory's stats, or it would drop the ones it
        could not see.
        """
        return True

    def stats_stored(self, directory: TGFSDirectory) -> bool:
        """Whether the store may hold stats for files in ``directory``."""
        if self._tree_has_stats is None:
            self._tree_has_stats = any(
                fr.stat is not None for fr in _iter_file_refs(self.root())
            )
        return self._tree_has_stats

    def schedule_stats(self, directories: Iterable[TGFSDirectory]) -> bool:
        """Mark ``directories`` for the next ``save_stats`` call.

        Returns whether the store took them; False means the caller has to
        call ``save_stats`` itself.
        """
        return False

    async def save_stats(
        self, directories: Iterable[TGFSDirectory], keep: bool
    ) -> None:
        """Write the stats of ``directories``; ``keep=False`` removes every
        stored stat instead (``stat_in_metadata`` turned off)."""
        if not keep:
            for fr in _iter_file_refs(self.root()):
                fr.stat = None
            self._tree_has_stats = False
        await self.push()


def _iter_file_refs(directory: TGFSDirectory) -> Iterator[TGFSFileRef]:
    yield from directory.find_files()
    for child in directory.find_dirs():
        yield from _iter_file_refs(child)
