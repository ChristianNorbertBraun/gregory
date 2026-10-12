# Working on Gregory

- Python 3.11+, standard library only. Do not add dependencies.
- Run the tests with `python3 -m unittest discover -s tests`. Every change needs a test.
- Tests must not need the network, not even loopback: sandboxes that run them block connections to 127.0.0.1. Test the HTTP handler directly with a fake connection, as `HttpTest` does.
- The updater runs these tests inside a bubblewrap sandbox before it installs a release (home `/home/sandbox`, no access to the real home, system read-only). A test that depends on the real home or on files outside the checkout blocks every update.
- A change that should reach an installation needs a new version: raise `__version__` in `gregory/version.py` (patch for fixes, minor for a new feature) in the same pull request. The owner publishes the release tag by hand.
- Gregory has no access of its own. A request may use only what its caller names: a skill the caller is allowed to name and files from the caller's own hand-over folder. Do not add tools, credentials or standing access to other services. The caller is always the holder of the bearer token, never an argument.
- Foreign material (skills, handed-over files) is copied only through `gregory/safecopy.py`, which never follows a link. Do not read such files with plain `open()` or `shutil`.
- The model runs only inside the sandbox (`sandbox.wrap_model`), with the request's directory read-only and without any tool that writes or executes. Its token is passed in the environment, never in argv, and is removed from answers.
- A new kind of model is a class in `gregory/backends.py` listed in `registry()`. The client never chooses the backend.
- `examples/check-skills.py` repeats the rules of `skills.read()` for the skills repository; change both together (a test compares them).
- Never log or return tokens, prompts, answers or paths. Errors shown to a client must not contain internals.
- Code, comments, commit messages and pull requests are in English.
