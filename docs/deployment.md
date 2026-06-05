# Deployment

The canonical public docs live at:

```text
https://docs.thalovant.com/developers/sdks/python/
```

This MkDocs site is generated API reference material for maintainers and release
checks.

## Local Preview

Install the docs dependencies:

```bash
pip install -e ".[docs]"
```

Run a local preview server:

```bash
mkdocs serve
```

Build the site exactly as CI does:

```bash
mkdocs build --strict
```

The generated site is written to `site/`.

## GitHub Pages

The repository includes `.github/workflows/docs.yml`. On every push to `main`,
the workflow:

1. Installs the package with documentation dependencies.
2. Runs `mkdocs build --strict`.
3. Uploads the generated static site as a Pages artifact.
4. Deploys it to GitHub Pages.

In GitHub repository settings, set Pages source to **GitHub Actions**.

The current public docs URL in `mkdocs.yml` is:

```text
https://docs.thalovant.com/developers/sdks/python/
```

If the public docs route changes later, update `site_url` in `mkdocs.yml`.
