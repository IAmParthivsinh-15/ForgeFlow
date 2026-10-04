# repos/

Put (or `git clone`) the repositories you want ForgeFlow to analyse in this folder.
Each sub-folder appears in the UI's **Repository** drop-down.

The folder is mounted **read-only** at `/repos` in docker compose, and the Requirement
Analyzer can only read inside the selected repository. It never reads `.env`, keys,
or other secret-bearing files.

Contents of this folder are git-ignored (except this README).
