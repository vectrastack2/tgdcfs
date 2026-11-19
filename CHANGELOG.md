# Changelog

All notable changes to tgdcfs are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is below 1.0.0, a minor release may change behaviour or
configuration defaults; such changes are listed under **Changed** and marked
as breaking where an existing setup needs attention.

Every release is a Git tag `vX.Y.Z` with a matching
[GitHub release](https://github.com/Xyvran/tgdcfs/releases), and the Docker
images `xyvran/tgdcfs` and `xyvran/tgdcfs-fe` carry the tags `X.Y.Z` and `X.Y`
next to the commit tag. Releases up to and including 0.6.1 were tagged after
the fact on the commits that shipped them.

## [Unreleased]

### Fixed

- Removing files and folders over SFTP is no longer slow. FileZilla
  removes a folder one entry at a time -- an ``SSH_FXP_REMOVE`` per file,
  an ``SSH_FXP_RMDIR`` per folder -- and every one of those requests wrote
  the metadata on its own: a commit per file to a git metadata store, two
  with ``listing.stat_in_metadata`` on (one for the file reference, one
  for the folder's stats file), so removing two files took over three
  minutes and a folder of a few thousand files hours. The server now keeps
  one metadata batch open for a run of removals and writes once when the
  client goes quiet (or every couple of seconds during a long deletion), a
  recursive ``rm_dir`` is one write, and the descriptors of the files being
  removed are read a few at a time instead of strictly one after another.
  A git tree store carries the stats of the folders a removal touched on
  that same commit, so a folder of ten thousand files costs two commits
  instead of twenty thousand.
- The GitHub metadata store no longer blocks the event loop on a folder
  delete or move: the tree rewrite and the commit run on the store's writer
  thread like every other write. ``GithubDirectory.delete()`` and
  ``move_to()`` only change the tree now; the blobs are removed and moved
  through ``delete_async()`` and ``move_to_async()``, which ``rm_dir`` and
  ``mv_dir`` use.

- The file data of a replicated file system now heals itself the way the
  metadata does. With ``sync: tee`` a mirror write that failed (a mirror
  store being temporarily down, for instance) was detected and the file's
  path was put on the persistent replication queue, but no worker ever
  drained the queue for a ``tee`` file system: the copy only came back
  through a manual backfill. A replication worker now runs for every file
  system with mirrors, whatever its ``sync`` mode, and ``inline`` writes
  report their gaps to the queue as well. A store that was unreachable
  during an upload is caught up automatically once it is back, with the
  queue's usual backoff, without manual action.

- The primary metadata store is no longer the source of truth by
  definition. It always was, so a mirror that kept receiving writes
  while the primary was down (as reported in the logs: metadata was
  still written to the mirror) lost those writes at the next boot to
  the primary's older copy -- which was then also synced over the
  mirror, erasing them there too. Every store is now read at start and
  dated: git stores by the branch head commit's date, document and
  pinned-message stores by a revision that every push writes into the
  metadata document. The most recent tree is served and the older
  stores are re-synced to it, primary included. Within a short window
  (~1 minute) the primary keeps the lead, so the blink between two
  stores' write clocks never flips the source of truth in calm
  operation.

- The metadata store constructors no longer touch the network. The
  GitHub stores resolved their repository with a blocking call in
  `__init__`, on the event loop, while the configuration loaded: with
  api.github.com unreachable, PyGithub's retry cycle froze the whole
  server before the replicated metadata layer even existed, and a
  perfectly healthy GitLab mirror never got the chance to serve. The
  repositories now resolve lazily on the first API call, which runs on a
  worker thread; a failure there surfaces as that store's read error, so
  the layer fails over to the next store and the heal worker catches the
  unavailable store up once it answers again. The GitHub metadata
  clients also cap PyGithub's retry cycle at two attempts, and a circuit
  breaker refuses the store's calls once one of them ran into a dead
  connection, while a background probe alone waits for the service: one
  outage costs its connection timeouts once -- at boot, instead of
  several minutes -- and then no listing, write, stats commit or heal
  queues behind it again. When the probe finds the service answering
  again, the breaker closes and the heal worker brings the store back in
  line within a probe interval.
- The full metadata tree sync and the GitLab tree mirror no longer send
  a create element that carries both a sha and a content: the GitHub
  Git tree API rejects that combination with a 422, which left every
  sync with new blobs failing forever, and the GitLab mirror dropped
  the deletion of a stats file through a blob fetch of a null sha.
- The Docker image names in the config generator's command panel and in
  the getting-started guide follow the repository the site is built from.
  They were hard-coded (and kept pointing at a foreign namespace, because
  `scripts/sync_badges.py` only knew how to replace the upstream's names).
  `next.config.ts` now derives the Docker Hub image, the GHCR image and the
  Pages URL from `GITHUB_REPOSITORY` and `DOCKERHUB_NAMESPACE` at build
  time, and the Pages workflow and the `tgdcfs-fe` image build pass them
  in. The GHCR image is shown as `ghcr.io/<owner>/<repo>/tgdcfs`, the path
  the workflows actually publish to.

### Changed

- `scripts/sync_badges.py` and the "Sync README badges" workflow only
  touch `README.md` now; the web frontend sources are no longer rewritten.

### Added

- Repository metadata stores (GitHub and GitLab) now have a `layout`
  option. `tree` (the default for both) mirrors the directory tree into
  the repository's own directory structure, one blob per directory,
  written incrementally -- the shape `github_repo` has always used.
  `document` keeps the whole tree in one JSON file that is rewritten on
  every push, the same shape the pinned-message store uses, and is
  AES-GCM encrypted when at-rest encryption is on. GitLab's tree layout
  speaks the same engine as the GitHub store (incremental writes, diff
  sync, per-folder stats files, directory dates) through a REST facade,
  so both hosts behave identically; a GitLab repository that still holds
  the single-document layout of an older release is migrated to the tree
  layout automatically on the first push. The config generator offers
  the layout for both hosts and reads it back from an existing config.
- The config generator also gained the `file_path` option for GitHub's
  document layout, matching the GitLab store. The file path is only shown
  (and written) for the document layout, where it means something. The
  generator now checks the metadata stores of a file system the way the
  loader does -- two pinned-message stores on one store, the same
  repository twice, a pinned store outside the file system, a missing
  repository or access token -- and shows the problem inline; the store
  picker of a pinned-message store only offers the file system's own
  stores, and renaming a store keeps the pinned-message stores pointing
  at it.
- The Docker images are published to GHCR (`ghcr.io/<owner>/tgdcfs` and
  `ghcr.io/<owner>/tgdcfs-fe`) next to Docker Hub, with the same `latest`,
  commit, `preview` and release tags. GHCR publishing uses the workflow's
  own `GITHUB_TOKEN`, so a fork without Docker Hub secrets still ships its
  images; Docker Hub publishing is skipped with a warning there instead of
  failing the workflow.
- Config generator: the Docker command panel has a registry switch, so the
  shown `docker run` command and `docker-compose.yml` pull the image from
  Docker Hub or from GHCR.
- The web frontend's home page links to the documentation next to the
  getting-started guide. The link is relative, so it follows the fork's own
  Pages site like the other buttons on the page.
- Config generator: the Docker command panel has an engine switch, so the
  shown commands and hints use `podman` and `podman-compose` instead of
  Docker when Podman is selected. Docker stays the default.
- This changelog, SemVer release tags and GitHub releases. A pushed tag
  `vX.Y.Z` publishes the release notes from this file and adds the version
  tags to the Docker images of that commit.
- New config block `tgdcfs.listing` for folders with many files (see
  "Large folders" in the README). `validate_on_list` (default `true`) can
  turn off the check of every file's content messages during a listing,
  which halves the store reads of a WebDAV `PROPFIND` or an SFTP `ls`.
  `message_cache_size` (default `1024`) sets how many messages are kept in
  memory per channel; it used to be fixed at 1024, so a folder with more
  files was read from the store again on every listing.
- `tgdcfs.listing.stat_in_metadata` (default `false`) keeps each file's
  size and dates in the metadata, so a listing reads no descriptor at all
  and a folder of 10,000 files opens without 20,000 store reads. Missing
  values are read on the first listing and written back in the
  background, also when the client gives up on the listing halfway. With
  `pinned_message` they are a `stat` field in `metadata.json`, with
  `github_repo` one `.tgdcfsstats.json` per folder. Only turn it on when
  every instance that writes to the file system supports it.
- Config generator: a "Large Folders" section for the `listing` block; it
  is written only with the values that differ from the defaults, and
  "Load Existing config.yaml" reads it back.
- New option `metadata.github_repo.tree_fetch_concurrency` (default `8`):
  when the metadata repository is too big for one recursive tree call and
  the startup falls back to reading it one directory at a time, the
  directories are now read in parallel instead of one round trip after
  another, and the load logs how many directories it read and how long
  the fallback took.
- Redundant metadata (RAID-1 for the metadata, like the file stores have
  for the file bytes). A file system can list extra metadata stores under
  `metadata_mirrors`: any combination of a pinned message in a specific
  store's channel (`store: <name>` -- a Telegram pinned message and a
  Discord pinned message are two separate, individually configurable
  stores), a `github_repo` and a new `gitlab_repo` type. Every metadata
  write goes to the primary store and to each mirror; a store that is
  temporarily unreachable is skipped and marked stale, writes and reads
  continue with the stores that are up, and a background task (every 30 s,
  plus once right after start) re-writes the stale stores until they take
  the tree again. On a restart the tree is loaded from the first store
  that answers, preferring the primary, so a store that was down still
  serves its last consistent copy. `gitlab_repo` keeps the whole tree as
  one document (`metadata.json` by default, AES-256-GCM encrypted when
  at-rest encryption is on) in a GitLab repository -- gitlab.com or a
  self-hosted instance via `base_url`. Without `metadata_mirrors` nothing
  changes: the classic single-store behaviour (including the pinned
  copies a pinned-message metadata store keeps in the mirror channels)
  is preserved. The manager API reports the stores that still await
  healing.

### Changed

- SFTP uploads stream into Telegram and Discord as the data arrives instead
  of being buffered whole first. SFTP does not announce the file size up
  front, so the Telegram store now uses the streamed-upload protocol
  (`upload.saveBigFilePart` with `file_total_parts = -1` per part, the real
  total only on the last part), and large files are sent as ≤ 2 GB document
  parts by the bot without needing a premium account. Writes that run ahead
  of the upload are staged in a spooled buffer: in memory up to
  `upload_buffer_size_mb`, on disk in `upload_buffer_dir` beyond that, so
  strongly interleaved clients (sshfs, rclone with many requests in
  flight) work whatever order their writes arrive in. The upload only
  moves through a staged region once it is contiguous, and a gap no write
  ever covers is zero-filled on close. A write that lands behind bytes
  already streamed fails the upload rather than store a corrupted file,
  and a jump further ahead than the new `sftp.max_ahead_gap_mb` (default
  1024 MiB) is rejected. The disk staging is enforced against the actual
  free space of `upload_buffer_dir`, re-checked on every staged write: an
  upload that would outgrow the disk fails with a clear error ("not enough
  space in upload_buffer_dir: need X, have Y") and is discarded instead of
  being committed truncated, and the startup warning reports the effective
  cap rather than only the configured maximum.

- WebDAV listings report a file's creation date as the date of its oldest
  version. It used to be the time of the listing.

- Cache evictions are logged as one line per run, `Cache: evicted N entries
  (size) to make room` or `… for the disk headroom`, instead of one line per
  entry; the per-entry lines are at debug level.

### Fixed

- A file deleted or renamed while its deferred file-reference create was
  still retrying could reappear after a restart: the retry re-created the
  blob the delete had just removed. All file-reference blob writes of a
  repo are serialised now, and a deferred create that finds its reference
  gone from the tree stops instead of writing. A 422 answer after an
  earlier attempt (whose response was lost) is also accepted as "the blob
  landed" without matching GitHub's message text.
- An SFTP upload no longer fails with "Connection timed out after 20
  seconds of inactivity" when GitHub is slow or briefly unavailable. The
  file reference that reserves the name at open was written to the GitHub
  metadata repo synchronously: a GitHub hiccup (a run of 500 errors) kept
  the open -- and with it the whole transfer -- waiting for over 20 s, and
  FileZilla gave up. The reservation is still made right away, but the
  blob write now runs in the background and retries for about ten minutes
  before the reference is given up on; the upload itself only talks to
  Telegram and Discord.
- A background file reference write whose HTTP response got lost no longer
  drops the file from the live listing: GitHub's 422 "sha wasn't supplied"
  answer means the blob already landed, and is now accepted as success on
  every retry instead of only within one attempt.
- With `sync: tee`, streamed uploads of unknown size (SFTP) now feed the
  re-uploading mirrors from the stream too. The tee refused a stream
  without a size, so with a Discord mirror nothing at all arrived there
  until a later background pass; the branches now end at the stream's
  own EOF instead of at a byte count.
- An upload that fails or is cut off (client timeout, reset connection) no
  longer leaves the empty file behind that the PUT creates first. FileZilla
  listed it as an existing 0 B file and asked "Target file already exists" on
  every retry.
- Uploaded files lost their stored size and dates again right after the
  upload: WebDAV clients such as FileZilla set the modification time with a
  PROPPATCH after every PUT, and that dropped the stat that had just been
  recorded. The first listing of the folder then read the descriptor of every
  file (thousands of store reads, a client timeout). A changed file now gets
  its new stat straight away, and without forcing an extra stats commit.
- A dropped connection to GitHub while creating a file reference (for
  example "Max retries exceeded") is tried again twice before the upload
  fails, instead of ending the upload with a server error.
- Writing the stats of a big folder no longer starves the uploads into it.
  With stats kept per upload the folder's stats (one blob of megabytes for
  10k files) were committed every ~11 s on the same GitHub writer as the file
  references: a commit took 6-10 s, so uploads slowed from 2 s to 4-19 s per
  file and FileZilla gave up after 20 s with "Connection timed out after 20
  seconds of inactivity" (11051 files in, at 02:45). Each failed transfer
  then listed the folder again and the pile of PROPFINDs took the server
  down. After a stats write the next waits ten times as long as it took, and
  a listing reads at most 16 missing descriptors at once instead of
  thousands.
- A stat kept at upload no longer depends on the descriptor reading back as
  complete (it was just written), and when one is skipped the first ones are
  logged as "Did not keep the stat of ... on upload".
- Listing a folder of freshly uploaded files no longer reads every file back
  from the store. An upload did not keep the file's size and dates, so the
  first PROPFIND of a 20k-file folder read 20k descriptors and 20k encryption
  headers from Telegram (flood waits, about 45 minutes) and FileZilla gave up
  after 20 seconds with "Failed to retrieve directory listing", every time.
  The stat is now recorded when the file is written, the encryption header is
  kept from the write, and both go into the stored stats like any other.
  Folders uploaded by an earlier version are read once, as before.
- A metadata repository too big for GitHub's recursive tree (100,000 entries
  or 7 MB) is read one directory at a time with the tree API. It fell back to
  the contents API, which stops at 1000 entries per directory, so a folder of
  20k files came back with 1000 of them after a restart.
- Bulk uploads with a `github_repo` metadata store no longer freeze the whole
  server. Every new file is one commit in the metadata repository, and
  PyGithub made that call on the event loop. When GitHub throttled the
  commits, the loop sat in PyGithub's rate-limit sleep for minutes: no
  request was answered, the Discord heartbeat blocked, and FileZilla failed
  files with "Connection timed out after 20 seconds of inactivity" (11 files
  of a 20k-file test). Creating, deleting and moving file references now run
  on a dedicated GitHub writer thread. It is a single thread because all
  writes are commits on one branch, so the stats commit also stops losing
  races against file commits ("Update is not a fast forward").
- Uploads above 2 GB over WebDAV no longer time out between Telegram parts.
  A streamed file goes to Telegram in parts of up to 2000 MiB, and after
  each part Telegram can take a minute or more to turn it into a message.
  Nothing was read from the client in that time, so FileZilla gave up after
  20 seconds ("Connection timed out after 20 seconds of inactivity") and the
  file stayed at 0 bytes. The next part is now read while the previous one
  is being sent.
- An upload that the client aborts is now logged as a warning
  (`Client disconnected during PUT …`) instead of at debug level, so a file
  left behind incomplete shows up in the server log.
- A large upload into a cache full of small entries no longer freezes the
  server. Making room for it rewrote the cache index after every evicted
  entry on the event loop; with about 10,000 cached files that held every
  request for several minutes, so WebDAV clients timed out ("Connection timed
  out after 20 seconds of inactivity"), directory listings failed and the
  Telegram and Discord connections dropped. Evictions now run in one pass and
  write the index once; the background sweep and bulk removals do the same.
- Large files mirrored to Discord can be written with a Telegram primary.
  The file descriptor lists every Discord part (10 MB each), so a 10 GB file
  needs over a thousand ids and no longer fit Telegram's 4096-character
  message: the upload finished in both stores, then failed with
  `MessageTooLongError`, the client got a 500 and the file stayed at 0 bytes.
  Such descriptors now go into a small `overflow.json` document that the
  descriptor message points at; the message keeps its id. Older versions
  cannot read these descriptors.
- Files whose descriptor went into an `overflow.json` document no longer
  show up with 0 bytes after a restart. The document was read "to the end"
  (`-1`), which only the block cache understood; with `chunk_cache_mb: 0`
  (the default) the download was refused as an invalid range, the
  descriptor counted as lost and the file as empty. Nothing was lost: the
  files read correctly again with this fix.
- With `sync: tee` an upload is no longer written to the local cache as
  well, as the README already promised: the mirrors take the bytes from the
  upload stream, so the staged copy was never read and only cost disk space
  and evictions. `stage_uploads` now only applies to `sync: inline` and
  `sync: background`.
- The `xyvran/tgdcfs-fe` image is published again. Its static build ran
  under arm64 emulation and hit the six-hour job limit; it now runs once on
  the build host, and only the nginx stage is built per platform.

### Security

- With `encrypt_names: true` and `github_repo` metadata, the commit messages
  in the metadata repository no longer contain plaintext file and directory
  names ("Create file reference for movie.mp4"); they name the encrypted
  segment that the path already shows. Commits made by earlier versions keep
  their plaintext messages until the repository history is rewritten or the
  repository is replaced.

## [0.6.1] - 2026-10-04

### Changed

- When the local cache refuses an entry, the log now says why: which limit is
  hit (`max_size_mb`, `max_files` or the `min_free_mb` disk headroom) with the
  figures, and what holds the rest (pinned entries, entries being written, or
  a disk filled by something outside the cache). The messages read
  `Cache: not caching …`, `… is only partly cached: …` and
  `… is read without the cache: …`.

### Fixed

- A client that hangs up while its request body is being read is no longer
  logged as "Exception in ASGI application" with a traceback; it is logged at
  debug level only.

## [0.6.0] - 2026-09-28

### Added

- `sync: tee` for file systems with mirrors that re-upload (Discord on either
  side, another backend, or `mode: reupload`). The client's upload stream is
  split: the primary consumes it as before and each such mirror uploads the
  same bytes at the same time, without reading them back from the primary and
  without touching the disk. A write lasts as long as the slowest store. A
  mirror that fails mid-stream is detached and backfilled later; a failing
  primary aborts the mirrors. Mirrors that copy server-side are still
  forwarded afterwards.
- `tgdcfs.transfer.tee_buffer_mb` (default `64`): buffer per mirror in tee
  mode.
- Config generator: "Tee" in the Sync select, a "Tee Buffer per Mirror (MB)"
  field, and an on/off switch for the Telegram section like the Discord one,
  so a Discord-only config can be built from the form.

### Changed

- `sync: tee` is refused together with `strict: true` or `write_ack: cache`.
- Discord uploads read a part only once an upload slot is free, so a source
  faster than Discord is held back instead of being buffered whole in memory.
- `backends.discord.max_concurrent_uploads` now defaults to one part per bot,
  at least 3, so every configured bot stays busy.

## [0.5.1] - 2026-09-27

### Fixed

- Discord stores with several bot tokens: editing a message (the mirrored
  file descriptor) went to the next bot of the round-robin and failed with
  403 / error 50005 almost every time, leaving stale descriptor copies in
  Discord mirrors. Edits now go to the bot that sent the message; a message
  whose sender is no longer configured is replaced by a fresh one.
- Listing a folder with thousands of files blocked the event loop for 10 to
  27 seconds per PROPFIND (and made Discord bots miss their heartbeats). File
  lookups by name in a folder are now constant-time.

## [0.5.0] - 2026-09-26

### Added

- Config generator: "Load Existing config.yaml" reads a config back into the
  form, both the current layout and the tgfs layout, and lists keys the form
  has no field for instead of dropping them silently.
- Config generator: a Docker panel with the `docker run` command and a
  downloadable `docker-compose.yml` for the config at hand (ports, data
  volume, passphrase variable from a `.env` file).
- Config generator: a read-only checkbox for users.

### Changed

- Read cache: large files are now cached block by block as they are read.
  Previously a version had to pass `max_file_size_mb`, fit the whole budget
  and leave the disk headroom before a single block was cached, so large
  files were never cached at all. `max_file_size_mb` now limits only what is
  staged whole (uploads and mirror downloads).

## [0.4.1] - 2026-09-26

### Fixed

- A PROPFIND on encrypted files left one empty read-cache entry per listed
  file. They cost no budget, but piled up without a `max_files` limit, could
  evict real content, and their sparse files made a plain `tar` of the data
  directory as large as every file listed.
- `MKCOL` and `PUT` directly below the WebDAV root (outside every configured
  file system) failed with 500. They now answer 403, and a `PUT` whose parent
  folder does not exist answers 409, as RFC 4918 requires. Upload tools such
  as rclone and tgsaver now get a proper answer.

## [0.4.0] - 2026-09-26

### Added

- Local cache on the data volume (`tgdcfs.cache`, off by default). It holds
  ciphertext only and never fails a transfer: a disk error or a full cache
  means the transfer runs without it.
  - Staged uploads (`stage_uploads`): mirrors that re-upload read the bytes
    from the cache instead of downloading them from the primary again.
  - Read cache (`keep_for_reads`): repeated reads are served from disk, filled
    block by block (`block_kb`).
  - Budget and limits: `max_size_mb` (default 20480) is a hard ceiling,
    `max_files`, `max_file_size_mb`, and `min_free_mb` (default 1024) keeps
    free disk space for everything else in the data directory.
  - A background sweep every 15 minutes and at start removes orphans, releases
    stale pins, drops entries unread for `max_age_hours` (default 0, off) and
    evicts down to `target_fill_percent` (default 90) of the budget.
- Write-back uploads per file system (`write_ack: cache`, default `primary`):
  a PUT is answered once the bytes are in the local cache, and a worker
  distributes them to the primary and every mirror in parallel. Requires
  `cache.enabled`, refused with `strict: true`. Until the primary has the
  file, it exists only on the local disk.
- Multi-source downloads per file system (`read_parallel`, default off;
  `read_sources` to limit the stores): a download is split into pieces served
  by every store that holds a complete copy, with per-piece failover.
- `tgdcfs.transfer.upload_parts_in_flight` (default 1): upload several
  Telegram parts at once from a seekable source such as the cache.
- `tgdcfs.transfer.read_parallel_window`: reorder window for multi-source
  reads.
- Manager API: `GET /api/cache` (budget, entries, hits and misses, free disk
  space, last sweep) and `POST /api/cache/evict`. The mini app shows the cache
  in the "Mirrors and replication" dialog.
- Config generator: a "Local Cache" section and the per-file-system fields
  `write_ack`, `read_parallel` and `read_sources`. Only values that differ
  from the defaults are written.

## [0.3.0] - 2026-09-26

### Added

- WebDAV `PROPPATCH`: `getlastmodified` and `Win32LastModifiedTime` set the
  modification time of the latest file version. The Windows attributes
  CarotDAV sends along (`Win32CreationTime`, `Win32LastAccessTime`,
  `Win32FileAttributes`) are accepted and ignored, so CarotDAV no longer
  reports a failed upload. Other properties are refused with 403.
- A startup warning when an explicit `sync: inline` or `strict: true` makes
  every write wait for a mirror that re-uploads.

### Changed

- **Breaking for configs without `sync`:** a file system that leaves out
  `sync` now gets `background` when a mirror re-uploads (another backend,
  Discord on either side, `mode: reupload`) and `inline` for Telegram to
  Telegram forwarding. Before, it was always `inline`, which made large
  uploads with a Discord mirror time out.
- The config generator writes `sync` only when it differs from that derived
  default, and `strict` only when true.
- GitHub metadata loads with a fixed number of API calls regardless of the
  folder count (one recursive tree call, folder dates cached on disk as
  `dir-timestamps-<repo>-<branch>.json`). Startup went from minutes to
  seconds on large trees.

### Fixed

- The Discord login is retried on server errors (5xx, refused connections)
  instead of taking the server down at startup; a rejected token still fails
  at once.
- A request for a path outside every file system (e.g. a browser's
  `/webdav/favicon.ico`) answers 404 instead of raising a `KeyError`.
- WebDAV reported the boot time as the modification date of every folder that
  contains files; the folder dates from the repository history are kept now.

## [0.2.0] - 2026-09-25

### Added

- Discord as a storage backend (`backends.discord`): a Discord channel can be
  a primary or a mirror store. Files are cut into parts of
  `max_file_size_bytes` (default 10 MB), several bot tokens are used in
  parallel, long descriptors travel as attachments, and transient failures
  are retried. Adapted from [dcfs](https://github.com/VulcanoSoftware/dcfs).
- Store abstraction and a new config layout: `backends`, named `stores` and
  `filesystems` with a `primary` and optional `mirrors`. Swapping primary and
  mirror is a config change. The tgfs layout (`private_file_channel`,
  `metadata` per channel, `redundancy`) is still read and translated on load.
- Cross-backend mirroring with replicas in the mirror's own part layout, and
  reads that fail over part by part across copies and replicas
  (`read_preference` sets the order).
- Background replication (`sync: background`) with a persistent queue
  (`replication_queue.json` in the data directory) and retries.
- Manager API: `GET /stores`, `GET /filesystems`, `GET /replication/queue`,
  `POST /replication/retry`.
- Frontend: the config generator edits stores and file systems and has an
  optional Discord section; the getting-started guide covers the Discord bot;
  the mini app has a "Mirrors and replication" dialog.

## [0.1.0] - 2026-09-24

### Added

- tgdcfs, bootstrapped from [tgfs](https://github.com/Xyvran/tgfs) at commit
  `7464666`: Telegram as storage with WebDAV and SFTP access, at-rest
  encryption and GitHub or Telegram metadata.
- Docker images `xyvran/tgdcfs` and `xyvran/tgdcfs-fe`; container user and
  data directory `/home/tgdcfs/.tgdcfs`.

### Changed

- Renamed from tgfs: package `tgdcfs`, environment variables `TGDCFS_*` and
  data directory `~/.tgdcfs`. The old `TGFS_*` variables and a top-level
  `tgfs:` config block still work with a deprecation warning.
- Files and metadata written by tgfs are read unchanged; the on-disk
  encryption format keeps its tgfs constants.

[Unreleased]: https://github.com/Xyvran/tgdcfs/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/Xyvran/tgdcfs/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Xyvran/tgdcfs/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/Xyvran/tgdcfs/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/Xyvran/tgdcfs/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Xyvran/tgdcfs/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Xyvran/tgdcfs/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Xyvran/tgdcfs/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Xyvran/tgdcfs/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Xyvran/tgdcfs/releases/tag/v0.1.0
