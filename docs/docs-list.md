---
summary: "Docs list script behavior and expected front matter."
read_when:
  - When updating docs list tooling or doc front matter
---

# Docs list script

The docs list script (`docs/docs-list.ts`) prints a summary of every markdown file under `docs/`, skipping hidden entries plus `archive/` and `research/`. Run it with `npm run docs:list` or `npx tsx docs/docs-list.ts`.

## Expected front matter

Each `docs/**/*.md` file must start with:

```md
---
summary: "One-line summary"
read_when:
  - When this doc should be read
---
```

`read_when` is optional and can be a bullet list or an inline array.

## What happens on missing metadata

If a file is missing front matter or a non-empty `summary`, the script still lists it but appends an error label:

- `missing front matter`
- `unterminated front matter`
- `summary key missing`
- `summary is empty`
