"""A tar archive of a folder that is never written to disk.

A model folder can be 70 GB or more. Packing it into a tar before splitting
it would need that much space again, so the tar is described instead: a list
of segments (header blocks, file contents, padding) whose concatenation is a
standard, uncompressed POSIX (pax) archive. Any byte range of it can be read
straight from the files, which is all a split download needs, and on the GPU
host `cat parts | tar -x` unpacks it with no copy either.

The layout is deterministic: fixed owner, mode and mtime, files in the order
given. The same folder always gives the same bytes, so checksums computed
once stay valid for as long as the files are unchanged.
"""

import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

BLOCK = 512
READ_CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class Segment:
    offset: int
    size: int
    data: bytes | None = None   # header or padding
    path: Path | None = None    # file content


class Layout:
    def __init__(self, segments: list[Segment]):
        self.segments = segments
        self.size = segments[-1].offset + segments[-1].size if segments else 0

    def read(self, offset: int = 0, length: int | None = None):
        """Yield the bytes [offset, offset + length) of the archive."""
        end = self.size if length is None else min(self.size, offset + length)
        for segment in self.segments:
            seg_end = segment.offset + segment.size
            if seg_end <= offset or segment.size == 0:
                continue
            if segment.offset >= end:
                break
            start = max(offset, segment.offset) - segment.offset
            stop = min(end, seg_end) - segment.offset
            if segment.data is not None:
                yield segment.data[start:stop]
                continue
            with open(segment.path, "rb") as handle:
                handle.seek(start)
                remaining = stop - start
                while remaining > 0:
                    chunk = handle.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise OSError(f"{segment.path.name} is shorter than recorded; the model changed on disk")
                    remaining -= len(chunk)
                    yield chunk


def _header(info: tarfile.TarInfo) -> bytes:
    return info.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8", errors="strict")


def _info(name: str, kind: bytes, size: int, mode: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.mode, info.mtime = kind, size, mode, mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def layout(folder: Path, top: str, files: list[dict], mtime: int) -> Layout:
    """Describe `top/` plus each of `files` ([{"name", "size"}]) from `folder`."""
    segments: list[Segment] = []
    position = 0

    def add(**fields):
        nonlocal position
        segment = Segment(offset=position, **fields)
        segments.append(segment)
        position += segment.size

    head = _header(_info(top + "/", tarfile.DIRTYPE, 0, 0o755, mtime))
    add(size=len(head), data=head)
    for entry in files:
        size = int(entry["size"])
        head = _header(_info(f"{top}/{entry['name']}", tarfile.REGTYPE, size, 0o644, mtime))
        add(size=len(head), data=head)
        add(size=size, path=Path(folder) / entry["name"])
        padding = (-size) % BLOCK
        if padding:
            add(size=padding, data=bytes(padding))
    # End of archive: two zero blocks.
    add(size=2 * BLOCK, data=bytes(2 * BLOCK))
    return Layout(segments)


def as_bytes(lay: Layout) -> bytes:
    """The whole archive in memory; for tests of small folders only."""
    buffer = io.BytesIO()
    for chunk in lay.read():
        buffer.write(chunk)
    return buffer.getvalue()
