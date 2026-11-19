"""Redundancy for the metadata itself: RAID-1 across metadata stores.

A file system's file bytes already have redundancy (a primary store plus
mirror stores). The metadata had exactly one store: a pinned message
*or* a git repository. This module gives the metadata the same shape the
file stores have: one primary metadata store plus any number of
redundant metadata stores (:class:`ReplicatedMetadataRepository`), any
combination of a pinned message in a specific store's channel (Telegram
or Discord, each its own store), a GitHub repo and a GitLab repo.

Behaviour:

* Every ``push`` writes the primary store and then every mirror. A
  mirror that fails is logged, marked *stale* and skipped -- the write
  still succeeds as long as one store holds the tree, and the next push
  writes the stale store again.
* ``init`` loads the tree from the primary; when the primary cannot be
  read (store down, repository unreachable) it fails over to the first
  mirror that holds one. A store that holds no document at all counts as
  empty, not as a failure: a fresh installation bootstraps an empty tree
  and pushes it everywhere, while a primary that is merely unreachable
  never gets shadowed by an empty bootstrap.
* A background heal (:mod:`tgdcfs.core.metadata_healing`) re-writes the
  stores that are stale or were skipped, so a store that was down for a
  while is brought back in line once it returns. Until then it keeps
  serving its last consistent copy, which is what a read failover after
  a restart falls back to.

Conflict handling: every push stamps the tree with a revision (see
``TGFSMetadata.revision``) and the load serves the store with the
freshest one -- the primary only by preference when a mirror is not
meaningfully newer, never by force. The stores only diverge while one
of them is down, exactly the situation the heal closes; a store whose
copy was beaten at load is handed the winner's tree like a stale one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.errors import (
    MetadataBlobTooLarge,
    MetadataNotInitialized,
    NoPinnedMessage,
    NoStoredMetadata,
    TechnicalError,
)

logger = logging.getLogger(__name__)

# A store that answers "I hold no metadata document yet" is empty, not
# broken. Both blob-store shapes raise one of these on a fresh store.
EMPTY_METADATA_ERRORS = (NoPinnedMessage, NoStoredMetadata)

# A mirror's copy is always written shortly after the primary's (the
# writes run in order) and the clocks behind the revisions are not one
# clock: git stores report their server's commit date, document stores
# the time of the writing process. A mirror only wins the load when it
# is newer by more than this much -- otherwise the primary keeps the
# lead, and calm operation never flips the source of truth.
PRIMARY_FRESHNESS_WINDOW_SECONDS = 60.0


@dataclass
class MetadataStore:
    """One metadata store inside a replicated group.

    ``key`` identifies the store in logs and in the manager API; it is
    derived from the configuration (``pinned_message@<store>``,
    ``github_repo@<repo>@<branch>``), not from the store object.
    """

    key: str
    repo: IMetaDataRepository
    primary: bool = False


class ReplicatedMetadataRepository(IMetaDataRepository):
    def __init__(self, stores: Sequence[MetadataStore]):
        if not stores:
            raise ValueError("a replicated metadata repository needs a store")
        if not stores[0].primary:
            raise ValueError("the first metadata store must be the primary")
        self._stores: List[MetadataStore] = list(stores)
        # Keys of the stores that missed a write and await a re-sync.
        self._stale: Set[str] = set()
        # Keys of the stores that cannot hold the current document at all
        # (a pinned copy whose messages are too small), with the reason.
        # Re-checked on every heal: a smaller tree fits again.
        self._skipped: dict[str, str] = {}
        super().__init__()
        # Read as a plain attribute by the API layer, so it is copied from
        # the primary instead of forwarded dynamically.
        self.push_writes_stats = stores[0].repo.push_writes_stats

    # -- the shared tree ----------------------------------------------------

    async def get(self) -> TGFSMetadata:
        """The tree this layer serves; loads it when nothing is loaded yet."""
        if self.metadata is not None:
            return self.metadata
        tree = await self._load()
        self.metadata = tree
        return tree

    @property
    def metadata(self) -> Optional[TGFSMetadata]:
        return self._stores[0].repo.metadata

    @metadata.setter
    def metadata(self, value: Optional[TGFSMetadata]) -> None:
        # One tree, shared by every store: the APIs mutate it in place and
        # each store serializes (or incrementally writes) that same object.
        for store in self._stores:
            store.repo.metadata = value

    # -- loading --------------------------------------------------------------

    async def init(self) -> None:
        tree = await self._load()
        self.metadata = tree
        await self.push()

    async def _load(self) -> TGFSMetadata:
        """The tree to serve: the most recent one across the stores.

        Every store that answers is read and dated by its revision, so
        the copy a store received while the primary was down wins over
        the primary's older one; git stores date themselves by their
        branch head commit. The primary is preferred only while no other
        store is meaningfully newer, so calm operation never flips the
        source of truth on the split seconds between two stores' clocks.

        Stores that could not serve a tree (failed *or* empty *or* older
        than the winner) are marked stale, so the push that follows and
        the periodic heal write the winning tree to them."""
        candidates: List[tuple[MetadataStore, TGFSMetadata]] = []
        fresh: Optional[TGFSMetadata] = None
        fresh_store: Optional[MetadataStore] = None
        failures: List[str] = []
        first_error: Optional[Exception] = None

        for store in self._stores:
            try:
                candidates.append((store, await store.repo.get()))
            except EMPTY_METADATA_ERRORS:
                # The store is reachable and holds nothing. Remember the
                # first one; if no store holds a tree, this is where a
                # fresh installation starts.
                self._stale.add(store.key)
                if fresh is None:
                    fresh = TGFSMetadata(dir=TGFSDirectory.root_dir())
                    fresh_store = store
            except Exception as ex:
                self._stale.add(store.key)
                if first_error is None:
                    first_error = ex
                failures.append(f"{store.key}: {ex}")
                logger.warning(
                    f"Could not read the metadata from store {store.key}: {ex}"
                )

        if not candidates:
            if fresh is not None and fresh_store is not None:
                if failures:
                    logger.warning(
                        f"No metadata store could serve a tree "
                        f"({'; '.join(failures)}); starting from the "
                        f"empty tree of {fresh_store.key}"
                    )
                return fresh
            raise TechnicalError(
                "No metadata store could be read: " + "; ".join(failures)
            ) from first_error

        winner, tree = self._freshest(candidates)
        for store, _ in candidates:
            if store is not winner:
                self._stale.add(store.key)
                logger.info(
                    f"Metadata store {winner.key} holds the most recent "
                    f"metadata tree; store {store.key} will be re-synced"
                )
        return tree

    def _freshest(
        self,
        candidates: Sequence[tuple[MetadataStore, TGFSMetadata]],
    ) -> tuple[MetadataStore, TGFSMetadata]:
        """The most recent tree among the loaded ones, the primary
        preferred on a tie or when no store beats it beyond the window.
        A tree without a revision (a legacy document, or a store that
        could not date itself) ranks below every dated one."""
        primary = self._stores[0]
        primary_tree = next(
            (tree for store, tree in candidates if store is primary), None
        )

        def revision(tree: TGFSMetadata) -> float:
            return tree.revision if tree.revision is not None else 0.0

        # A stable sort keeps the configuration order within equal dates.
        ordered = sorted(candidates, key=lambda pair: revision(pair[1]), reverse=True)
        winner, tree = ordered[0]
        if (
            winner is not primary
            and primary_tree is not None
            and primary_tree.revision is not None
            and revision(primary_tree)
            >= revision(tree) - PRIMARY_FRESHNESS_WINDOW_SECONDS
        ):
            return primary, primary_tree
        return winner, tree

    # -- writing ---------------------------------------------------------------

    def begin_batch(self) -> None:
        """Fold the writes of a bulk operation into one, for every store."""
        for store in self._stores:
            store.repo.begin_batch()

    async def end_batch(self) -> None:
        for store in self._stores:
            await store.repo.end_batch()

    def schedule_stats(self, directories: Iterable[TGFSDirectory]) -> bool:
        """Let the stores that can carry stats along take them; the others
        need their own stats write, so the caller is told to make one."""
        taken = [store.repo.schedule_stats(directories) for store in self._stores]
        return all(taken)

    async def push(self) -> None:
        if not self.metadata:
            raise MetadataNotInitialized()
        self._stamp_revision()

        any_store_ok = False
        errors: List[str] = []
        for i, store in enumerate(self._stores):
            # The primary gets its regular write (a github repo primary has
            # already written incrementally while the tree was mutated); the
            # mirrors always get the full sync, since they may have missed
            # any of those incremental writes while they were down. A primary
            # that could not serve the tree at boot (it was down or empty)
            # gets the full sync too: its regular write is a no-op for a tree
            # store, and everything it missed while it was unavailable has
            # to land before the next mutation goes through incrementally.
            ok, error = await self._write(store, full=i > 0 or store.key in self._stale)
            any_store_ok = any_store_ok or ok
            if error:
                errors.append(error)
        if not any_store_ok:
            # Not one store holds the tree; the caller has to know.
            raise TechnicalError(
                "Writing the metadata failed for every store: " + "; ".join(errors)
            )

    def _stamp_revision(self) -> None:
        """Date the tree being written, so the stores hold comparable
        revisions at the next load. Document shaped stores serialize it
        into the metadata document; git tree stores date themselves by
        their branch head commit either way."""
        tree = self.metadata
        if tree is None:
            return
        now = time.time()
        tree.revision = now if tree.revision is None else max(tree.revision, now)

    async def _write(
        self, store: MetadataStore, full: bool
    ) -> tuple[bool, Optional[str]]:
        """Write the whole tree to ``store``; reports success and, on
        failure, a short reason. Failures mark the store stale (or
        skipped, when it cannot hold the document at all) for the heal."""
        try:
            if full:
                await store.repo.push_full()
            else:
                await store.repo.push()
        except MetadataBlobTooLarge as ex:
            reason = str(ex)
            if store.key not in self._skipped:
                logger.warning(
                    f"Metadata store {store.key} cannot hold the metadata "
                    f"document; it is skipped until a smaller document fits: "
                    f"{reason}"
                )
            self._skipped[store.key] = reason
            return False, reason
        except Exception as ex:
            reason = str(ex) or repr(ex)
            self._stale.add(store.key)
            logger.error(
                f"Writing the metadata to store {store.key} failed; it will "
                f"be re-synced once it is available again: {reason}"
            )
            return False, reason
        self._stale.discard(store.key)
        self._skipped.pop(store.key, None)
        return True, None

    async def heal(self) -> None:
        """Re-write every store that is stale or skipped.

        Called periodically by the heal worker and safe to call any time;
        a store that is up to date costs nothing, and one that just came
        back is brought in line with the current tree.
        """
        if not self.metadata:
            return
        for store in self._stores:
            if store.key in self._stale or store.key in self._skipped:
                await self._write(store, full=True)

    def stale_stores(self) -> List[str]:
        return sorted(self._stale | set(self._skipped))

    # -- file stats (``listing.stat_in_metadata``) ---------------------------
    #
    # Stats are a primary-store concern: they ride on the file refs of the
    # shared tree (blob stores) or live in the primary git repo's stats
    # files (``github_repo``). Mirrors pick them up through the shared tree
    # or not at all.

    async def load_stats(self, directory: TGFSDirectory) -> bool:
        return await self._stores[0].repo.load_stats(directory)

    def stats_stored(self, directory: TGFSDirectory) -> bool:
        return self._stores[0].repo.stats_stored(directory)

    async def save_stats(
        self, directories: Iterable[TGFSDirectory], keep: bool
    ) -> None:
        await self._stores[0].repo.save_stats(directories, keep)
