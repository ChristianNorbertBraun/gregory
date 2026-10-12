"""Gregory: an expert that other agents may ask, run by its own unprivileged unix user.

A chat agent sends a prompt, optionally the name of a skill and the names of files it put into its
hand-over folder. A model behind a configurable backend answers with text. Gregory has no access to
anything of its own: what a request may use is what the caller names, and it is gone afterwards.
"""
