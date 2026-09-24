# Security policy

## Supported versions

LayerSmith is pre-1.0. Fixes go to the newest release; there are no long-term
support branches yet.

| Version | Supported |
| --- | --- |
| 0.1.x | yes |
| older | no |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting instead:
[Security → Report a vulnerability](https://github.com/r0lfi/layersmith/security/advisories/new).
It is private to the maintainers until an advisory is published.

Useful in a report: what an attacker can do, the steps to reproduce it, the
affected version and deployment shape (container with a socket mount, or
directly on a host), and anything you already know about a fix.

Expect an acknowledgement within a week. Since this is a spare-time project,
please allow reasonable time for a fix before disclosing publicly.

## What is in scope

LayerSmith builds container images from user input, so the interesting areas
are:

- command injection through project, package, path or image-reference values
- path traversal in uploads, build contexts, downloads or static file serving
- escaping the build context, or reading host files that a build should not see
- authentication bypass, once authentication exists
- a build reading or exfiltrating another project's data
- secrets ending up in an image, a log or a build record

## What LayerSmith checks about itself

Every push, pull request and weekly on a schedule
([`.github/workflows/security.yml`](.github/workflows/security.yml)):

- **Dependencies** — `pip-audit` over the installed Python packages, and
  `npm audit` over the frontend's production dependencies.
- **Secrets** — the repository *and its whole history*, because a credential
  deleted in a later commit is still in the repository.
- **The application image** — built, exported and scanned as a file. The
  scanner gets an archive, not the Docker socket, exactly as LayerSmith does
  it for the images its users build. A CRITICAL or HIGH *with a fix
  available* fails the job; everything else is reported, including what has
  no fix yet, so nothing is hidden and nothing fails over something no one
  can act on.

A CycloneDX SBOM of the application image is published as a build artifact.

## Scanning the images you build

Optional, off until you configure it, and it reports rather than blocks —
see [docs/scanning.md](docs/scanning.md). An image LayerSmith has not scanned
is reported as *unknown*, never as clean.

## What is known, and not a vulnerability

These are documented design limitations of 0.1.x, not findings:

- **No authentication, by design.** LayerSmith is meant to run locally, on a
  machine or network you trust. Anyone who can reach it can use it; front it
  with an authenticating proxy if you need logins.
- **A build runs code you supply.** Advanced mode runs your Containerfile and
  GUI mode runs your build scripts. That is the product.
- **The mounted runtime socket is powerful.** Whoever can reach LayerSmith can
  run containers as the socket's owner. `docs/deployment.md` describes
  configurations that isolate the builder.
- **No quotas and no garbage collection.** A user can fill the data volume.
- **A scan is a snapshot.** It records what one vulnerability database knew
  on one date. An image scanned clean last month is not clean today, and
  LayerSmith never labels an image "secure".

If you can get past a boundary the documentation claims to hold, that *is* a
vulnerability and we want to hear about it.
