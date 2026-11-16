# Factory backlog (`/factory backlog …`)

Names (`S`, `REPO`, `H`) are as in `SKILL.md` §0. The backlog is `H/backlog.md`, committed with
the repo. The backlog commands need no dashboard, no run setup and no worker. They never commit:
after a change, tell the user that `.factory/backlog.md` is modified and is theirs to commit.
Every call is `FACTORY_HOME=H python3 S/factory_backlog.py …`.

An item is one line: `- [ ] <feature text> [after:<job-id>] [default:<branch>] [wi:<n>]
[install:"<command>"]`. A claim appends ` → <job-id>`, and `finish` appends the job's stage.
The state box is `[ ]` todo, `[~]` claimed, `[x]` done (merged, shipped, closed) or
`[!]` stopped (held, blocked, failed). Item numbers count the item lines from 1 in file order;
headings and notes in the file are kept but not numbered.

| Command | Run | Then |
|---|---|---|
| `/factory backlog` or `/factory backlog status [--repo <path>]` | `list` prints `{repo, items: [{n, state, feature, after, default, install, work_item, job_id, stage}]}` | Show a table: n, state, feature, tokens, job and stage. For a claimed item, add `factory_job.py show <id>`'s stage when it differs. No items: say so, and suggest `add` |
| `/factory backlog add <feature …> [--repo <path>]` | `add "<feature …>"` prints the new item number | Show the item as `list` prints it |
| `/factory backlog edit <n> <new text …>` | `edit <n> "<new text …>"` | Only `todo` items can be edited. Exit 2 (claimed, done, stopped, no such item, or text ending in a job marker): report it |
| `/factory backlog remove <n>` | Show the item and ask once: "Remove item <n>: <feature>? (y/n)". On yes: `remove <n>` | A claimed item is refused: finish or close its job first. Removing a done or stopped item drops only the line; the job folder stays |

Pass the text through unchanged, tokens included. Item numbers shift after a remove, so list
the backlog again before using a number a second time.
