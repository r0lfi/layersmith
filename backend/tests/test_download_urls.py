"""Download URLs for wget and curl: resumable parts, the build's file list and download-all.sh."""

import hashlib

from tests.test_api import client, run_pending_build  # noqa: F401 - fixture
from tests.test_splitting import PART, make_ready_build, original, split


def test_split_part_honours_range_so_wget_c_can_resume(client):
    build_id = make_ready_build(client, int(1.5 * PART))
    status = split(client, build_id)
    part = status["files"][1]  # the second part: offset PART into the archive
    url = f"/api/builds/{build_id}/split/{part['name']}"
    data = original(client, build_id)[PART:]

    whole = client.get(url)
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes"
    assert whole.content == data

    tail = client.get(url, headers={"Range": "bytes=100-"})
    assert tail.status_code == 206 and tail.content == data[100:]
    assert tail.headers["content-range"] == f"bytes 100-{len(data) - 1}/{len(data)}"
    assert int(tail.headers["content-length"]) == len(data) - 100

    assert client.get(url, headers={"Range": "bytes=10-19"}).content == data[10:20]
    assert client.get(url, headers={"Range": "bytes=-5"}).content == data[-5:]
    # wget -c on a file that is already complete asks for the byte after the end.
    done = client.get(url, headers={"Range": f"bytes={len(data)}-"})
    assert done.status_code == 416 and done.headers["content-range"] == f"bytes */{len(data)}"


def test_whole_archive_download_supports_range_too(client):
    build_id = make_ready_build(client, 4096)
    partial = client.get(f"/api/builds/{build_id}/download/export", headers={"Range": "bytes=1000-"})
    assert partial.status_code == 206 and partial.content == original(client, build_id)[1000:]


def test_split_archive_has_a_download_all_script(client):
    build_id = make_ready_build(client, int(1.5 * PART))
    status = split(client, build_id)
    response = client.get(f"/api/builds/{build_id}/split/download-all.sh",
                          headers={"x-forwarded-proto": "https", "x-forwarded-host": "layersmith.example.test"})
    assert response.status_code == 200
    script = response.text
    assert "https://layersmith.example.test" in script
    assert "bash assemble-image.sh --load" in script and "receiving host" in script and "GPU host" not in script
    for item in status["files"]:
        assert f"/api/builds/{build_id}/split/{item['name']}|{item['name']}|" in script


def test_build_files_lists_every_download_with_its_url(client):
    build_id = make_ready_build(client, int(1.5 * PART))
    build = client.get(f"/api/builds/{build_id}").json()
    archive = build["export_name"]
    assert archive == "gpu-benchmark-1.0.0.tar"

    before = client.get(f"/api/builds/{build_id}/files").json()["files"]
    assert [f["name"] for f in before] == [archive, f"{archive}.sha256"]
    assert before[0]["url"] == f"/api/builds/{build_id}/download/export"
    assert before[0]["sha256"] == hashlib.sha256(original(client, build_id)).hexdigest()

    status = split(client, build_id)
    after = client.get(f"/api/builds/{build_id}/files").json()["files"]
    split_files = [f for f in after if f["group"] == "Split archive"]
    assert [f["name"] for f in split_files] == [f["name"] for f in status["files"]] + ["download-all.sh"]
    for item in after:  # every listed URL answers
        assert client.get(item["url"]).status_code == 200, item


def test_build_files_of_a_missing_build_is_404(client):
    assert client.get("/api/builds/nope/files").status_code == 404
