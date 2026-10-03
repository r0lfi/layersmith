#!/usr/bin/env bash
# Build the LayerSmith terminal client release artifacts.
#
#   client/scripts/build-release.sh 0.4.0 [OUTPUT_DIR]
#
# Produces, in OUTPUT_DIR (default: client/dist):
#
#   layersmith_client-<version>-py3-none-any.whl
#   layersmith-client-<version>-offline.tar.gz   the wheel plus every
#       dependency for Python 3.9-3.13, an install note and SHA256SUMS
#   SHA256SUMS
#
# The version is stamped into a temporary copy of the source, never into the
# checkout (see layersmith_client/_version.py). Building needs network access
# to PyPI and uv on PATH; installing from the offline archive needs neither.
set -euo pipefail

VERSION="${1:?usage: build-release.sh VERSION [OUTPUT_DIR]}"
VERSION="${VERSION#v}"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-z0-9.+-]*)?$ ]]; then
    echo "error: version must look like 1.2.3 or v1.2.3, not $VERSION" >&2
    exit 2
fi
CLIENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${2:-$CLIENT_DIR/dist}" && cd "${2:-$CLIENT_DIR/dist}" && pwd)"
PYTHON="${PYTHON:-python3}"
# Python versions the offline archive is tested for. Every dependency is a
# pure-Python wheel; the check below fails the build if that ever changes.
PYTHON_VERSIONS="${PYTHON_VERSIONS:-3.9 3.10 3.11 3.12 3.13}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Stamping version $VERSION into a copy of the source"
cp -r "$CLIENT_DIR" "$WORK/src"
rm -rf "$WORK/src/dist" "$WORK/src/build" "$WORK/src"/*.egg-info
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$WORK/src/layersmith_client/_version.py"

echo "==> Building the wheel"
"$PYTHON" -m pip wheel --quiet --no-deps --wheel-dir "$WORK/wheel" "$WORK/src"
WHEEL="$(ls "$WORK/wheel"/layersmith_client-*.whl)"

echo "==> Collecting dependencies for Python $PYTHON_VERSIONS"
BUNDLE="layersmith-client-$VERSION-offline"
mkdir -p "$WORK/$BUNDLE/wheelhouse"
cp "$WHEEL" "$WORK/$BUNDLE/wheelhouse/"
# pip evaluates dependency markers (python_version < "3.11" and so on) for
# the interpreter running it, not for --python-version, so it would leave out
# what only older Pythons need. uv resolves for the target version properly;
# pip then downloads exactly those pins.
command -v uv >/dev/null || { echo "error: uv is required to resolve per Python version (pip install uv)" >&2; exit 1; }
for version in $PYTHON_VERSIONS; do
    uv pip compile --quiet --python-version "$version" --python-platform linux --no-header \
        --output-file "$WORK/requirements-$version.txt" "$WORK/src/pyproject.toml"
    "$PYTHON" -m pip download --quiet --no-deps --only-binary=:all: --python-version "$version" \
        --dest "$WORK/$BUNDLE/wheelhouse" -r "$WORK/requirements-$version.txt"
done

# pipx refreshes pip in its shared environment through the same --no-index
# options, so an offline pipx install needs a pip wheel here too.
"$PYTHON" -m pip download --quiet --no-deps --only-binary=:all: --dest "$WORK/$BUNDLE/wheelhouse" pip

echo "==> Checking that every wheel is platform independent"
if ls "$WORK/$BUNDLE/wheelhouse" | grep -v -- '-none-any\.whl$'; then
    echo "error: the wheels above are platform specific; the offline archive would not be universal" >&2
    exit 1
fi

cat > "$WORK/$BUNDLE/INSTALL.txt" <<'EOF'
LayerSmith terminal client @VERSION@ - offline installation

Needs: Python 3.9 or newer with the venv module (or pipx) on the target
machine. No network access, no LayerSmith server, Docker or Podman needed
on this machine. Built for Python @PYTHONS@; every wheel here is pure
Python (py3-none-any).

1. Check the archive, in this directory:

     sha256sum -c SHA256SUMS

2a. With pipx:

     pipx install --pip-args="--no-index --find-links=$PWD/wheelhouse" layersmith-client

2b. Or in a virtual environment of your own:

     python3 -m venv ~/.local/share/layersmith-client
     ~/.local/share/layersmith-client/bin/pip install --no-index --find-links wheelhouse layersmith-client
     mkdir -p ~/.local/bin
     ln -s ~/.local/share/layersmith-client/bin/layersmith ~/.local/bin/layersmith

3. Point it at your server and check:

     layersmith config set server https://layersmith.example.org
     layersmith doctor

Documentation: docs/client.md in the LayerSmith repository.
EOF
sed -i "s/@VERSION@/$VERSION/; s/@PYTHONS@/${PYTHON_VERSIONS// /, }/" "$WORK/$BUNDLE/INSTALL.txt"

(cd "$WORK/$BUNDLE" && sha256sum wheelhouse/*.whl INSTALL.txt > SHA256SUMS)
tar -C "$WORK" -czf "$OUT/$BUNDLE.tar.gz" "$BUNDLE"
cp "$WHEEL" "$OUT/"
(cd "$OUT" && sha256sum "$(basename "$WHEEL")" "$BUNDLE.tar.gz" > SHA256SUMS)

echo "==> Done"
ls -l "$OUT"
cat "$OUT/SHA256SUMS"
