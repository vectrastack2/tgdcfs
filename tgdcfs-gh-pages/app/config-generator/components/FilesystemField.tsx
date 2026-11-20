import { Add, Delete } from "@mui/icons-material";
import {
  Box,
  Button,
  Checkbox,
  Chip,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  OutlinedInput,
  Select,
  Typography,
} from "@mui/material";
import {
  FilesystemConfig,
  MetadataEntry,
  MetadataLayout,
  MetadataPushMode,
  MetadataType,
  MirrorMode,
  StoreConfig,
  SyncMode,
  WriteAck,
  needsReupload,
} from "../types";
import { ConfigTextField } from "./ConfigTextField";

interface FilesystemFieldProps {
  filesystem: FilesystemConfig;
  stores: StoreConfig[];
  onUpdate: <K extends keyof FilesystemConfig>(
    field: K,
    value: FilesystemConfig[K]
  ) => void;
  onUpdateMetadata: (entry: MetadataEntry) => void;
  onUpdateMetadataMirror: (index: number, entry: MetadataEntry) => void;
  onAddMetadataMirror: () => void;
  onRemoveMetadataMirror: (index: number) => void;
  onDelete?: () => void;
  nameErrors?: string[];
  primaryErrors?: string[];
  mirrorErrors?: string[];
  metadataErrors?: string[];
  // Set when a mirror is the primary of another file system.
  needsSharedStore?: boolean;
  // Whether the local cache is on; write-back (write_ack: cache) needs it.
  cacheEnabled?: boolean;
}

const storeLabel = (store: StoreConfig) =>
  `${store.name || "(unnamed)"} · ${store.backend}`;

const layoutLabel = (layout: MetadataLayout): string =>
  layout === "document" ? "Single JSON document" : "Mirrored tree (default)";

const metadataDescription = (type: MetadataType): string => {
  if (type === "pinned_message") {
    return "The directory tree is kept as a JSON document pinned in a store's channel and copied to every file mirror. Never delete the pinned file or pin anything else in that channel. On a Discord store the tree has to fit one attachment; use a repository store for anything but small trees. The Store field picks the channel: the file system's primary store by default, or any other store -- a pinned copy in a Telegram store and one in a Discord store are two separate metadata stores this way.";
  }
  if (type === "github_repo") {
    return "The directory tree is kept in a GitHub repository, so it survives even the loss of every store. Recommended whenever mirrors are configured. Layout 'mirrored tree' stores one blob per directory and writes incrementally; layout 'single JSON document' rewrites one file on every push, the same shape the pinned-message store uses.";
  }
  return "The metadata is kept in a GitLab repository. Layout 'mirrored tree' (the default) stores it exactly like the GitHub repository store: one blob per directory, written incrementally. Layout 'single JSON document' rewrites one file on every push, the same shape the pinned-message store uses. Base URL points at a self-hosted GitLab; the default is gitlab.com.";
};

interface MetadataStoreEditorProps {
  entry: MetadataEntry;
  // The stores this file system may pin its metadata in: its primary and
  // its mirrors. The loader refuses any other store.
  fsStores: StoreConfig[];
  stores: StoreConfig[];
  onChange: (entry: MetadataEntry) => void;
}

function MetadataStoreEditor({
  entry,
  fsStores,
  stores,
  onChange,
}: MetadataStoreEditorProps) {
  const update = (patch: Partial<MetadataEntry>) =>
    onChange({ ...entry, ...patch });
  const updateGithub = (
    field: keyof MetadataEntry["github_repo"],
    value: string | number
  ) => update({ github_repo: { ...entry.github_repo, [field]: value } });
  const updateGitlab = (
    field: keyof MetadataEntry["gitlab_repo"],
    value: string | number
  ) => update({ gitlab_repo: { ...entry.gitlab_repo, [field]: value } });

  return (
    <Box sx={{ mb: 1 }}>
      <Box
        sx={{
          display: "flex",
          alignItems: "flex-start",
          gap: 1,
          mb: 1,
          flexWrap: "wrap",
        }}
      >
        <FormControl size="small" sx={{ minWidth: 200 }}>
          <InputLabel>Metadata Type</InputLabel>
          <Select
            value={entry.type}
            label="Metadata Type"
            onChange={(e) =>
              update({ type: e.target.value as MetadataType })
            }
          >
            <MenuItem value="pinned_message">Pinned Message</MenuItem>
            <MenuItem value="github_repo">GitHub Repository</MenuItem>
            <MenuItem value="gitlab_repo">GitLab Repository</MenuItem>
          </Select>
        </FormControl>
        {entry.type === "pinned_message" && (
          <FormControl size="small" sx={{ minWidth: 240 }}>
            <InputLabel>Store</InputLabel>
            <Select
              value={
                stores.some((s) => s.name === entry.store) ? entry.store : ""
              }
              label="Store"
              onChange={(e) => update({ store: e.target.value })}
            >
              <MenuItem value="">Primary store (default)</MenuItem>
              {fsStores.map((store) => (
                <MenuItem key={store.name} value={store.name}>
                  {storeLabel(store)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        )}
      </Box>
      {entry.type === "github_repo" && (
        <>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Repository"
              value={entry.github_repo.repo}
              onChange={(e) => updateGithub("repo", e.target.value)}
              helperText="Format: username/repository-name"
              required
              style={{ flex: 1 }}
            />
            <ConfigTextField
              label="Commit/Branch"
              value={entry.github_repo.commit}
              onChange={(e) => updateGithub("commit", e.target.value)}
              width={200}
            />
          </Box>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Access Token"
              value={entry.github_repo.access_token}
              onChange={(e) => updateGithub("access_token", e.target.value)}
              type="password"
              required
              style={{ flex: 1 }}
            />
            <Button
              variant="outlined"
              component="a"
              size="large"
              href="https://github.com/settings/personal-access-tokens/new"
              target="_blank"
              rel="noopener noreferrer"
              sx={{ textTransform: "none" }}
            >
              Get Token
            </Button>
          </Box>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <FormControl size="small" sx={{ minWidth: 240 }}>
              <InputLabel>Layout</InputLabel>
              <Select
                value={entry.github_repo.layout}
                label="Layout"
                onChange={(e) =>
                  updateGithub(
                    "layout",
                    e.target.value as MetadataLayout
                  )
                }
              >
                <MenuItem value="tree">{layoutLabel("tree")}</MenuItem>
                <MenuItem value="document">{layoutLabel("document")}</MenuItem>
              </Select>
            </FormControl>
            {entry.github_repo.layout === "document" && (
              <ConfigTextField
                label="File Path"
                value={entry.github_repo.file_path}
                onChange={(e) => updateGithub("file_path", e.target.value)}
                helperText="Where the JSON document lives; default: metadata.json"
                style={{ flex: 1 }}
              />
            )}
            {entry.github_repo.layout === "tree" && (
              <ConfigTextField
                label="Squash History At"
                value={String(entry.github_repo.squash_history_max_commits)}
                onChange={(e) =>
                  updateGithub(
                    "squash_history_max_commits",
                    Math.max(0, parseInt(e.target.value, 10) || 0)
                  )
                }
                helperText="Compact the commit history once it grows past this many commits (0 = off)"
                width={220}
              />
            )}
          </Box>
        </>
      )}
      {entry.type === "gitlab_repo" && (
        <>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Repository"
              value={entry.gitlab_repo.repo}
              onChange={(e) => updateGitlab("repo", e.target.value)}
              helperText="Format: group/project or a numeric project id"
              required
              style={{ flex: 1 }}
            />
            <ConfigTextField
              label="Branch"
              value={entry.gitlab_repo.branch}
              onChange={(e) => updateGitlab("branch", e.target.value)}
              width={200}
            />
          </Box>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Access Token"
              value={entry.gitlab_repo.access_token}
              onChange={(e) => updateGitlab("access_token", e.target.value)}
              helperText="Needs the api or write_repository scope"
              type="password"
              required
              style={{ flex: 1 }}
            />
            <Button
              variant="outlined"
              component="a"
              size="large"
              href="https://gitlab.com/-/user_settings/personal_access_tokens"
              target="_blank"
              rel="noopener noreferrer"
              sx={{ textTransform: "none" }}
            >
              Get Token
            </Button>
          </Box>
          <Box
            sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}
          >
            <ConfigTextField
              label="Base URL"
              value={entry.gitlab_repo.base_url}
              onChange={(e) => updateGitlab("base_url", e.target.value)}
              helperText="For a self-hosted GitLab; default: https://gitlab.com"
              style={{ flex: 1 }}
            />
            <FormControl size="small" sx={{ minWidth: 220 }}>
              <InputLabel>Layout</InputLabel>
              <Select
                value={entry.gitlab_repo.layout}
                label="Layout"
                onChange={(e) =>
                  updateGitlab(
                    "layout",
                    e.target.value as MetadataLayout
                  )
                }
              >
                <MenuItem value="tree">{layoutLabel("tree")}</MenuItem>
                <MenuItem value="document">{layoutLabel("document")}</MenuItem>
              </Select>
            </FormControl>
            {entry.gitlab_repo.layout === "document" && (
              <ConfigTextField
                label="File Path"
                value={entry.gitlab_repo.file_path}
                onChange={(e) => updateGitlab("file_path", e.target.value)}
                helperText="Where the JSON document lives; a document left by an older release is migrated from here too. Default: metadata.json"
                style={{ flex: 1 }}
              />
            )}
            {entry.gitlab_repo.layout === "tree" && (
              <ConfigTextField
                label="Blob Fetch Concurrency"
                value={String(entry.gitlab_repo.blob_fetch_concurrency)}
                onChange={(e) =>
                  updateGitlab(
                    "blob_fetch_concurrency",
                    Math.max(0, parseInt(e.target.value, 10) || 0)
                  )
                }
                helperText="Parallel blob reads during a full-tree sync; default: 8"
                width={220}
              />
            )}
            {entry.gitlab_repo.layout === "tree" && (
              <ConfigTextField
                label="Squash History At"
                value={String(entry.gitlab_repo.squash_history_max_commits)}
                onChange={(e) =>
                  updateGitlab(
                    "squash_history_max_commits",
                    Math.max(0, parseInt(e.target.value, 10) || 0)
                  )
                }
                helperText="Compact the commit history once it grows past this many commits (0 = off)"
                width={220}
              />
            )}
          </Box>
        </>
      )}
      <Typography variant="body2" color="text.secondary" sx={{ pl: 2 }}>
        {metadataDescription(entry.type)}
      </Typography>
    </Box>
  );
}

export function FilesystemField({
  filesystem,
  stores,
  onUpdate,
  onUpdateMetadata,
  onUpdateMetadataMirror,
  onAddMetadataMirror,
  onRemoveMetadataMirror,
  onDelete,
  nameErrors = [],
  primaryErrors = [],
  mirrorErrors = [],
  metadataErrors = [],
  needsSharedStore = false,
  cacheEnabled = false,
}: FilesystemFieldProps) {
  const namedStores = stores.filter((s) => s.name.trim() !== "");
  const mirrorCandidates = namedStores.filter(
    (s) => s.name !== filesystem.primary
  );
  const reuploadMirror = needsReupload(filesystem, namedStores);

  return (
    <Box sx={{ mb: 3, pb: 2, borderBottom: 1, borderColor: "divider" }}>
      <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 2 }}>
        <ConfigTextField
          label="File System Name"
          value={filesystem.name}
          onChange={(e) => onUpdate("name", e.target.value)}
          required
          error={nameErrors.length > 0}
          helperText={
            nameErrors.length > 0
              ? nameErrors.join("; ")
              : "Top-level directory over WebDAV and SFTP"
          }
          width={220}
        />
        <FormControl
          size="small"
          sx={{ minWidth: 220 }}
          error={primaryErrors.length > 0}
        >
          <InputLabel>Primary Store</InputLabel>
          <Select
            value={
              namedStores.some((s) => s.name === filesystem.primary)
                ? filesystem.primary
                : ""
            }
            label="Primary Store"
            onChange={(e) => onUpdate("primary", e.target.value)}
          >
            {namedStores.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {primaryErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {primaryErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
        {onDelete && (
          <IconButton
            color="error"
            onClick={onDelete}
            sx={{ mt: 0.5 }}
            size="small"
          >
            <Delete />
          </IconButton>
        )}
      </Box>

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Metadata
      </Typography>
      <MetadataStoreEditor
        entry={filesystem.metadata}
        fsStores={namedStores.filter((s) =>
          [filesystem.primary, ...filesystem.mirrors].includes(s.name)
        )}
        stores={namedStores}
        onChange={onUpdateMetadata}
      />

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Metadata Push
      </Typography>
      <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}>
        <FormControl size="small" sx={{ minWidth: 260 }}>
          <InputLabel>Write mode</InputLabel>
          <Select
            value={filesystem.metadata_push.mode}
            label="Write mode"
            onChange={(e) =>
              onUpdate("metadata_push", {
                ...filesystem.metadata_push,
                mode: e.target.value as MetadataPushMode,
              })
            }
          >
            <MenuItem value="coalesced">Coalesced (default)</MenuItem>
            <MenuItem value="inline">Inline</MenuItem>
          </Select>
        </FormControl>
        {filesystem.metadata_push.mode === "coalesced" && (
          <ConfigTextField
            label="Window (ms)"
            value={String(filesystem.metadata_push.window_ms)}
            onChange={(e) =>
              onUpdate("metadata_push", {
                ...filesystem.metadata_push,
                window_ms: Math.max(0, parseInt(e.target.value, 10) || 0),
              })
            }
            helperText="Pushes landing within the window share one write; default: 300"
            width={220}
          />
        )}
      </Box>
      <FormControlLabel
        label="Write the metadata stores in parallel (the write lasts as long as the slowest store instead of their sum)"
        control={
          <Checkbox
            checked={filesystem.metadata_push.parallel_stores}
            onChange={(e) =>
              onUpdate("metadata_push", {
                ...filesystem.metadata_push,
                parallel_stores: e.target.checked,
              })
            }
          />
        }
      />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, pl: 2 }}>
        A mutation pushes the metadata tree to the stores once it is done.
        Coalesced merges the push requests of a burst -- a folder deleted
        one file at a time -- into one write per window, so a slow mirror
        is paid once, not per file. A coalesced push that fails is logged
        and caught up by the heal instead of failing the operation.
      </Typography>

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Metadata Mirrors
      </Typography>
      {filesystem.metadata_mirrors.map((entry, index) => (
        <Box
          key={index}
          sx={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-start",
            gap: 1,
            mb: 1,
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
            p: 1,
          }}
        >
          <Box sx={{ flex: 1 }}>
            <MetadataStoreEditor
              entry={entry}
              fsStores={namedStores.filter((s) =>
                [filesystem.primary, ...filesystem.mirrors].includes(s.name)
              )}
              stores={namedStores}
              onChange={(updated) => onUpdateMetadataMirror(index, updated)}
            />
          </Box>
          <IconButton
            color="error"
            onClick={() => onRemoveMetadataMirror(index)}
            sx={{ mt: 0.5 }}
            size="small"
          >
            <Delete />
          </IconButton>
        </Box>
      ))}
      <Button
        startIcon={<Add />}
        onClick={onAddMetadataMirror}
        variant="outlined"
        size="small"
        sx={{ width: "fit-content", mb: 1 }}
      >
        Add Metadata Store
      </Button>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, pl: 2 }}>
        Every metadata store holds a full copy of the directory tree. A write
        goes to all of them; while one store is unreachable, reads and writes
        carry on with the others and the store is caught up once it returns.
        Two pinned-message stores on the same store are not allowed.
      </Typography>
      {metadataErrors.length > 0 && (
        <Typography variant="body2" color="error" sx={{ mb: 2, pl: 2 }}>
          {metadataErrors.map((error, index) => (
            <Box key={index}>{error}</Box>
          ))}
        </Typography>
      )}

      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Mirrors
      </Typography>
      <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 1, mb: 1 }}>
        <FormControl
          size="small"
          sx={{ flex: 1, minWidth: { xs: 0, sm: 300 } }}
          error={mirrorErrors.length > 0}
        >
          <InputLabel>Mirror Stores</InputLabel>
          <Select
            multiple
            value={filesystem.mirrors.filter((name) =>
              mirrorCandidates.some((s) => s.name === name)
            )}
            label="Mirror Stores"
            input={<OutlinedInput label="Mirror Stores" />}
            onChange={(e) =>
              onUpdate(
                "mirrors",
                (typeof e.target.value === "string"
                  ? e.target.value.split(",")
                  : e.target.value) as string[]
              )
            }
            renderValue={(selected) => (
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                {(selected as string[]).map((value) => (
                  <Chip key={value} label={value} size="small" />
                ))}
              </Box>
            )}
          >
            {mirrorCandidates.map((store) => (
              <MenuItem key={store.name} value={store.name}>
                {storeLabel(store)}
              </MenuItem>
            ))}
          </Select>
          {mirrorErrors.length > 0 && (
            <Typography variant="caption" color="error" sx={{ pl: 2 }}>
              {mirrorErrors.join("; ")}
            </Typography>
          )}
        </FormControl>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, pl: 2 }}>
        Every file lands in the primary and is copied to each mirror. A
        mirror in another backend, or with smaller messages, receives a
        re-uploaded copy. Swapping primary and a mirror later is a config
        change.
      </Typography>

      {filesystem.mirrors.length > 0 && (
        <>
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, mb: 1 }}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Copy Mode</InputLabel>
              <Select
                value={filesystem.mode}
                label="Copy Mode"
                onChange={(e) =>
                  onUpdate("mode", e.target.value as MirrorMode)
                }
              >
                <MenuItem value="auto">Auto (recommended)</MenuItem>
                <MenuItem value="forward">Forward only</MenuItem>
                <MenuItem value="reupload">Re-upload only</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Sync</InputLabel>
              <Select
                value={filesystem.sync}
                label="Sync"
                onChange={(e) => onUpdate("sync", e.target.value as SyncMode)}
              >
                <MenuItem value="inline">Inline (during the upload)</MenuItem>
                <MenuItem value="background">Background queue</MenuItem>
                <MenuItem value="tee">Tee (every store at once)</MenuItem>
              </Select>
            </FormControl>
          </Box>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mb: 1, pl: 2 }}
          >
            &quot;Auto&quot; copies server-side between Telegram channels
            (no bandwidth) and re-uploads where that is impossible.
            &quot;Inline&quot; makes an upload wait for its copies;
            &quot;Background&quot; queues the file and a worker copies it
            afterwards; &quot;Tee&quot; feeds a re-uploading mirror from
            the upload itself, so primary and mirror fill at the same time
            without a read-back or a cache, and the upload runs at the pace
            of the slowest store. Background is preselected as soon as a
            mirror re-uploads, since a client would otherwise time out
            waiting for a large upload to pass through the mirror a second
            time. The server picks the same default when the config leaves
            Sync out, so it is only written when your choice differs.
            {reuploadMirror && filesystem.sync === "inline" && (
              <>
                {" "}
                <strong>
                  One of the mirrors re-uploads every byte; switch Sync to
                  Background unless uploads may wait for it.
                </strong>
              </>
            )}
          </Typography>
          <FormControlLabel
            label="Read from every store in parallel (pieces of one download are shared between the stores)"
            control={
              <Checkbox
                checked={filesystem.read_parallel}
                onChange={(e) => onUpdate("read_parallel", e.target.checked)}
              />
            }
          />
          {filesystem.read_parallel && (
            <>
              <FormControl size="small" sx={{ minWidth: { xs: "100%", sm: 300 }, mb: 1 }}>
                <InputLabel>Stores taking part (empty: all)</InputLabel>
                <Select
                  multiple
                  value={filesystem.read_sources.filter((name) =>
                    [filesystem.primary, ...filesystem.mirrors].includes(name)
                  )}
                  label="Stores taking part (empty: all)"
                  input={<OutlinedInput label="Stores taking part (empty: all)" />}
                  onChange={(e) =>
                    onUpdate(
                      "read_sources",
                      (typeof e.target.value === "string"
                        ? e.target.value.split(",")
                        : e.target.value) as string[]
                    )
                  }
                  renderValue={(selected) => (
                    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.5 }}>
                      {(selected as string[]).map((value) => (
                        <Chip key={value} label={value} size="small" />
                      ))}
                    </Box>
                  )}
                >
                  {namedStores
                    .filter((s) =>
                      [filesystem.primary, ...filesystem.mirrors].includes(s.name)
                    )
                    .map((store) => (
                      <MenuItem key={store.name} value={store.name}>
                        {storeLabel(store)}
                      </MenuItem>
                    ))}
                </Select>
              </FormControl>
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ mb: 1, pl: 2 }}
              >
                Only pays off with more than one store holding the file: a
                fast store simply takes more pieces than a slow one. Small
                reads stay on a single store. Leave a Discord mirror out to
                spare its rate limit for bulk reads.
              </Typography>
            </>
          )}
          {cacheEnabled && (
            <>
              <FormControl size="small" sx={{ minWidth: { xs: "100%", sm: 260 }, mb: 1 }}>
                <InputLabel>Write acknowledgement</InputLabel>
                <Select
                  value={filesystem.write_ack}
                  label="Write acknowledgement"
                  disabled={filesystem.strict}
                  onChange={(e) =>
                    onUpdate("write_ack", e.target.value as WriteAck)
                  }
                >
                  <MenuItem value="primary">
                    When the primary store has the file
                  </MenuItem>
                  <MenuItem value="cache">
                    When the local cache has the file (write-back)
                  </MenuItem>
                </Select>
              </FormControl>
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ mb: 1, pl: 2 }}
              >
                Write-back answers an upload as soon as it is on the local
                disk; a worker then fills the primary and every mirror at
                once from that copy. Until the primary has it, the file
                exists only on this host: a lost disk in that window loses
                the version. Leave it on &quot;primary&quot; for a
                single-disk host holding the only copy of irreplaceable
                data. Not available with Strict.
              </Typography>
            </>
          )}
          <FormControlLabel
            label="Strict: fail an upload when a mirror copy fails (needs inline sync)"
            control={
              <Checkbox
                checked={filesystem.strict}
                disabled={filesystem.sync !== "inline"}
                onChange={(e) => onUpdate("strict", e.target.checked)}
              />
            }
          />
          {needsSharedStore && (
            <FormControlLabel
              label="Allow a mirror that is the primary of another file system"
              control={
                <Checkbox
                  checked={filesystem.allow_shared_store}
                  onChange={(e) =>
                    onUpdate("allow_shared_store", e.target.checked)
                  }
                />
              }
            />
          )}
        </>
      )}
    </Box>
  );
}
