# Deployment

The documentation website is built with MkDocs Material and published as a
static GitHub Pages site.

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

The current GitHub Pages URL in `mkdocs.yml` is:

```text
https://thalovant.github.io/thalovant-python-sdk/
```

If a custom docs domain is added later, update `site_url` in `mkdocs.yml`.
