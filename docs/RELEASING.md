# Releasing LayerSmith

Releases are cut by pushing a tag. GitHub Actions does the rest.

## Everyday work

Pushing or merging to `main` runs the tests and publishes a development image:

```
ghcr.io/r0lfi/layersmith:edge
```

`edge` is never `latest`. It exists so you can try what is on `main` without
waiting for a release.

## Cutting a release

```bash
# 1. Make sure main is green and the version is what you want
git switch main && git pull

# 2. Tag it
git tag v0.1.0
git push origin v0.1.0
```

That is the whole process. The `container.yml` workflow then:

1. builds the image for `linux/amd64` and `linux/arm64`,
2. stamps `VERSION` and `REVISION` into the image and its OCI labels,
3. pushes these tags:

```
ghcr.io/r0lfi/layersmith:0.1.0
ghcr.io/r0lfi/layersmith:0.1
ghcr.io/r0lfi/layersmith:0
ghcr.io/r0lfi/layersmith:latest
```

### Prereleases

```bash
git tag v0.2.0-rc1
git push origin v0.2.0-rc1
```

publishes **only** `ghcr.io/r0lfi/layersmith:0.2.0-rc1`. No `0.2`, no `0`, and
`latest` is left alone.

## Versioning

Semantic versioning, and the version is never written into the source: it
comes from the tag through the `VERSION` build argument, and shows up in
**Settings → About**, in `/api/health` and in the build manifest of every
image produced by that release.

A source checkout reports `0.0.0-dev`, so a locally built image cannot pretend
to be a release.

## Package visibility

A package published by Actions from a public repository inherits that
visibility, so `ghcr.io/r0lfi/layersmith` was pullable anonymously right
after the first push. Verified with an unauthenticated client.

Check it yourself from a machine that is not logged in to ghcr.io:

```bash
podman pull ghcr.io/r0lfi/layersmith:edge
```

If that ever asks for credentials, the package is private: open
`https://github.com/users/r0lfi/packages/container/package/layersmith`,
then **Package settings → Danger Zone → Change visibility → Public**, and
under **Manage Actions access** confirm the repository has `write`.

## Checklist before tagging

- [ ] CI green on `main`
- [ ] `README.md` matches what the release actually does
- [ ] `docs/deployment.md` limitations still accurate
- [ ] Screenshots in `docs/screenshots/` still show the current UI
- [ ] Upgrade from the previous release tested with an existing `/data` volume

## Release artifacts

The container image is the distribution. Source archives come from GitHub
automatically. Large image TARs are deliberately not attached to releases; an
air-gap bundle of LayerSmith itself can be added later if there is demand.
