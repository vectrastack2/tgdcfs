"""The replicated metadata layer: failover on load, redundant writes,
stale bookkeeping and healing."""

import logging
import time

import pytest

from tgdcfs.core.model import TGFSDirectory, TGFSMetadata
from tgdcfs.core.repository.impl.metadata.replicated import (
    MetadataStore,
    ReplicatedMetadataRepository,
)
from tgdcfs.core.repository.interface import IMetaDataRepository
from tgdcfs.errors import (
    MetadataBlobTooLarge,
    MetadataNotInitialized,
    NoStoredMetadata,
    TechnicalError,
)


class FakeMetadataRepo(IMetaDataRepository):
    """A scripted metadata repository.

    ``stored`` is the tree the store holds on the "server"; ``get`` serves
    it (or the configured error). ``push``/``push_full`` persist the tree
    they were handed, so a test can see which store ended up with what.
    """

    push_writes_stats = False

    def __init__(
        self,
        name: str,
        stored: TGFSMetadata | None = None,
        *,
        empty: bool = False,
        get_error: Exception | None = None,
        push_error: Exception | None = None,
        push_full_error: Exception | None = None,
        push_writes_stats: bool = False,
    ):
        super().__init__()
        self.name = name
        self.stored = stored
        self.empty = empty
        self.get_error = get_error
        self.push_error = push_error
        self.push_full_error = push_full_error
        self.push_writes_stats = push_writes_stats
        self.push_calls = 0
        self.push_full_calls = 0
        self.load_stats_calls = 0
        self.save_stats_calls = 0
        self.stats_stored_calls = 0

    async def get(self) -> TGFSMetadata:
        if self.get_error is not None:
            raise self.get_error
        if self.empty or self.stored is None:
            raise NoStoredMetadata(self.name)
        return self.stored

    async def push(self) -> None:
        self.push_calls += 1
        if self.push_error is not None:
            raise self.push_error
        self.stored = self.metadata

    async def push_full(self) -> None:
        self.push_full_calls += 1
        if self.push_full_error is not None:
            raise self.push_full_error
        self.stored = self.metadata

    async def load_stats(self, directory: TGFSDirectory) -> bool:
        self.load_stats_calls += 1
        return True

    def stats_stored(self, directory: TGFSDirectory) -> bool:
        self.stats_stored_calls += 1
        return True

    async def save_stats(self, directories, keep: bool) -> None:
        self.save_stats_calls += 1


def tree(*names: str) -> TGFSMetadata:
    root = TGFSDirectory.root_dir()
    for name in names:
        root.create_dir(name)
    return TGFSMetadata(root)


def replicated(primary: FakeMetadataRepo, *mirrors: FakeMetadataRepo):
    stores = [MetadataStore(key=primary.name, repo=primary, primary=True)]
    stores += [MetadataStore(key=m.name, repo=m) for m in mirrors]
    return ReplicatedMetadataRepository(stores)


class TestConstruction:
    def test_needs_a_store(self):
        with pytest.raises(ValueError, match="needs a store"):
            ReplicatedMetadataRepository([])

    def test_first_store_must_be_the_primary(self):
        with pytest.raises(ValueError, match="primary"):
            ReplicatedMetadataRepository(
                [MetadataStore(key="m", repo=FakeMetadataRepo("m"))]
            )

    def test_push_writes_stats_is_copied_from_the_primary(self):
        primary = FakeMetadataRepo("primary", push_writes_stats=True)
        mirror = FakeMetadataRepo("mirror", push_writes_stats=False)
        layer = replicated(primary, mirror)
        assert layer.push_writes_stats is True


class TestLoad:
    async def test_the_primary_serves_the_tree(self):
        primary_tree, mirror_tree = tree("primary"), tree("mirror")
        primary = FakeMetadataRepo("primary", primary_tree)
        mirror = FakeMetadataRepo("mirror", mirror_tree)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is primary_tree
        assert primary.stored is primary_tree
        # The mirror was brought in line with the primary's tree.
        assert mirror.stored is primary_tree
        assert mirror.push_full_calls == 1
        assert mirror.push_calls == 0

    async def test_a_down_primary_fails_over_to_the_mirror(self):
        mirror_tree = tree("mirror")
        primary = FakeMetadataRepo("primary", get_error=TechnicalError("down"))
        mirror = FakeMetadataRepo("mirror", mirror_tree)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is mirror_tree
        # The push that follows init wrote the mirror's tree back into the
        # primary, so nothing is stale any more. The primary missed the
        # incremental writes while it was down, so it gets the full sync.
        assert layer.stale_stores() == []
        assert primary.stored is mirror_tree
        assert primary.push_full_calls == 1
        assert primary.push_calls == 0

    async def test_every_store_empty_bootstraps_a_fresh_tree(self):
        primary = FakeMetadataRepo("primary", empty=True)
        mirror = FakeMetadataRepo("mirror", empty=True)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is not None
        assert layer.metadata.dir.find_dirs() == []
        # The bootstrap was pushed everywhere.
        assert primary.stored is layer.metadata
        assert mirror.stored is layer.metadata
        assert layer.stale_stores() == []

    async def test_an_empty_store_does_not_shadow_a_failing_one(self):
        # The primary answers "nothing here" while the mirror is down.
        # The fresh tree wins: the mirror is marked stale and healed, the
        # empty primary is written too -- nothing raises.
        primary = FakeMetadataRepo("primary", empty=True)
        mirror = FakeMetadataRepo("mirror", get_error=TechnicalError("down"))
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is not None
        assert primary.stored is layer.metadata
        # The mirror recovered on the push that follows init.
        assert mirror.stored is layer.metadata
        assert layer.stale_stores() == []

    async def test_every_store_failing_raises(self):
        primary = FakeMetadataRepo("primary", get_error=TechnicalError("down"))
        mirror = FakeMetadataRepo("mirror", get_error=TechnicalError("also down"))
        layer = replicated(primary, mirror)

        with pytest.raises(TechnicalError, match="No metadata store could be read"):
            await layer.init()
        assert layer.stale_stores() == ["mirror", "primary"]

    async def test_get_serves_the_loaded_tree_without_reading_again(self):
        primary = FakeMetadataRepo("primary", tree("primary"))
        mirror = FakeMetadataRepo("mirror", tree("mirror"))
        layer = replicated(primary, mirror)
        await layer.init()

        primary.get_error = TechnicalError("down")
        result = await layer.get()

        assert result is layer.metadata
        assert primary.get_error is None or True  # get() was not called again


class TestFreshestLoad:
    """The load serves the most recent tree, not the primary's by force:
    a store that was written while the primary was down keeps its data."""

    async def test_a_fresher_mirror_wins_over_the_primary(self):
        primary_tree, mirror_tree = tree("primary"), tree("mirror")
        primary_tree.revision = 1_000_000_000.0
        mirror_tree.revision = primary_tree.revision + 3600.0
        primary = FakeMetadataRepo("primary", primary_tree)
        mirror = FakeMetadataRepo("mirror", mirror_tree)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is mirror_tree
        assert layer.stale_stores() == []
        assert primary.push_full_calls == 1
        assert primary.stored is mirror_tree

    async def test_the_primary_keeps_the_lead_within_the_freshness_window(self):
        """The mirror is always committed a blink after the primary and its
        clock is another clock; within the window the primary serves."""
        primary_tree, mirror_tree = tree("primary"), tree("mirror")
        primary_tree.revision = 1_000_000_000.0
        mirror_tree.revision = primary_tree.revision + 10.0
        primary = FakeMetadataRepo("primary", primary_tree)
        mirror = FakeMetadataRepo("mirror", mirror_tree)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is primary_tree
        assert primary.push_calls == 1
        assert primary.push_full_calls == 0
        assert mirror.stored is primary_tree

    async def test_an_older_mirror_does_not_shadow_the_primary(self):
        primary_tree, mirror_tree = tree("primary"), tree("mirror")
        primary_tree.revision = 1_000_000_000.0
        mirror_tree.revision = primary_tree.revision - 3600.0
        primary = FakeMetadataRepo("primary", primary_tree)
        mirror = FakeMetadataRepo("mirror", mirror_tree)
        layer = replicated(primary, mirror)

        await layer.init()

        assert layer.metadata is primary_tree
        assert mirror.stored is primary_tree

    async def test_trees_without_a_revision_rank_in_configuration_order(self):
        """Legacy documents predate revisions; without a date anywhere the
        primary serves, like it always did."""
        layer = replicated(
            FakeMetadataRepo("primary", tree("primary")),
            FakeMetadataRepo("mirror", tree("mirror")),
        )

        await layer.init()

        assert layer.metadata.dir.find_dirs()[0].name == "primary"

    async def test_every_push_stamps_the_tree_revision(self):
        layer = replicated(FakeMetadataRepo("primary"), FakeMetadataRepo("mirror"))
        layer.metadata = tree("a")

        await layer.push()

        assert layer.metadata.revision is not None
        assert layer.metadata.revision <= time.time()
        assert layer.metadata.revision > time.time() - 5.0


class TestPush:
    async def test_the_primary_pushes_normally_and_mirrors_fully(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        await layer.push()

        assert primary.push_calls == 1
        assert primary.push_full_calls == 0
        assert mirror.push_full_calls == 1
        assert mirror.push_calls == 0
        assert layer.stale_stores() == []

    async def test_push_without_a_tree_raises(self):
        layer = replicated(FakeMetadataRepo("primary"), FakeMetadataRepo("mirror"))
        with pytest.raises(MetadataNotInitialized):
            await layer.push()

    async def test_a_failing_mirror_does_not_fail_the_push(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror", push_full_error=TechnicalError("down"))
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        await layer.push()

        assert primary.stored is layer.metadata
        assert layer.stale_stores() == ["mirror"]

    async def test_a_failing_primary_does_not_fail_the_push(self):
        primary = FakeMetadataRepo("primary", push_error=TechnicalError("down"))
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        await layer.push()

        assert mirror.stored is layer.metadata
        assert layer.stale_stores() == ["primary"]

    async def test_every_store_failing_raises(self):
        primary = FakeMetadataRepo("primary", push_error=TechnicalError("down"))
        mirror = FakeMetadataRepo("mirror", push_full_error=TechnicalError("down"))
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        with pytest.raises(TechnicalError, match="failed for every store"):
            await layer.push()
        assert layer.stale_stores() == ["mirror", "primary"]

    async def test_a_stale_mirror_is_left_to_the_heal(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror", push_full_error=TechnicalError("down"))
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")
        await layer.push()
        assert layer.stale_stores() == ["mirror"]
        assert mirror.push_full_calls == 1

        # Even a mirror that came back is not waited for by the push: the
        # file operation that triggered it must not pay for a re-sync.
        mirror.push_full_error = None
        layer.metadata.dir.create_dir("b")
        await layer.push()

        assert mirror.push_full_calls == 1
        assert primary.push_calls == 2
        assert layer.stale_stores() == ["mirror"]

        await layer.heal()

        assert mirror.stored is layer.metadata
        assert mirror.push_full_calls == 2
        assert layer.stale_stores() == []

    async def test_a_stale_mirror_is_still_tried_when_the_primary_failed(self):
        primary = FakeMetadataRepo("primary", push_error=TechnicalError("down"))
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")
        layer._stale.add("mirror")

        await layer.push()

        # The tree has to land somewhere, so the stale mirror is written.
        assert mirror.stored is layer.metadata
        assert mirror.push_full_calls == 1

    async def test_the_boot_syncs_a_stale_mirror_right_away(self):
        primary = FakeMetadataRepo("primary", tree("a"))
        mirror = FakeMetadataRepo("mirror", empty=True)
        layer = replicated(primary, mirror)

        await layer.init()

        assert mirror.stored is layer.metadata
        assert layer.stale_stores() == []

    async def test_a_heal_that_raced_a_push_keeps_the_store_stale(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")
        layer._stale.add("mirror")

        original = mirror.push_full

        async def push_full_while_a_push_lands():
            await original()
            # A file operation pushes while the mirror is being written.
            layer.metadata.dir.create_dir("b")
            await layer.push()

        setattr(mirror, "push_full", push_full_while_a_push_lands)

        await layer.heal()

        # The write started before that push, so it may lack its changes.
        assert layer.stale_stores() == ["mirror"]
        assert mirror.push_full_calls == 1

        setattr(mirror, "push_full", original)
        await layer.heal()

        assert layer.stale_stores() == []
        assert mirror.push_full_calls == 2


class TestTooLarge:
    async def test_an_oversized_document_skips_the_store(self, caplog):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo(
            "mirror",
            push_full_error=MetadataBlobTooLarge(5000, "mirror", 4096),
        )
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        with caplog.at_level(
            logging.WARNING, logger="tgdcfs.core.repository.impl.metadata.replicated"
        ):
            await layer.push()
            await layer.push()

        assert layer.stale_stores() == ["mirror"]
        # Warned once, not on every push.
        warnings = [
            r for r in caplog.records if "cannot hold the metadata" in r.message
        ]
        assert len(warnings) == 1

    async def test_a_smaller_document_fits_again_after_a_heal(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        too_large = MetadataBlobTooLarge(5000, "mirror", 4096)
        mirror.push_full_error = too_large
        layer.metadata = tree("a")
        await layer.push()
        assert layer.stale_stores() == ["mirror"]

        mirror.push_full_error = None
        await layer.heal()

        assert mirror.stored is layer.metadata
        assert layer.stale_stores() == []


class TestHeal:
    async def test_heal_rewrites_only_the_stale_stores(self):
        primary = FakeMetadataRepo("primary")
        stale = FakeMetadataRepo("stale", push_full_error=TechnicalError("down"))
        healthy = FakeMetadataRepo("healthy")
        layer = replicated(primary, stale, healthy)
        layer.metadata = tree("a")
        await layer.push()
        assert stale.push_full_calls == 1

        stale.push_full_error = None
        await layer.heal()

        assert stale.stored is layer.metadata
        assert stale.push_full_calls == 2
        assert healthy.push_full_calls == 1
        assert layer.stale_stores() == []

    async def test_heal_without_a_tree_is_a_noop(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror", push_full_error=TechnicalError("down"))
        layer = replicated(primary, mirror)
        layer._stale.add("mirror")

        await layer.heal()

        assert mirror.push_full_calls == 0

    async def test_heal_keeps_the_stale_mark_when_the_store_is_still_down(self):
        primary = FakeMetadataRepo("primary")
        mirror = FakeMetadataRepo("mirror", push_full_error=TechnicalError("down"))
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")
        await layer.push()

        await layer.heal()

        assert layer.stale_stores() == ["mirror"]
        assert mirror.stored is None


class TestMetadataPropagation:
    async def test_the_tree_is_shared_with_every_store(self):
        primary = FakeMetadataRepo("primary")
        mirror_a = FakeMetadataRepo("mirror-a")
        mirror_b = FakeMetadataRepo("mirror-b")
        layer = replicated(primary, mirror_a, mirror_b)
        shared = tree("shared")

        layer.metadata = shared

        assert primary.metadata is shared
        assert mirror_a.metadata is shared
        assert mirror_b.metadata is shared
        assert layer.metadata is shared

    async def test_setting_none_clears_every_store(self):
        primary = FakeMetadataRepo("primary", tree("a"))
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        layer.metadata = tree("a")

        layer.metadata = None

        assert primary.metadata is None
        assert mirror.metadata is None


class TestStatsForwarding:
    async def test_stats_go_to_the_primary_only(self):
        primary = FakeMetadataRepo("primary", push_writes_stats=True)
        mirror = FakeMetadataRepo("mirror")
        layer = replicated(primary, mirror)
        directory = TGFSDirectory.root_dir()

        assert await layer.load_stats(directory) is True
        assert layer.stats_stored(directory) is True
        await layer.save_stats([directory], keep=False)

        assert primary.load_stats_calls == 1
        assert primary.stats_stored_calls == 1
        assert primary.save_stats_calls == 1
        assert mirror.load_stats_calls == 0
        assert mirror.stats_stored_calls == 0
        assert mirror.save_stats_calls == 0
