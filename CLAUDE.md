# CLAUDE.md

## Build & Run

- Python 3.12+
- Entry points: `git_ctrl/example.py` (demo), `git_ctrl/project_init.py` (batch init)
- No test suite yet

## Key Notes

- `APP_NAME` in `app_settings.py` is the single source of truth for the application name (`RegisterEditor`). All paths, prefixes, and project filters derive from it — never hardcode the name elsewhere.
- Admin projects in `_GERRIT_ADMIN_PROJECTS` are handled with an early `continue` in filter logic, so they bypass the prefix check entirely. A project in the admin set will never be matched by `startswith(_GERRIT_PROJECT_PREFIX)`.
- `project_init.py` `commit_and_push_changes()` is live — it will actually push to Gerrit remotes. The calls were previously commented out during development.
- When `pull` fails in `project_init.py`, the project is skipped entirely (no fallback branch). This is intentional to avoid writing to repos in an unknown state.
