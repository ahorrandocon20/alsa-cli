# Contributing

Thanks for helping. This project follows a simple flow: issue first, then a pull request that closes it.

## 1. Open an issue first

Use the Bug or Feature form and wait for a reply before writing code, so the change is agreed before you spend time on it. Typos and small documentation fixes can go straight to a pull request.

## 2. Set up

Fork the repo, create a branch with any name, and install the project with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run alsa --help
```

## 3. Make the change

- Keep tests offline, with fake or trimmed responses: no requests to alsa.es.
- Scripts and agents read the JSON, so add fields rather than renaming or removing them.
- Never commit real passenger or booking data, HAR captures, tickets or tokens.

Before pushing:

```sh
uv run pytest -q
uvx ruff check .
```

## 4. Open the pull request

- Title in [Conventional Commits](https://www.conventionalcommits.org/) form: `type: description`, e.g. `fix: reject past dates`. Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `style`, `revert`.
- Body starts with `Closes #N`, the issue it solves.
- CI checks the title, ruff and the tests. It must be green to merge.

Pull requests are squash-merged: the title becomes the commit on `main`, so the commits inside your branch can have any message.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
