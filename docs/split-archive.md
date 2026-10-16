# Split image archives

Some environments refuse any single file of 1 GB or more: a mail gateway, a
file-transfer portal, a USB policy. An exported image is often several GB. The
build page's **Image archive** section therefore offers **Download split
archive** next to **Download TAR**: the same archive, cut into parts of at most
900 MB (900 000 000 bytes), plus a script that puts it back together.

Nothing about the image changes. There is no compression and no conversion:
the parts concatenated in order are byte for byte the `podman save` /
`docker save` archive, with the same sha256, and `podman load -i` on the
restored file gives the same layers, metadata and tags.

## What you download

For `gpu-benchmark-1.0.0.tar` (about 4.6 GB):

```
gpu-benchmark-1.0.0.tar.part-000      900 MB
gpu-benchmark-1.0.0.tar.part-001      900 MB
gpu-benchmark-1.0.0.tar.part-002      900 MB
gpu-benchmark-1.0.0.tar.part-003      900 MB
gpu-benchmark-1.0.0.tar.part-004      900 MB
gpu-benchmark-1.0.0.tar.part-005      ~100 MB
assemble-image.sh                     restores and verifies
gpu-benchmark-1.0.0.tar.sha256        checksum of the whole archive
gpu-benchmark-1.0.0.parts.sha256      checksum of every part
```

Every file is under 1 GB. The page shows the part size and the number of parts
before you start, and each file with its size, checksum and its own download
button afterwards.

## On the receiving host

Put every file in one directory, then:

```sh
chmod +x assemble-image.sh
./assemble-image.sh
```

```
Checking archive parts... OK (6 parts)
Verifying checksums... OK
Reassembling image archive... OK
Verifying original archive... OK

Image archive restored successfully!

File: gpu-benchmark-1.0.0.tar
...
Load image using:
podman load -i gpu-benchmark-1.0.0.tar
```

The script needs bash and coreutils (`cat`, `sha256sum`, `df`), nothing else:
no LayerSmith, Python or container runtime. Every expected part name, size and
checksum is written into it, so it does not depend on the `.sha256` files.

| Option | Effect |
| --- | --- |
| `--load` | after restoring, run `podman load -i` (or `docker load -i` when there is no podman) |
| `--remove-parts` | delete the parts once the archive is restored and verified |
| `--force` | replace an existing archive of the same name that does not match |
| `--dir DIR` | where the parts are and the archive goes (default: the script's directory) |

What it checks, in order: every part is present and has the expected size;
every part's sha256; that there is room for the archive; then it writes the
archive to a temporary name while hashing it, compares size and sha256 with
the original and only then renames it. A missing, short or corrupt part is
named, and nothing is written. An interrupted run removes its temporary file.
Running it again after success verifies the existing archive and stops.

Exit codes: `0` ok, `1` usage, `2` missing or incomplete part, `3` corrupt
part or mismatch, `4` not enough space or a write error, `5` an existing
archive differs, `6` `--load` failed.

Without the script, the same thing by hand:

```sh
sha256sum -c gpu-benchmark-1.0.0.parts.sha256
cat gpu-benchmark-1.0.0.tar.part-* > gpu-benchmark-1.0.0.tar
sha256sum -c gpu-benchmark-1.0.0.tar.sha256
podman load -i gpu-benchmark-1.0.0.tar
```

## How the server does it

The parts are not written to disk. A part is a byte range of the export
LayerSmith already has; downloading it streams that range. So splitting uses
no extra space, leaves no temporary files behind, and the plain TAR download
keeps serving the very same file.

What splitting does compute is a checksum per part, which means reading the
archive once (a few seconds per GB). That runs in the background, with a
progress bar on the page. The same read recomputes the whole archive's sha256;
if it no longer matches the checksum recorded at export time, splitting stops
and says so instead of describing a damaged file. The result is stored on the
build in one step at the end, so an interrupted or failed run changes nothing
and can simply be started again. If the archive is replaced or removed later,
the stored split no longer applies and the page offers to split again.

API: `GET /api/builds/{id}/split` (plan, progress or the file list),
`POST /api/builds/{id}/split` (start; returns at once), and
`GET /api/builds/{id}/split/{file}` for each listed file. `/api/health` lists
`split-archive` in `api_features`.

`LAYERSMITH_SPLIT_PART_BYTES` sets a smaller part size, between 1 MB and the
900 MB maximum; larger values are capped.
