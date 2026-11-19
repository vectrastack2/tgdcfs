import asyncio
import base64
import bisect
import datetime
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, NamedTuple, Optional, Sequence, Set, cast

from github import Github, GithubException, GithubRetry, InputGitTreeElement
from github.ContentFile import ContentFile
from github.Repository import Repository

from tgdcfs.config import GithubRepoConfig, expand_path
from tgdcfs.core.model import FileStat, TGFSDirectory, TGFSFileRef, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.breaker import (
    FailFastRepository,
    StoreCircuitBreaker,
    probe_github_branch,
)
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.crypto.path_names import (
    PathNameEncryptionError,
    decrypt_path_name,
    encrypt_path_name,
    is_encrypted_path_name,
)
from tgdcfs.errors import MetadataNotInitialized, TechnicalError

from .dir_timestamps import DirTimestampStore, cache_file_name
from .gh_directory import GithubConfig, GithubDirectory
from .stats_file import STATS_COMMIT_PREFIX, STATS_FILE_NAME

logger = logging.getLogger(__name__)


class _TreeEntry(NamedTuple):
    """The part of a git tree entry the tree builder reads: a full storage
    path, ``"tree"`` or ``"blob"``, and the object's sha."""

    path: str
    type: str
    sha: str


# How often a stats commit is retried when the branch moved under it.
STATS_COMMIT_ATTEMPTS = 3
# How often the full-tree sync commit is retried when the branch moved.
SYNC_COMMIT_ATTEMPTS = 3


def _blob_sha(content: str) -> str:
    """The git blob sha of ``content``, as GitHub will report it."""
    data = content.encode()
    # Git's object id, not a security measure.
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


class GithubRepoMetadataRepository(IMetaDataRepository):
    def __init__(
        self,
        config: GithubRepoConfig,
        name_key: Optional[bytes] = None,
        repo: Optional[Repository] = None,
    ):
        super().__init__()
        self._stats_loads: dict[int, asyncio.Future[bool]] = {}
        # Folders whose stats are carried on the next full tree sync, see
        # ``schedule_stats``.
        self._stats_due: Set[int] = set()

        gh = Github(config.access_token, retry=GithubRetry(total=1))
        # Injectable for a store that speaks the PyGithub surface without
        # being GitHub (the GitLab tree mirror); the default client talks
        # to GitHub itself. The lazy flag keeps the resolution request out
        # of the constructor: the repository completes on its first API
        # call, which runs on a worker thread. An unreachable GitHub at
        # boot must surface as this store's read error -- so the
        # replicated layer fails over to a healthy mirror and the heal
        # worker catches this store up once it answers again -- never as a
        # minutes-long blocking call on the event loop before the layer
        # even exists. The fail-fast wrapper keeps it that way afterwards:
        # once a call ran into a dead connection, every further call of
        # this store is refused until a background probe finds the
        # service again, so one outage costs its connect timeouts once --
        # not once per listing, write, stats commit and heal.
        self._breaker: Optional[StoreCircuitBreaker] = None
        if repo is None:
            raw = gh.get_repo(config.repo, lazy=True)
            self._breaker = StoreCircuitBreaker(probe_github_branch(raw, config.commit))
            repo = cast(Repository, FailFastRepository(raw, self._breaker))

        self._timestamps = DirTimestampStore(
            repo=repo,
            branch=config.commit,
            path=expand_path(cache_file_name(config.repo, config.commit)),
        )

        self._ghc = GithubConfig(
            gh=gh,
            repo_name=config.repo,
            repo=repo,
            commit=config.commit,
            name_key=name_key,
            timestamps=self._timestamps,
            tree_fetch_concurrency=config.tree_fetch_concurrency,
        )

    # -- the shared tree -----------------------------------------------------
    #
    # The replicated layer hands every store the same tree object, loaded
    # from whichever store could serve it. Mutations persist through the
    # ``GithubDirectory`` nodes (the incremental writes) and ``push_full``
    # diffs whatever tree it is given, so a tree that arrived as plain
    # ``TGFSDirectory`` nodes -- loaded from a pinned message or a document
    # store -- is converted in place: same object, store-backed nodes, with
    # the mirrors, store key, stat and dates of every file ref and folder
    # preserved.

    @property
    def metadata(self) -> Optional[TGFSMetadata]:
        return self._metadata

    @metadata.setter
    def metadata(self, value: Optional[TGFSMetadata]) -> None:
        self._metadata = None if value is None else self._as_github_tree(value)

    def _as_github_tree(self, metadata: TGFSMetadata) -> TGFSMetadata:
        if isinstance(metadata.dir, GithubDirectory):
            return metadata
        plain_root = metadata.dir
        root = GithubDirectory(
            self._ghc, name="root", parent=None, children=[], files=[]
        )
        root.created_at = plain_root.created_at
        root.modified_at = plain_root.modified_at
        self._convert_directory(plain_root, root)
        metadata.dir = root
        return metadata

    def _convert_directory(
        self, source: TGFSDirectory, target: GithubDirectory
    ) -> None:
        for child in source.find_dirs():
            # New nodes follow the configured name key, like freshly created
            # directories do; the plaintext names of a document store get
            # encrypted on the way out.
            node = GithubDirectory(
                self._ghc,
                name=child.name,
                parent=target,
                stored_encrypted=None,
                defer_timestamps=True,
            )
            target.children.append(node)
            node.created_at = child.created_at
            node.modified_at = child.modified_at
            self._convert_directory(child, node)
        for fr in source.find_files():
            # Built directly instead of via ``attach_file_ref``, which loses
            # the mirrors, store key and stat of a loaded ref.
            target.files.append(
                TGFSFileRef(
                    message_id=fr.message_id,
                    name=fr.name,
                    location=target,
                    mirrors=dict(fr.mirrors),
                    store=fr.store,
                    stat=fr.stat,
                    stat_epoch=fr.stat_epoch,
                )
            )

    async def push(self) -> None:
        pass

    # -- full-tree sync (``push_full``) -------------------------------------
    #
    # As the primary metadata store the repo is written incrementally while
    # the tree is mutated (``GithubDirectory``), so ``push`` has nothing to
    # do. As a redundant copy under the replicated metadata layer -- and as
    # the healing step after this store missed incremental writes -- the
    # whole in-memory tree is diffed against the repo and reconciled in one
    # commit.

    async def push_full(self) -> None:
        if not self.metadata:
            raise MetadataNotInitialized()
        # Under the write lock, like the incremental writes: a deferred file
        # reference create that is still retrying must not race the sync.
        # The tree is read on the event loop (it is mutated there); only the
        # GitHub round trips run on the writer thread.
        async with self._ghc.lock:
            desired_blobs = self._desired_blobs()
            stats = self._desired_stats()
            self._stats_due.clear()
            await self._ghc.write(self._sync_tree_commit, desired_blobs, stats)

    def _desired_stats(self) -> dict[str, Optional[str]]:
        """The stats blobs the scheduled folders imply."""
        blobs: dict[str, Optional[str]] = {}
        for directory in self._folders_with_stats_due(self._stats_due):
            blobs[directory.stats_path] = self._stats_content(directory)
        return blobs

    def _desired_blobs(self) -> Set[str]:
        """The blob paths the in-memory tree implies."""
        if self.metadata is None:
            raise MetadataNotInitialized()
        metadata = self.metadata
        blob_paths: Set[str] = set()
        dir_paths: Set[str] = set()

        def walk(directory: TGFSDirectory) -> None:
            path = self._dir_storage_path(directory)
            dir_paths.add(path)
            for fr in directory.find_files():
                blob_paths.add(self._ref_blob_path(directory, path, fr))
            for child in directory.find_dirs():
                walk(child)

        walk(metadata.dir)
        # Git cannot represent an empty directory: every directory without
        # a blob beneath it gets a ``.gitkeep``, like ``create_dir`` makes.
        sorted_blobs = sorted(blob_paths)
        for path in dir_paths:
            if not path:
                continue
            marker = f"{path}/"
            i = bisect.bisect_left(sorted_blobs, marker)
            if i >= len(sorted_blobs) or not sorted_blobs[i].startswith(marker):
                blob_paths.add(f"{marker}.gitkeep")
        return blob_paths

    def _dir_storage_path(self, directory: TGFSDirectory) -> str:
        """On-repo path of ``directory``; per-directory encrypted state is
        preserved for a tree of ``GithubDirectory`` nodes, a plain tree
        (loaded from another metadata store) follows the configured key."""
        if isinstance(directory, GithubDirectory):
            return directory._github_path
        segments: list[str] = []
        node: Optional[TGFSDirectory] = directory
        while node is not None and node.parent is not None:
            segments.append(self._encode_segment(node.name))
            node = node.parent
        return GithubDirectory.join_path(*reversed(segments))

    def _encode_segment(self, name: str) -> str:
        key = self._ghc.name_key
        return encrypt_path_name(key, name) if key is not None else name

    def _ref_blob_path(
        self, directory: TGFSDirectory, path: str, fr: TGFSFileRef
    ) -> str:
        if isinstance(directory, GithubDirectory):
            segment = directory._file_ref_storage_name(fr.name, fr.message_id)
        else:
            segment = f"{self._encode_segment(fr.name)}.{fr.message_id}"
        return GithubDirectory.join_path(path, segment)

    @staticmethod
    def _is_managed_blob(path: str) -> bool:
        """Whether the sync may delete this blob: file references (``name.id``
        -- the same shape the loader parses) and ``.gitkeep`` placeholders.
        Anything else in the repo (stats files, a README) is left alone."""
        segment = path.rsplit("/", 1)[-1]
        if segment == ".gitkeep":
            return True
        if segment == STATS_FILE_NAME:
            return False
        _, _, suffix = segment.rpartition(".")
        return suffix.isdigit()

    def _sync_tree_commit(
        self,
        desired_blobs: Set[str],
        stats: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        """Diff the repo against the desired blobs and commit the delta.

        Blocking; raises when the sync cannot be done safely. Retried when
        the branch moved under it, like the stats commit. A repo without
        the branch yet (a fresh mirror) gets an initial commit. ``stats``
        are the stats blobs to write in the same commit (``None`` drops a
        folder's stats file), see ``schedule_stats``.
        """
        stats = stats or {}
        repo = self._ghc.repo
        for attempt in range(SYNC_COMMIT_ATTEMPTS):
            try:
                ref = repo.get_git_ref(f"heads/{self._ghc.commit}")
            except GithubException as ex:
                if getattr(ex, "status", None) != 404:
                    raise
                self._initial_sync_commit(desired_blobs)
                return
            base_commit = repo.get_git_commit(ref.object.sha)
            tree = repo.get_git_tree(base_commit.tree.sha, recursive=True)
            if tree.truncated:
                # A truncated listing would make the rewrite below drop
                # every blob it did not see. Never sync from that.
                raise TechnicalError(
                    f"The tree of {self._ghc.repo_name}@{self._ghc.commit} is "
                    f"truncated; the metadata sync cannot run safely"
                )
            current = {e.path: (e.sha, e.mode) for e in tree.tree if e.type == "blob"}
            creates = sorted(desired_blobs - set(current))
            deletes = sorted(
                path
                for path in current
                if path not in desired_blobs and self._is_managed_blob(path)
            )
            # A stats file the tree sync does not manage is only dropped
            # when the caller says so, and a folder that keeps its stats
            # (a move or a rename) keeps its file too.
            stats_writes = {
                path: content
                for path, content in stats.items()
                if content is not None
                and (path not in current or _blob_sha(content) != current[path][0])
            }
            stats_deletes = [
                path
                for path, content in stats.items()
                if content is None and path in current
            ]
            if not creates and not deletes and not stats_writes and not stats_deletes:
                return
            elements = [
                # The content creates the blob server side; an element that
                # also carries a sha (even null) is rejected with a 422.
                InputGitTreeElement(path=path, mode="100644", type="blob", content="")
                for path in creates
            ]
            elements += [
                InputGitTreeElement(
                    path=path, mode="100644", type="blob", content=content
                )
                for path, content in sorted(stats_writes.items())
            ]
            gone = set(deletes) | set(stats_deletes)
            elements += [
                InputGitTreeElement(path=path, mode=mode, type="blob", sha=sha)
                for path, (sha, mode) in sorted(current.items())
                if path not in gone
            ]
            new_tree = repo.create_git_tree(elements)
            commit = repo.create_git_commit(
                f"Sync TGDCFS metadata tree ({len(creates)} created, "
                f"{len(deletes)} deleted)",
                new_tree,
                [base_commit],
            )
            try:
                ref.edit(commit.sha)
                logger.info(
                    f"Synced the metadata tree to {self._ghc.repo_name}@"
                    f"{self._ghc.commit}: {len(creates)} created, "
                    f"{len(deletes)} deleted, "
                    f"{len(stats_writes) + len(stats_deletes)} stats file(s) written"
                )
                return
            except GithubException as ex:
                if attempt + 1 == SYNC_COMMIT_ATTEMPTS:
                    raise
                logger.info(
                    f"Branch {self._ghc.commit} moved while syncing the "
                    f"metadata tree ({ex.status}), retrying"
                )

    def _initial_sync_commit(self, desired_blobs: Set[str]) -> None:
        """First commit on a branch that does not exist yet."""
        repo = self._ghc.repo
        elements = [
            # See _sync_tree_commit: content must go alone.
            InputGitTreeElement(path=path, mode="100644", type="blob", content="")
            for path in sorted(desired_blobs)
        ]
        new_tree = repo.create_git_tree(elements)
        commit = repo.create_git_commit(
            f"Sync TGDCFS metadata tree ({len(desired_blobs)} created)",
            new_tree,
            [],
        )
        repo.create_git_ref(f"refs/heads/{self._ghc.commit}", commit.sha)
        logger.info(
            f"Created {self._ghc.repo_name}@{self._ghc.commit} with the "
            f"synced metadata tree ({len(desired_blobs)} blobs)"
        )

    # -- file stats (``listing.stat_in_metadata``) -------------------------
    #
    # Every folder that has stats keeps them in its own ``.tgdcfsstats.json``
    # next to the file refs, keyed by descriptor message id, so a move or a
    # rename keeps them without a write. The tree load only notes the blob
    # sha; the content is read on the folder's first listing.

    push_writes_stats = False

    async def load_stats(self, directory: TGFSDirectory) -> bool:
        if not isinstance(directory, GithubDirectory) or directory.stats_loaded:
            return True
        if directory.stats_sha is None:
            directory.stats_loaded = True
            return True
        # Ten thousand concurrent listings of one folder read the blob once.
        key = id(directory)
        task = self._stats_loads.get(key)
        if task is None:
            task = asyncio.ensure_future(self._load_stats(directory))
            self._stats_loads[key] = task
            task.add_done_callback(lambda _: self._stats_loads.pop(key, None))
        # Shielded: a client that gives up does not cancel the read for the
        # listings that wait on it.
        return await asyncio.shield(task)

    async def _load_stats(self, directory: GithubDirectory) -> bool:
        sha = directory.stats_sha
        if sha is None:
            directory.stats_loaded = True
            return True
        try:
            blob = await asyncio.to_thread(self._ghc.repo.get_git_blob, sha)
            entries = json.loads(base64.b64decode(blob.content))
            if not isinstance(entries, dict):
                raise ValueError("not a JSON object")
        except Exception as ex:
            logger.warning(
                f"Could not read {directory.stats_path} from "
                f"{self._ghc.repo_name}: {ex}"
            )
            return False
        for fr in directory.find_files():
            # A ref that changed since the start has a stored entry from
            # before the change; it stays empty and is read again.
            if fr.stat is None and fr.stat_epoch == 0:
                fr.stat = FileStat.from_dict(entries.get(str(fr.message_id)))
        directory.stats_loaded = True
        return True

    def stats_stored(self, directory: TGFSDirectory) -> bool:
        return isinstance(directory, GithubDirectory) and (
            directory.stats_sha is not None
        )

    def schedule_stats(self, directories: Iterable[TGFSDirectory]) -> bool:
        """Carry the stats of ``directories`` on the next tree sync.

        A stats commit of its own made removing a folder cost two commits
        per file; folded into the sync of the removal, a folder of ten
        thousand files is two commits instead of twenty thousand. Only a
        store that writes its tree by diffing (``push_full``) can do this:
        the other shapes write nothing there, so they say no and the caller
        writes the stats itself.
        """
        if type(self).push_full is GithubRepoMetadataRepository.push_full:
            return False
        for directory in directories:
            if isinstance(directory, GithubDirectory) and directory.is_attached():
                self._stats_due.add(id(directory))
        return True

    async def save_stats(
        self, directories: Iterable[TGFSDirectory], keep: bool
    ) -> None:
        """Write the stats files of ``directories`` in one commit.

        Raises when a folder's stored stats could not be read first or the
        commit failed; the caller keeps the folders and tries again.
        """
        changes: list[tuple[GithubDirectory, Optional[str]]] = []
        unreadable: list[str] = []
        # Directories compare by content, so they are told apart by identity.
        scheduled, self._stats_due = self._stats_due, set()
        due = list(directories)
        if scheduled:
            due.extend(self._folders_with_stats_due(scheduled))
        for directory in {id(d): d for d in due}.values():
            if not isinstance(directory, GithubDirectory):
                continue
            if not directory.is_attached():
                # Deleted meanwhile; its stats file went with it.
                continue
            if keep and not await self.load_stats(directory):
                unreadable.append(directory.stats_path)
                continue
            content = self._stats_content(directory) if keep else None
            if content is None:
                if directory.stats_sha is not None:
                    changes.append((directory, None))
            elif _blob_sha(content) != directory.stats_sha:
                changes.append((directory, content))

        if changes:
            # Paths and contents are fixed here, on the event loop; the
            # thread only talks to GitHub.
            writes = [(d.stats_path, content) for d, content in changes]
            # Same writer thread as the file references: both commit on the
            # branch, and two at once is what made the stats ref update fail
            # with "not a fast forward".
            await self._ghc.write(self._commit_stats, writes)
            for directory, content in changes:
                directory.stats_sha = None if content is None else _blob_sha(content)
                directory.stats_loaded = True

        if unreadable:
            raise TechnicalError(
                f"Could not read the stored stats of {', '.join(unreadable)}"
            )

    def _folders_with_stats_due(self, ids: Set[int]) -> list[GithubDirectory]:
        """The scheduled folders that are still in the tree; blocking, reads
        the tree the write is about to serialize anyway."""
        found: list[GithubDirectory] = []

        def walk(directory: TGFSDirectory) -> None:
            if isinstance(directory, GithubDirectory) and id(directory) in ids:
                found.append(directory)
            for child in directory.find_dirs():
                walk(child)

        root = self.root()
        walk(root)
        return found

    @staticmethod
    def _stats_content(directory: GithubDirectory) -> Optional[str]:
        entries = {
            str(fr.message_id): fr.stat.to_dict()
            for fr in directory.find_files()
            if fr.stat is not None
        }
        if not entries:
            return None
        return json.dumps(entries, sort_keys=True, separators=(",", ":"))

    def _commit_stats(self, writes: list[tuple[str, Optional[str]]]) -> None:
        """One commit on top of the branch head; retried when another
        commit landed in between (the ref update is not forced)."""
        repo = self._ghc.repo
        elements = [
            (
                InputGitTreeElement(path=path, mode="100644", type="blob", sha=None)
                if content is None
                else InputGitTreeElement(
                    path=path, mode="100644", type="blob", content=content
                )
            )
            for path, content in writes
        ]
        for attempt in range(STATS_COMMIT_ATTEMPTS):
            ref = repo.get_git_ref(f"heads/{self._ghc.commit}")
            base_commit = repo.get_git_commit(ref.object.sha)
            tree = repo.create_git_tree(elements, base_tree=base_commit.tree)
            commit = repo.create_git_commit(
                f"{STATS_COMMIT_PREFIX} of {len(writes)} folder(s)",
                tree,
                [base_commit],
            )
            try:
                ref.edit(commit.sha)
                logger.info(
                    f"Wrote the stats of {len(writes)} folder(s) "
                    f"to {self._ghc.repo_name}"
                )
                return
            except GithubException as ex:
                if attempt + 1 == STATS_COMMIT_ATTEMPTS:
                    raise
                logger.info(
                    f"Branch {self._ghc.commit} moved while writing file stats "
                    f"({ex.status}), retrying"
                )

    async def get(self) -> TGFSMetadata:
        # One call to read the repo head plus at most one to diff it against
        # the cached one. Off the event loop because PyGithub is synchronous.
        await asyncio.to_thread(self._timestamps.load)
        # Off the loop too: with a large repo the walk is a request per
        # directory. The structure is built fresh, so nothing is shared.
        root_dir = await asyncio.to_thread(self._build_directory_structure)
        metadata = TGFSMetadata(dir=root_dir)
        # The replicated layer compares stores by this date: the branch head
        # is when this copy was written last. A store that proves dead later
        # during the read fails the load anyway; an empty repository has no
        # head and simply dates as unknown.
        metadata.revision = await asyncio.to_thread(self._head_commit_epoch)
        return metadata

    def _head_commit_epoch(self) -> Optional[float]:
        """When the branch was written last, as a Unix epoch, or None."""
        try:
            ref = self._ghc.repo.get_git_ref(f"heads/{self._ghc.commit}")
            commit = self._ghc.repo.get_git_commit(ref.object.sha)
            date = commit.committer.date
        except Exception:
            return None
        if not isinstance(date, datetime.datetime):
            return None
        return date.timestamp()

    def _build_directory_structure(self) -> GithubDirectory:
        root = GithubDirectory(
            self._ghc, name="root", parent=None, children=[], files=[]
        )
        self._restore_root_timestamps(root)

        try:
            entries = self._read_tree()
            if entries is not None:
                self._build_from_tree(entries, root)
            else:
                contents = self._ghc.repo.get_contents("", ref=self._ghc.commit)
                self._process_contents(contents, root)
        except GithubException as ex:
            if ex.status in (404, 409):
                # An empty repository, or the branch does not exist yet:
                # there is no tree, which is how every installation starts.
                logger.warning(
                    f"{self._ghc.repo_name}@{self._ghc.commit} holds no tree "
                    f"yet ({ex.status}); starting with an empty directory tree"
                )
            else:
                raise TechnicalError(
                    f"Could not read the metadata tree from "
                    f"{self._ghc.repo_name}@{self._ghc.commit}: {ex}"
                ) from ex
        except Exception as ex:
            # An unreachable repo must surface as a failure (the replicated
            # layer fails over to another metadata store), never as a
            # silently empty tree that would go on to diverge from the repo.
            raise TechnicalError(
                f"Could not read the metadata tree from "
                f"{self._ghc.repo_name}@{self._ghc.commit}: {ex}"
            ) from ex

        return root

    def _read_tree(self) -> Optional[Sequence[_TreeEntry]]:
        """The whole repo tree in one request, or ``None`` if it is unusable.

        ``get_contents`` lists exactly one directory, so walking the tree with
        it costs one round trip per directory -- 306 of them here, all before
        a single file can be listed. The recursive tree API returns every blob
        and subtree at once instead, which makes the load independent of how
        many folders the repo holds.

        GitHub truncates the response for very large trees (over 100,000
        entries or 7 MB, which a few tens of thousands of encrypted file
        names reach). That would silently hide whole folders, so the tree is
        then read one directory at a time instead.
        """
        try:
            tree = self._ghc.repo.get_git_tree(self._ghc.commit, recursive=True)
            if not tree.truncated:
                return [_TreeEntry(e.path, e.type, e.sha) for e in tree.tree]
            logger.warning(
                f"Tree of {self._ghc.repo_name}@{self._ghc.commit} came back "
                "truncated; reading it one directory at a time"
            )
        except Exception as ex:
            logger.warning(
                f"Could not read the tree of {self._ghc.repo_name}@{self._ghc.commit}, "
                f"falling back to a directory-by-directory walk: {ex}"
            )
            return None
        try:
            return self._walk_tree()
        except Exception as ex:
            logger.warning(
                f"Could not walk the tree of {self._ghc.repo_name}@{self._ghc.commit}, "
                f"falling back to the contents API: {ex}"
            )
            return None

    def _walk_tree(self) -> list[_TreeEntry]:
        """Every entry of the repo, one (non-recursive) tree per directory.

        One request per directory, like the contents API, but a tree lists
        all of a directory's entries where the contents API stops at 1000:
        a folder of 20k files came back with 1000 of them. Directories are
        read level by level, ``tree_fetch_concurrency`` requests at a time,
        so a big repo does not pay one round trip after another. Raises
        when a single directory's tree is truncated too.
        """
        started = time.monotonic()
        entries: list[_TreeEntry] = []
        read = 0
        pending: list[tuple[str, str]] = [("", self._ghc.commit)]
        with ThreadPoolExecutor(
            max_workers=self._ghc.tree_fetch_concurrency,
            thread_name_prefix="github-tree",
        ) as pool:
            while pending:
                batch, pending = (
                    pending[: self._ghc.tree_fetch_concurrency],
                    pending[self._ghc.tree_fetch_concurrency :],
                )
                trees = list(
                    pool.map(lambda item: self._ghc.repo.get_git_tree(item[1]), batch)
                )
                for (prefix, _), t in zip(batch, trees):
                    read += 1
                    if t.truncated:
                        raise TechnicalError(
                            f"The tree of '{prefix or '/'}' is truncated"
                        )
                    for e in t.tree:
                        path = f"{prefix}{e.path}"
                        entries.append(_TreeEntry(path, e.type, e.sha))
                        if e.type == "tree":
                            pending.append((f"{path}/", e.sha))
        logger.info(
            f"Rebuilt the tree of {self._ghc.repo_name}@{self._ghc.commit} "
            f"directory by directory: {read} directories in "
            f"{time.monotonic() - started:.1f}s"
        )
        return entries

    def _build_from_tree(
        self, entries: Sequence[_TreeEntry], root: GithubDirectory
    ) -> None:
        """Materialise the in-memory tree from one flat list of tree entries.

        Entries carry full storage paths and arrive in no guaranteed order, so
        directories are resolved on demand and every missing parent is created
        along the way.
        """
        dirs: dict[str, GithubDirectory] = {"": root}

        for entry in entries:
            if entry.type == "tree":
                self._dir_at(entry.path, dirs)
            elif entry.type == "blob":
                parent_path, _, segment = entry.path.rpartition("/")
                if segment == ".gitkeep":
                    continue
                if segment == STATS_FILE_NAME:
                    # Read on the folder's first listing, not here.
                    self._dir_at(parent_path, dirs).stats_sha = entry.sha
                    continue
                try:
                    stored_name, message_id = segment.rsplit(".", 1)
                    file_name, _ = self._decode_name(stored_name)
                    # attach, not create: the directory's date is the git
                    # history's, not the moment this tree was rebuilt.
                    self._dir_at(parent_path, dirs).attach_file_ref(
                        file_name, int(message_id)
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid name format for {segment}, expected a format like 'name.message_id'"
                    )

    def _dir_at(
        self, storage_path: str, dirs: dict[str, GithubDirectory]
    ) -> GithubDirectory:
        """The directory at ``storage_path``, created with its parents if new."""
        found = dirs.get(storage_path)
        if found is not None:
            return found

        parent_path, _, segment = storage_path.rpartition("/")
        dir_name, was_encrypted = self._decode_name(segment)
        child = self._create_child_dir(
            dir_name,
            self._dir_at(parent_path, dirs),
            stored_encrypted=was_encrypted,
            defer_timestamps=True,
        )
        # Dates come from the cache on first read. Warming them here only
        # queues the path; the workers pay for the lookup in the background
        # instead of holding the load for two calls per directory.
        if self._ghc.timestamps is not None:
            self._ghc.timestamps.request(storage_path)
        dirs[storage_path] = child
        return child

    def _restore_root_timestamps(self, root: GithubDirectory) -> None:
        """Give the root its real dates from the repo's own metadata.

        The root has no ``.gitkeep`` to date it from, so fall back to the
        backing repository's creation and last-push timestamps instead of
        the ``now()`` dataclass default. Best-effort: any failure leaves the
        default rather than breaking the load.
        """
        try:
            created = self._ghc.repo.created_at
            modified = self._ghc.repo.pushed_at or self._ghc.repo.updated_at
            if isinstance(created, datetime.datetime):
                root.created_at = created
            if isinstance(modified, datetime.datetime):
                root.modified_at = modified
        except Exception as ex:
            logger.debug(f"Could not read repo timestamps for root: {ex}")

    def _create_child_dir(
        self,
        name: str,
        parent_dir: GithubDirectory,
        stored_encrypted: bool = False,
        defer_timestamps: bool = False,
    ) -> GithubDirectory:
        child_dir = GithubDirectory(
            self._ghc,
            name,
            parent_dir,
            stored_encrypted=stored_encrypted,
            defer_timestamps=defer_timestamps,
        )
        parent_dir.children.append(child_dir)
        return child_dir

    def _decode_name(self, raw: str) -> tuple[str, bool]:
        """Map an on-repo path segment to its (plaintext, was_encrypted) form.

        Legacy plaintext segments pass through unchanged; encrypted ones are
        decrypted with the configured key. This is what lets encrypted and
        plaintext entries coexist in the same repo during/after migration.
        """
        if not is_encrypted_path_name(raw):
            return raw, False
        key = self._ghc.name_key
        if key is None:
            return raw, True  # cannot decrypt without the key
        try:
            return decrypt_path_name(key, raw), True
        except PathNameEncryptionError as ex:
            logger.warning(f"Failed to decrypt path name {raw!r}: {ex}")
            return raw, True

    def _process_contents(
        self, contents: list[ContentFile] | ContentFile, parent_dir: GithubDirectory
    ) -> None:
        if not isinstance(contents, list):
            contents = [contents]

        for content in contents:
            if content.type == "dir":
                # content.name is the on-repo (possibly encrypted) segment;
                # decrypt it for the in-memory model but keep using
                # content.path (the storage path) for the git-history lookup.
                dir_name, was_encrypted = self._decode_name(content.name)
                child_dir = self._create_child_dir(
                    dir_name,
                    parent_dir,
                    stored_encrypted=was_encrypted,
                    defer_timestamps=True,
                )
                # Dates come from the cache on first read. Warming them here
                # only queues the path; the workers pay for the lookup in the
                # background instead of holding the boot for two calls each.
                if self._ghc.timestamps is not None:
                    self._ghc.timestamps.request(content.path)
                try:
                    child_contents = self._ghc.repo.get_contents(
                        content.path, ref=self._ghc.commit
                    )
                    self._process_contents(child_contents, child_dir)
                except Exception as ex:
                    logger.warning(
                        f"Failed to construct directory {content.name}: {ex}"
                    )
            elif content.type == "file":
                try:
                    if content.name == ".gitkeep":
                        continue
                    if content.name == STATS_FILE_NAME:
                        parent_dir.stats_sha = content.sha
                        continue
                    stored_name, message_id = content.name.rsplit(".", 1)
                    file_name, _ = self._decode_name(stored_name)
                    parent_dir.attach_file_ref(file_name, int(message_id))
                except ValueError:
                    logger.warning(
                        f"Invalid name format for {content.name}, expected a format like 'name.message_id'"
                    )
