# Experiment recipes

This directory is the maintained storage location for executable experiment
recipes. Both the Web experiment builder and multi-round executor use it.

The default location is `<autospin>/recipes`. Set `AUTOSPIN_RECIPES_DIR` before
starting the Web server to use a persistent directory elsewhere. The explicit
`recipes_path` argument to `src.webapp.create_app()` takes precedence over the
environment variable.

Do not point this directory at the retired sibling `AutoSpinmotorSystem`
project. Recipe JSON files can be copied here once for migration, after which
the maintained application is self-contained.
