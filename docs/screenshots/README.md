# Screenshots

Used by the project README.

| File | Shows |
| --- | --- |
| `dashboard.png` | Dashboard with stats, quick-build tiles and recent builds |
| `create-image.png` | Create image wizard, ideally the review step with the generated Containerfile |
| `build.png` | A build page with the live log |
| `image-details.png` | A finished build: image identity, packages and downloads |
| `security.png` | The Security panel on a build: severity counts, filters and findings |
| `training-purpose.png` | LLM Training: purpose questions, recommendation and profile cards |
| `training-software.png` | LLM Training: pinned base image and the tools included, with versions |
| `training-review.png` | LLM Training: review step with versions and the generated Containerfile |
| `training-build.png` | A training image build: the checks run inside the image |
| `training-getting-started.png` | Getting started for the exact image: host directories and run commands |
| `settings-security.png` | Settings -> Security: which scanner is in use and how old its data is |
| `tui-projects.png` | Terminal client: Projects with the selected project's details and build log |
| `tui-dashboard.png` | Terminal client: Dashboard with key figures, recent builds and server health |

Guidelines:

- Crop to the LayerSmith window. No browser chrome, bookmarks, tabs or URL bar.
- Use demo data. No internal hostnames, project names, IP addresses or paths.
- Keep the dark theme, at a width around 1280–1600 px.
- Terminal client images come from `client/scripts/tui_screenshots.py`, which
  runs the real TUI against a throwaway server with demo data and the
  address `https://layersmith.example.org`
  (`--sizes 160x48 --views projects,dashboard --select net-tools`), turned
  into PNG on a fixed character grid by `client/scripts/svg_grid_png.py`.
