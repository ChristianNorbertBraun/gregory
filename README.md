# Gregory

An expert that your other agents may ask. A chat agent sends a prompt over MCP; a stronger model
behind a configurable backend answers with text. Gregory runs as its own unprivileged unix user,
holds the model's token, and has **no access to anything else**: what a request may use is what
the caller names, and it is gone when the answer is back.

Named after Peter Gregory (Silicon Valley): odd insights that turn out to be right.

```
chat agent ── gregory_ask(prompt, skill, files) ──▶ Gregory (unix user gregory, 127.0.0.1:8768)
                                                      │  copies the named skill and files into a fresh directory
                                                      ▼
                                                    model in a sandbox: that directory read-only, web, nothing else
chat agent ◀──────────────── text ────────────────────┘
```

## What a request can carry

| Part | What it is | Where it comes from |
|---|---|---|
| `prompt` | The whole task. The expert knows nothing about the caller's conversation. | The caller |
| `skill` | A folder in the common `SKILL.md` format: rules, style, examples. The expert follows it but does not carry out its workflow. | The skills repository, if the caller is allowed to name it |
| `files` | Files or folders for this one request: long text, images, tool output. | The caller's own hand-over folder |
| web addresses | Just write them into the prompt. | The model reads them itself |

The answer is always text. The caller should treat it as advice or a draft.

## Tools

| Tool | What it does |
|---|---|
| `gregory_ask` | Ask the expert. Arguments: `prompt`, optional `skill`, optional `files`. |
| `gregory_list_skills` | The skills this client may name, and when they were last refreshed. |
| `gregory_update_skills` | Fetch the skills repository now. |
| `gregory_update_check` | Installed version, newest release, result of the last update. |
| `gregory_update_apply` | Install the newest release the owner published. |

Each client has its own bearer token (`token-<client>` in the config directory). The token decides
who is asking: which skills it may name, which hand-over folder it reads from, and its daily limit.

## Skills: one repository for the agents and for Gregory

```
agent-skills/                 a private repository of yours
├── podcast/
│   ├── access.toml           clients = ["merlin"]
│   └── shownotes/
│       ├── SKILL.md
│       └── references/...
└── dev/
    ├── access.toml           clients = ["anton"]      backend = "claude"   (optional)
    └── review/SKILL.md
```

Gregory fetches one branch with a read-only deploy key, checks the tree (no links, an `access.toml`
per area, a `SKILL.md` per skill, size limits) and publishes a plain copy as
`/srv/agent-skills/current/<area>/<skill>`. A tree that fails the check is not published. Your
agents can load the very same folders as their own skills (for Hermes: `skills.external_dirs`), so
there is one source. To change a skill, change the repository: with a coding agent that opens a
pull request, that works from a phone. `examples/check-skills.py` is the same check for the
repository's CI.

## Handing over files

Each client has a folder `/srv/handoff/<client>` that it can write and Gregory can read. The agent
puts files there (mount it into the agent's sandbox, e.g. as `/handoff`) and names them in `files`.
Gregory takes only plain files and folders from the caller's own folder, never follows a link, and
copies them into the directory of this one request. The folder is emptied after 12 hours.

## Backends

`config.toml` says which backend answers; a skill area can name another one. The caller never does.

| Type | What it runs |
|---|---|
| `claude-code` | The Claude Code command line program in a sandbox, with Read/Glob/Grep on the request's directory and, unless `web = false`, web search and fetch. No Bash, no writing. |

A new kind of model is one class in `gregory/backends.py`.

## Setting it up

Needs Python 3.11+, bubblewrap, git and systemd. Standard library only.

1. `sudo bash setup/root-setup.sh <agent-user> <client> [<client> ...]` creates the user `gregory` and the two shared folders.
2. As `gregory`: unpack a release to `~/releases/<version>`, link `~/current` to it, install Claude Code (`~/.local/bin/claude`).
3. `setup/set-claude-token.sh` stores the token from `claude setup-token`.
4. `setup/new-client-token.sh <client>` creates a client's bearer token.
5. Write `~/.config/gregory/config.toml` (see `examples/config.toml`), run `python3 -m gregory config-check --strict`.
6. Install `examples/gregory.service` as a systemd user unit and start it.
7. For skills: `setup/new-skills-key.sh`, add the printed public key to the skills repository as a read-only deploy key, add `[skills]` to the config, enable `examples/gregory-skills.timer`.
8. Point each agent at `http://127.0.0.1:8768/mcp` with its bearer token.

Try it without an agent: `echo "What is 2+2?" | python3 -m gregory ask`.

## Updates

Like its siblings: a release on GitHub (tag `vX.Y.Z`) published by the login in `[update]` is
downloaded, its tests run in a sandbox without access to the home, the config is checked with the
new code, `~/current` is switched in one step, the service restarts, and it rolls back if the new
version does not come up.

## Limits and things to know

- Whatever is sent to Gregory is sent to the provider of the model behind it.
- The model's token is in the environment of the sandboxed process. It has no tool that runs code, and reading `/proc` is denied, but the sandbox's network is open.
- Requests are answered one at a time per slot (`max_parallel`), with a time limit and a daily limit per client. There is no queue: a busy or timed-out request is refused with a message.
- Separation between clients is as strong as the separation between the unix users they run as.

MIT licensed.
