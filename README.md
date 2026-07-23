# Nurbly — FreeCAD plugin (MVP)

A thin PDM shell for FreeCAD. Toolbar buttons run the daily check-out /
check-in loop against a [Nurbly](https://nurbly.com) repository, by shelling out
to the `nrb` CLI (nine commands: seven actions plus Settings and Diagnostics).
The plugin **embeds no git** and **stores no credentials**,
`nrb` owns both (credentials live in `~/.nurbly/`).

## The seven actions

| Button | nrb sequence |
|---|---|
| **Sign in** | `nrb login --token <PAT>` |
| **Link & clone** | **(needs the active doc)** `nrb repo list` → pick → **choose a writable clone folder** → `nrb clone <owner>/<repo>` → **save the active doc INTO the clone** → persist doc↔repo link |
| **Open project** | **(no open document needed)** `nrb repo list` → pick → choose a folder → `nrb clone <owner>/<repo>` (skipped if already cloned) → discover the `.FCStd` files → open one (auto if single, pick if several) → persist doc↔repo link |
| **Check out** | `nrb pull` → `nrb lock <owner>/<repo> <path>` for the active file **and each in-tree linked part** (caches every printed `Lock ID`) |
| **Check in** | (if on `main`/`master`, a non-blocking "push to the shared trunk" warning first, linking the User Guide) → save in-clone doc → export STEP beside it → relocate out-of-tree linked parts into the clone → `nrb add .` (stages **both** the `.FCStd` and the `.step`) → `nrb commit -m` → `nrb push` → `nrb unlock <owner>/<repo> <lock-id>` for **every cached lock** |
| **Who has it?** | `nrb locks <owner>/<repo>` |
| **Switch branch** | **(needs a linked doc)** `nrb branch` (list) → pick an existing branch or create a new one → `nrb status` dirty-tree pre-check → `nrb switch [-c] <branch>` → reload the active document onto the new branch |

Two further entries — **Settings** and **Diagnostics** — sit after the seven
actions on the toolbar/menu; see [Settings & Diagnostics](#settings--diagnostics).

**Link & clone vs Open project.** They are the two ways into a repo, with
opposite preconditions. **Link & clone** takes the part you ALREADY have open,
clones a repo, and saves your document INTO it (greenfield, "publish this new
part"). **Open project** needs NO open document: it clones (or opens an
already-cloned) project that already has `.FCStd` files and opens one for you
(day-2, "join an existing project"). Open project derives the in-repo path from
the opened file via `assembly.repo_relative_path`, so it works for documents
nested in sub-folders, and it never saves over the cloned file.

**Signing in is self-service, no command line.** **Sign in** opens a dialog
with a **Get an access key** button that launches your Nurbly **API Keys** page
in the browser (`<web>/settings?tab=keys`). You mint a Personal Access Token
(`nrbpat_...`), copy it, and paste it into the masked field. When you mint the
key, keep at least **repo:read** and **repo:write** selected (the API Keys tab
defaults to both), since the plugin clones, pulls, pushes, and manages locks.
The plugin then runs `nrb login --token <PAT>` for you and `nrb` stores the one
key. That same PAT authenticates the REST API and `git`/LFS, so one key with
**repo:write** covers everything the plugin needs (admin is not required). The
web base defaults to the managed app and can be pointed at a
local stack with the `NURBLY_WEB_URL` env var or a `web_url` key in
`~/.nurbly/nurbly-plugin/config.json` (see [`links.py`](nurbly/links.py)).

**Clone-first working model:** Link & clone saves your active document into the
freshly cloned working copy and re-points FreeCAD at that in-clone file. It first
asks where to create the clone, defaulting to your document's folder when that is
writable and otherwise to your home directory, so opening a read-only FreeCAD
example (which lives under the install tree) no longer fails with "Access is
denied". From then on you edit the file inside the clone, so Check in commits both
your native `.FCStd` and the exported `.step` together in one push.

## Assemblies

Check in stages the **whole clone working directory** (`nrb add .`), so any part
file that already lives **inside** the clone is committed alongside the assembly.
A linked part that lives **outside** the clone is *not* staged — it would be
silently dropped from the push, leaving an assembly whose parts are missing on
the server.

Check in handles this by inspecting the active document's dependency closure
(its linked child `.FCStd` files). If every dependency is inside the clone, the
check-in is a clean success. If any live **outside** the clone, **v2 relocates
them into the clone** before staging: each out-of-tree part is copied to
`<clone>/deps/<basename>` (basename collisions disambiguated by a short stable
hash of the source's parent dir, re-copy is idempotent), the document's links are
re-pointed at the in-clone copies, the doc is re-saved, and then `nrb add .`
commits the relocated parts alongside the assembly. The success dialog reports
how many parts were relocated. With no out-of-tree deps (the common
single-document case) behaviour is unchanged.

The relocation **plan** — classifying deps as in/out-of-tree and turning the
out-of-tree paths into deterministic `(src, dest)` copy ops — is pure and
unit-tested (`nurbly/assembly.py`); core sequences the copy → re-path → re-save →
`add .` (`nurbly/core.py`, also pure, with the copy and re-path injected as
callables). The FreeCAD-side pieces — discovering linked files and re-pathing the
links (`nurbly/host/dependencies.py`) — are byte-compiled in CI but **not yet
validated** against a live FreeCAD; the re-path in particular needs the manual
smoke test (see [Known limitations](#known-limitations-mvp)).

### Per-part locking

Check out locks the **whole in-tree closure**, not just the active file: it
runs `nrb lock` for the active document's path **and every linked part that
lives inside the clone**. Each acquired lock id is cached (keyed by its
repo-relative path) so Check in can release them all. Out-of-tree parts are
**not** locked at Check out. They are not yet in the repo, so there is nothing
to lock (they get relocated into the clone at Check in, per the section above).

Acquisition is **report-and-abort**: if any lock in the loop hits a conflict
(someone else holds a part), Check out releases every lock it already acquired
**this call** and returns the conflict, so you never end up holding a half-set
of locks. The mapping is written only after the **whole** set is acquired
cleanly.

Check in releases **every** cached lock, one `nrb unlock` per id, and never
aborts on the first failure. The cache is cleared only on a **fully clean**
release. If some releases fail, the cleanly-released ids are dropped and only
the stuck ones stay cached, with the exact manual `nrb unlock` command for each.

A lone document (no linked parts) locks and unlocks exactly **one** path,
identical to the pre-per-part behaviour. The pure acquire/release logic is
unit-tested (`nurbly/core.py`, `nurbly/assembly.repo_relative_path`). The
mapping store caches the ids as a `lock_ids` dict (`doc-map.json` **schema 2**,
which still loads an old single-`lock_id` map, see `mapping_store.py`).

## Settings & Diagnostics

Two extra toolbar/menu entries support troubleshooting:

- **Settings** (`Nurbly_Settings`) — view/edit the located `nrb` binary path
  (prefilled with the saved override, or the auto-located path if none is saved),
  Browse to it, and Save (an empty field clears the override so auto-location
  takes over again). A **Show active config** button shells out to
  `nrb config list` and shows the `auth_url` / `api_url` / `git_url` it reports,
  verbatim. The plugin never edits `~/.nurbly/config.json` itself — `nrb config set`
  is the supported way to change it.
- **Diagnostics** (`Nurbly_Diagnostics`) — show the last `nrb` command the plugin
  ran (argv + exit code + raw stdout/stderr), so you can read the underlying
  failure when a dialog's summary isn't enough. If nothing has run yet, it says
  so. (Today only Settings → Show active config records a run.)

The saved nrb path and the last-run log live in the pure
[`config_store.py`](nurbly/config_store.py) at `~/.nurbly/nurbly-plugin/config.json`;
the two dialogs live in `nurbly/host/settings_panel.py` (FreeCAD-only,
byte-compiled in CI and covered by the manual smoke test).

## Install

Until this addon is in the **official FreeCAD index** (where you'd just search
and click), install it **from this repository** as a *custom repository*. It's a
one-time setup, and FreeCAD makes it fiddlier than it should be — the settings
are split across two places — so here is the exact path, including the traps.

**Requirements:** FreeCAD **1.0 or newer** (it uses the versioned `Mod` dir + the
managed addon loader) and the **`nrb` CLI** (see [The `nrb` CLI](#the-nrb-cli)).

### Step 1 — Register this repo (in Preferences, NOT the Addon Manager gear)

The custom-repository setting lives in FreeCAD's **global Preferences**, *not* in
the gear/⚙ menu inside the Addon Manager window (that gear only has "Open addons
folder" / "Open python dependencies").

1. Menu bar → **Edit → Preferences…**  (on macOS: **FreeCAD → Preferences**).
2. In the Preferences **left sidebar**, scroll to and click **Addon Manager**
   (puzzle-piece icon).
3. Find the **Custom repositories** table → **Add**:
   - **Repository URL:** `https://github.com/TRget88/Nurbly-freecad-plugin`
     _(the trailing `.git` is optional — either form works)_
   - **Branch:** `main`  — **required. Do not leave it blank**, or FreeCAD fetches
     nothing and the addon never appears.
4. Click **OK** to close Preferences.

### Step 2 — Install it

5. Open **Tools → Addon manager**. If it was already open, **close and reopen it**
   so it re-fetches with the new repo. Allow it to connect to the internet if asked.
6. In the **search box** type **`Nurbly`**, select it, and click **Install**.
7. **Fully restart FreeCAD.**
8. Pick **Nurbly** from the **workbench dropdown** (top toolbar) — the Nurbly
   toolbar + menu appear.

### If something goes wrong

- **`Failed to download … received response code 0`** — a transient GitHub/network
  hiccup in FreeCAD's downloader, **not** a repo problem. **Just retry** (click
  again / reopen the Addon Manager); it typically succeeds on the next try.
- **"Nurbly" never shows up** — re-check that the **Branch** is `main`, then close
  and reopen the Addon Manager. Any real fetch/metadata error is written to
  **View → Panels → Report view** (enable Log/Warning/Error messages first).

### Don't hand-copy the folder into `Mod/`

This addon ships a `package.xml`. For a **manually-copied** (unregistered) addon,
FreeCAD 1.0+ routes through its *managed loader*, which only runs
Addon-Manager-**registered** addons and skips the root `InitGui.py` — so a hand
copy **silently fails to register**. Always install through the Addon Manager
(the custom repository above, or the official index later), which registers it.

### The `nrb` CLI

The plugin shells out to the [`nrb` CLI](https://nurbly.com), which must be
installed separately — download it from the Nurbly CLI page. It's found on
`PATH` (or `~/.cargo/bin`, or via `$NRB_BINARY`); if it can't be found, a
first-run file picker asks for the binary and remembers the path in
`~/.nurbly/nurbly-plugin/config.json`. _(Automatic download of `nrb` on first
run is planned.)_

## Architecture

The plugin is split so that everything except STEP export and the GUI is
**unit-testable without FreeCAD**:

```
nurbly/                      pure logic — imports NO FreeCAD
  cmd_builder.py             build nrb argv lists
  output_parser.py           decode nrb --json output (lock id, commit sha, repo list, locks)
  errors.py                  classify nrb failures + plain-language remedies
  links.py                   build web-app deep links (e.g. the API Keys page)
  mapping_store.py           persist the doc↔repo/path link (+ cached per-part lock ids)
  config_store.py            persist plugin settings (nrb path, web-url override, last-run log)
  nrb_runner.py              locate + subprocess the nrb binary
  assembly.py                classify deps in/out-of-clone + plan relocations
  core.py                    compose the above into the seven actions
  host/                      FreeCAD-only — the host API lives HERE and only here
                             (NOT named `freecad/`: FreeCAD reserves that name)
    freecad_export.py        active document → neutral STEP (Part.export)
    dependencies.py          discover linked child .FCStd files + re-path them
    relink.py                relocate out-of-tree linked parts into the clone (v2)
    settings_panel.py        Settings + Diagnostics dialogs
    gui_dialogs.py           PySide dialogs (token + 'get a key' link, repo picker, document picker, errors, locks, text)
    gui_commands.py          the nine FreeCADGui command classes
InitGui.py                   FreeCAD GUI entry point (registers the workbench)
Init.py                      console-mode entry point (no-op)
tests/                       pure-logic unit tests (run with system python)
```

The single inherently per-CAD file is `host/freecad_export.py`. Everything
else is host-agnostic and shared with future plugins.

## Develop / test

Pure logic runs under system Python (no FreeCAD needed):

```sh
# unit tests run from the tests/ dir (each test self-inserts the addon root on sys.path)
cd tests
python -m unittest discover -v                 # 255 tests
# byte-compile every module (from the addon root)
cd .. && python -m py_compile $(git ls-files '*.py')
```

The FreeCAD-only modules (`host/*`, `InitGui.py`) can only be *imported*
inside FreeCAD; CI just byte-compiles them. To exercise the buttons end-to-end,
run the manual smoke test: [`SMOKE_TEST.md`](SMOKE_TEST.md).

## nrb version requirement

This plugin needs **nrb v0.1.0 or newer** (the first build that emits
`nrb commit --json`). On its first action per session the plugin runs
`nrb --version` and, if the binary is too old, shows an actionable
"Update nrb to v0.1.0 or newer" dialog instead of surfacing a raw clap error
from a missing flag. Update with `nrb update` (or re-run the install command
from the Nurbly CLI page). The check degrades gracefully: a missing or
unparseable version never blocks an action.

## Known limitations (MVP)

- The plugin parses the commit short-sha from `nrb commit --json`
  (`{ "commit", "short" }`) when available, and falls back to scraping the human
  `[<sha>] message` line for an older nrb. The other parsed commands
  (`repo list`, `lock`, `locks`) also use `nrb --json`.
- Assembly relocation (v2) is **unvalidated against a live FreeCAD**: the plan
  (classify + `(src, dest)` ops) and core's copy → re-path → re-save → `add .`
  sequence are pure and unit-tested, but the FreeCAD-side **re-path**
  (`dependencies.repath_links_to_clone`, which sets each dependent document's
  `FileName` to the in-clone copy) has only been byte-compiled — it still needs
  the manual smoke test to confirm the re-pathed links actually persist in-clone
  after save. The first cut keys off dependent documents' `FileName`; an
  object-level `App::Link` re-path within the active document is a possible
  follow-up for assemblies whose links don't resolve through the owning Document
  (see [Assemblies](#assemblies)).
- `Link & clone` defaults the in-repo path to the document's basename; deeper
  paths need a follow-up UI.
- New/changed buttons require a FreeCAD restart (FreeCAD registers commands at
  startup).
