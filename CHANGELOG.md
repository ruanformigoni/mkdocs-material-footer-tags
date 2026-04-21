# Changelog

## [Unreleased]

## [0.1.0] - 2026-04-21

### Added
- Jaccard-ranked Related pages section injected into each tagged MkDocs page at build time
- Nav breadcrumbs, bold linked title, truncated description, and tag pills per entry
- `md-tag--matched` CSS class on tags shared with the current page, rendered in the theme accent color via inline `<style>` injection
- Auto-detection of the Material tags-index page via the `<!-- material/tags -->` marker for correct pill anchor URLs
- Configuration options: `max_pages`, `min_shared_tags`, `heading`, `heading_level`, `show_summary`, `show_tags`, `show_breadcrumb`, `summary_max_chars`, `summary_ellipsis`
