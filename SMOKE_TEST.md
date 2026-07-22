# Nurbly FreeCAD plugin -- manual smoke test

This is the **in-FreeCAD** test the build environment cannot run (FreeCAD is a
GUI app and is not installed in CI). The automated suite covers the pure logic
(`python -m unittest discover -s tests`); this script covers the parts that
need a live FreeCAD + a reachable Nurbly server: the workbench loading, STEP
export, the dialogs, and the real `nrb` shell-out.

> No live FreeCAD run has been performed by the build. Run these steps
> yourself. Each has an explicit expected result.

## Prerequisites

- FreeCAD 0.20 or newer installed.
- `nrb` reachable: `nrb whoami` works in a terminal, OR `nrb` is on `PATH` /
  in `~/.cargo/bin` / set via `$NRB_BINARY`.
- A running Nurbly server the CLI is configured against
  (`nrb config list` shows `auth_url` / `api_url` / `git_url`).
- A Personal Access Token (`nrbpat_...`) minted in the web UI under
  **Settings -> API Keys**. The key must have **repo:read** and **repo:write**
  (the defaults) or Check out / Check in will be rejected.
- At least one repository you have **Write** access to.

## Install the plugin

1. Install the addon by ONE of: running the bundled installer
   (`bash install.sh` on Linux/macOS, `powershell -ExecutionPolicy Bypass -File
   install.ps1` on Windows), or copying this `freecad-nurbly/` directory into
   FreeCAD's `Mod` folder by hand (see README for the per-OS path). Then start
   FreeCAD.
   - If you ran the installer, **expect:** it prints the target `Mod` path and
     either `Found nrb: <path>` or a non-fatal `WARNING: the 'nrb' CLI was not
     found.` block (and still exits 0). Confirm `__pycache__` folders did not get
     copied into the installed addon.
2. **Expect:** the workbench selector lists **Nurbly**. Selecting it shows a
   **Nurbly** toolbar + menu with nine entries: Sign in, Link & clone,
   Open project, Check out, Check in, Who has it?, **Switch branch**,
   **Settings**, **Diagnostics**.
   - If it does not appear, open **View -> Panels -> Report view** and re-select
     the workbench. A Python traceback there is the first place to look.

## 0. First-run: locate nrb (only if autolocate fails)

1. Temporarily rename/move `nrb` off `PATH` (or unset `$NRB_BINARY`) to force
   the failure path, then click **Sign in**.
2. **Expect:** a file-picker dialog *"Locate the nrb CLI binary"* opens. Point
   it at the `nrb` executable.
3. **Expect:** the action proceeds with the chosen binary, AND the path is
   PERSISTED to `~/.nurbly/nurbly-plugin/config.json` (`{"schema":1,"nrb_path":
   "..."}`). On the next FreeCAD launch the plugin uses that saved path first.
   No picker reappears even with `nrb` still off `PATH`.
4. If you cancel the picker instead, expect the original *"nrb CLI not found"*
   error to surface. Restore `nrb` to `PATH` (or keep the saved path) and
   continue.

## 0b. First-run: too-old nrb version guard

1. With a current `nrb` on `PATH`, run any action -- it proceeds normally (the
   guard passes silently on the supported version).
2. To exercise the block, point the plugin at a pre-0.1.0 `nrb` (an older build,
   or a stub on `PATH` whose `nrb --version` prints e.g. `nrb 0.0.9`), then click
   any action.
3. **Expect:** an actionable dialog titled *"Update nrb to v0.1.0 or newer"*
   naming the installed (too-old) version, with the *"update nrb"* remedy. No raw
   clap error from a missing `--json` flag leaks through.
4. Update `nrb` (`nrb update` or re-run the install command) and confirm the
   action proceeds. A `nrb` whose `--version` cannot be parsed must NOT block --
   the guard degrades to allowing the action.

## 1. Sign in

1. Click **Sign in**. A *"Sign in to Nurbly"* dialog opens with a **Get an
   access key** button and a masked field.
   - Click **Get an access key**. **Expect:** the default browser opens your
     Nurbly **API Keys** page at `<web>/settings?tab=keys`. The managed default
     is `https://app.nurbly.com/settings?tab=keys`, or your `NURBLY_WEB_URL` env
     var / config `web_url` override for a local stack. Mint a key and copy it.
   - Paste the PAT into the masked field. Click **Sign in**.
2. **Expect (success):** an info dialog *"Signed in to Nurbly."*. The detail
   pane shows `Logged in as <you> (via PAT)`.
3. **Expect (bad token):** paste a wrong token, then a warning *"You are not
   signed in"* with the verbatim `Token rejected by the server ...` text and a
   remedy pointing you back at **Get an access key**.
4. Verify out-of-band: `nrb whoami` in a terminal prints your username.

## 2. Link & clone

1. Create or open a document and **save it** somewhere (File -> Save) so it has
   a `.FCStd` path on disk. Draw at least one visible solid (e.g. a Part Box).
2. Click **Link & clone**.
3. **Expect:** a picker listing your repos as `owner/repo`. Pick one.
4. **Expect:** a folder picker asking where to clone, pre-filled with your
   document's folder (or your home directory when the document is read-only,
   e.g. a FreeCAD example under `C:\Program Files\...`). Pick a writable folder.
   The clone then runs (the action is synchronous, FreeCAD may briefly pause
   during a large clone), then an info dialog *"Cloned `<owner>/<repo>`
   and linked this document."* appears. A new folder named after the repo
   appears under the folder you chose, **and the active document is SAVED INTO
   that folder** (clone-first working model). Check the FreeCAD title bar /
   `File -> Save As...` location: the active document now points at
   `<clone>/<name>.FCStd`. From here on you edit the in-clone copy. The original
   pre-clone `.FCStd` is left untouched.
5. Verify the link persisted: open
   `~/.nurbly/nurbly-plugin/doc-map.json`. It contains an entry keyed by the
   document's **in-clone** absolute path (`<clone>/<name>.FCStd`), with `owner`,
   `repo`, `repo_path`, `clone_dir`, and `lock_id: null`.
6. **Unsaved-doc guard:** with a brand-new never-saved document active, click
   **Link & clone**, then expect *"Save the document first"*.

## 2a. Open project (join an existing repo, no document needed)

This is the document-free entry point: clone (or open an already-cloned)
project that already has `.FCStd` files and open one, with NO active document.

1. **No-document-needed check:** with NO document open (an empty FreeCAD), the
   **Open project** button is still ENABLED (its `IsActive` is always True,
   unlike Link & clone / Check out which grey out without a document).
2. Click **Open project**. **Expect:** the repo picker (`owner/repo`). Pick a
   repo that already contains a `.FCStd` (the seeded CAD repos work). Then a
   folder picker (defaults to your home directory). Pick a writable folder.
3. **Expect (single document):** the clone runs, the lone `.FCStd` opens
   automatically (it becomes the active document), and an info dialog
   *"Opened `<owner>/<repo>`."* appears. No `Link & clone` was involved.
4. **Expect (several documents):** for a repo with more than one `.FCStd`, a
   *"Pick a document to open"* dropdown appears listing the repo-relative paths
   (e.g. `asm.FCStd`, `parts/bracket.FCStd`). Choose one. It opens and becomes
   active.
5. Verify the link persisted: `~/.nurbly/nurbly-plugin/doc-map.json` has an entry
   keyed by the opened document's **in-clone** absolute path, with `owner`,
   `repo`, `repo_path` (forward-slash, derived from where the file sits in the
   clone), `clone_dir`, and `lock_ids: {}`. Only the opened document is mapped.
6. **Acceptance proof:** immediately click **Check out** on the just-opened
   document. **Expect:** it works (no *"Document is not linked"*), confirming
   Open project created a usable mapping.
7. **No-documents case:** pick a repo that has no `.FCStd` (e.g. a STEP-only
   repo). **Expect:** the clone runs, then a soft-success dialog *"No FreeCAD
   documents found"* naming the clone folder, and NO mapping is written.
8. **Already-cloned (adopt) case:** run **Open project** again for the SAME repo
   into the SAME parent folder. **Expect:** it does NOT re-clone (no git error
   about a non-empty folder), it just re-opens the document and the mapping
   still resolves. Watch the report view: no `clone` verb runs this time.

## 3. Check out

1. With the linked document active, click **Check out**.
2. **Expect:** the pull + lock run (synchronous, FreeCAD may briefly pause),
   then an info dialog *"Checked out -- you hold the lock(s)."*. The detail pane
   lists one `Locked <repo path>` line per file that was locked.
3. Verify the lock ids were captured: `doc-map.json` now has a non-empty
   `lock_ids` dict (schema 2), keyed by the repo-relative path of each locked
   file with its lock id as the value.

### 3a. Per-part locking (assembly with in-tree parts)

This is the per-part path: Check out locks the active file **plus every linked
part that lives inside the clone**, all in one action.

1. In the linked (in-clone) assembly, link in at least one part that **also
   lives inside the clone** (e.g. a `parts/bracket.FCStd` next to the assembly).
   Save the assembly.
2. Click **Check out**.
3. **Expect:** the report view / terminal shows a `pull`, then **one `nrb lock`
   per in-tree path** (the assembly's own path **and** each in-tree part). The
   success dialog lists a `Locked <repo path>` line for each.
4. Verify `doc-map.json`: `lock_ids` now has **one entry per locked path**
   (the active file and each in-tree part), each with its own lock id.
5. **Out-of-tree parts are NOT locked:** if the assembly also links a part that
   lives **outside** the clone, confirm it does **not** appear in `lock_ids` and
   no `nrb lock` ran for it (it is not in the repo, so there is nothing to lock).
6. Cross-check with `nrb locks <owner>/<repo>`: every in-tree path you checked
   out should appear as locked by you.

### 3b. Lock-conflict (report and abort)

1. From a *second* account/CLI, lock **one of the in-tree parts** (not the
   assembly's own path) first.
2. From FreeCAD, Check out the assembly.
3. **Expect:** a warning *"File is locked by @<them>"* with the remedy to
   coordinate / use Who has it?. The check-out is **aborted**.
4. **Crucially, verify the rollback:** the report view shows the assembly's own
   lock was acquired and then **released** (an `nrb unlock` for it) once the part
   conflict was hit, so you should **not** be left holding a partial set. Confirm
   `doc-map.json` `lock_ids` is **empty** (nothing was cached), and
   `nrb locks <owner>/<repo> --my` does **not** list the assembly's path. If a
   rollback unlock itself fails, or a lock was acquired whose Lock ID could not
   be read, the warning's detail names the conflicting path AND lists those
   **still-held** paths so you can free them via Who has it? then `nrb unlock`.
5. **Single-file conflict:** for a lone document (no in-tree parts), have the
   second account lock that file, then Check out, then the same warning, and
   since nothing was acquired first, **no** `nrb unlock` rollback runs.

## 4. Check in

1. Modify the model (move the box, add a feature). You do NOT need to save
   first. Check in saves the in-clone `.FCStd` for you.
2. Click **Check in**. Enter a commit message. Click OK.
3. **Expect, in order (watch the Report view / terminal if `nrb` logs):**
   save (the in-clone `.FCStd`) -> STEP export -> `add .` -> `commit` -> `push`
   -> `unlock` (the action is synchronous, FreeCAD may briefly pause during the
   push), then an info dialog *"Checked in -- pushed and unlocked."*. The
   dialog's detail pane shows the commit short-sha (e.g. `Commit 1a2b3c4d`),
   now read from `nrb commit --json` (the plugin appends `--json` to the commit;
   on an older nrb it falls back to scraping the human `[<sha>]` line, so the sha
   still appears).
   Confirm the in-clone `.FCStd` and the `.step` are both written.
4. Verify BOTH files were committed: inside the clone dir, next to the `.FCStd`,
   there is a `<name>.step`. Run `nrb log` (or inspect the repo) and confirm the
   commit contains **both** the `.FCStd` and the `.step`. `add .` stages both.
   Re-import the STEP (File -> Import) to confirm it is valid geometry.
4b. **Named + coloured assembly tree (the 2026-06-21 `Import.export` upgrade).**
   Use an assembly with at least two named bodies (distinct Labels, e.g.
   `BasePlate`, `Pin`, and distinct ViewObject colours). After Check in, open
   the part in Nurbly's web viewer and confirm the **assembly-tree panel shows
   the real part NAMES** (`BasePlate` / `Pin`), NOT generic
   `Open CASCADE STEP translator` leaves, and that each part shows its
   **colour**. Colours come from the GUI `ViewObject`, so this is the one thing
   CI cannot check -- the structured export (names + App::Link) is validated
   headless, but colours are GUI-only. A FLAT, nameless, colourless tree means
   the export fell back to the geometry-only compound; check the Report view for
   an `Import.export` error.
5. Verify **every** lock was released: `doc-map.json` `lock_ids` is back to `{}`
   (empty), and `nrb locks <owner>/<repo>` no longer lists the file. If you
   checked out an assembly with in-tree parts (section 3a), confirm the report
   view ran **one `nrb unlock` per cached lock id** (the assembly path and each
   in-tree part) and that `nrb locks` lists **none** of them anymore.
6. **Push-rejected path:** from a second clone, push a change to the same branch
   first, then Check in from FreeCAD, then expect *"Someone pushed first -- pull
   latest"*, and confirm **no unlock happened** (you still hold every lock, with
   `lock_ids` still populated). Run Check out again to pull, then Check in.
7. **Unlock-failure path:** if the push succeeds but a lock release fails (e.g.
   the server denies one release), expect *"Pushed, but releasing the lock
   failed"* whose detail lists **each still-held path** with its **Lock ID** and
   the exact manual command `nrb unlock <owner>/<repo> <lock-id>`. Only the
   **stuck** ids stay cached in `lock_ids` (the ones that released cleanly are
   dropped), and the release loop does **not** abort on the first failure (the
   other locks are still attempted). Release the stuck ones by hand.
8. **Assembly with an out-of-tree linked part (dependency warning, v1
   fallback):**
   1. Save a standalone part `partB.FCStd` to a folder **outside** the clone
      (e.g. your Desktop), with at least one visible solid.
   2. In the linked (in-clone) assembly document, link that external part in
      (e.g. `Std LinkMake`, or insert it as an `App::Link`) so the assembly
      depends on `partB.FCStd`. Save the assembly.
   3. Click **Check in**, enter a message, click OK.
   4. **Expect:** if you DECLINE the auto-relocate offer (the consent flow in
      section 4.9), the check-in still **succeeds** (push + unlock run) but the
      dialog is a **warning** *"Checked in, but some linked parts were not
      committed"* whose detail lists the **out-of-tree** path
      (`.../Desktop/partB.FCStd`) and a remedy to move it into the repo clone
      folder and re-link it. Confirm via `nrb log` / repo inspection that
      `partB.FCStd` is **not** in the commit (only the assembly + its `.step`).
   5. **Now fix it by hand:** move `partB.FCStd` **into** the clone folder,
      re-link the assembly to the in-clone copy, save, and Check in again.
      **Expect:** a plain *"Checked in -- pushed and unlocked."* success (no
      warning), and the commit now contains `partB.FCStd` too. A single-document
      Check in (no links) must still show the plain success dialog with **no**
      dependency warning.

9. **Assembly v2: auto-relocate an out-of-tree linked part (consent flow).**
   This is the v2 path: instead of manually moving the part, the plugin offers to
   copy and re-link it for you. It needs a real assembly with a genuine
   out-of-tree link and a fresh clone to prove the rewrite persisted.

   1. Save a standalone part `partC.FCStd` to a folder OUTSIDE the clone (e.g.
      your Desktop), with at least one visible solid.
   2. In the linked (in-clone) assembly, link that external part in (Std
      LinkMake or insert it as an App::Link) so the assembly depends on the
      Desktop copy of `partC.FCStd`. Save the assembly.
   3. Click Check in, enter a message, click OK.
   4. Expect a CONSENT dialog titled Nurbly: "Some linked parts live outside the
      repository clone." Its detail pane lists the out-of-tree path
      (`.../Desktop/partC.FCStd`) and it offers Proceed or Cancel.
      - Click Cancel first to verify the v1 fallback: the check-in still
        succeeds but ends in the warning "Checked in, but some linked parts were
        not committed" listing `partC.FCStd`. The Desktop file is untouched and
        the link still points at it.
   5. Repeat the Check in and this time click Proceed. Expect a plain "Checked
      in -- pushed and unlocked." success with NO dependency warning. The detail
      reports *"Relocated 1 part(s) into the clone"* and lists the `src -> dest`
      move (`.../Desktop/partC.FCStd -> <clone>/parts/partC.FCStd`).
   6. Confirm the COPY: inside the clone there is now a `parts/partC.FCStd`
      (the file was copied into the flat parts/ folder, parent dirs created, and
      the out-of-tree original is left in place because it was copied, not
      moved).
   7. Confirm the RE-LINK PERSISTED: with the assembly closed and reopened from
      disk (or via `nrb log` / repo inspection of the committed `.FCStd`), the
      assembly's link to partC now points at the in-clone `parts/partC.FCStd`,
      NOT the Desktop path. The rewritten FileName must be saved in the `.FCStd`
      that was pushed (the relocator owns this save), and the commit contains
      both the assembly and `parts/partC.FCStd`. This re-path
      (`dependencies.repath_links_to_clone`) is the first-cut, byte-compile-only
      code, so this step is what actually validates it.
   8. **Idempotent re-check-in:** Check in again with no further edits.
      **Expect:** another clean success, the same `<clone>/parts/partC.FCStd`
      dest (the re-copy is idempotent), and no duplicate `parts/` entries.
   9. Confirm a FRESH CLONE RESOLVES the part: in a different directory, clone
      the repo again (`nrb clone <owner>/<repo>` into a new folder) and open the
      assembly there. Expect it to open with the part resolved from
      `parts/partC.FCStd` inside that clone, with NO "file not found" / broken
      link prompt for partC. This proves the re-link is repo-relative-correct,
      not tied to the original machine's Desktop.
   10. Re-run Check in on the now-clean assembly. Expect a plain success with NO
       consent dialog and NO warning (partC is already in tree, so there is
       nothing out of tree to relocate).

### 4w. Push-to-main warning (informational, never blocks)

A Check in while the clone is on the shared trunk (`main` / `master`) warns
first -- it does NOT block. Needs a linked document (sections 2 + 4).

1. Make sure the clone is on `main` (Switch branch -> `main`, or it is the
   default after Link & clone). Modify the model, then click **Check in**.
2. **Expect (BEFORE the commit-message prompt):** a *"Checking in to 'main'"*
   dialog explaining that `main` is the shared branch and suggesting a branch +
   pull request instead, with an **Open the guide** button and **Check in
   anyway** / **Cancel**.
   - Click **Open the guide**. **Expect:** the default browser opens the User
     Guide at `<web>/help#why-not-commit-straight-to-main` (the managed default
     `https://app.nurbly.com/help#...`, or your `NURBLY_WEB_URL` / config
     `web_url` override) scrolled to "Why not commit straight to main?". The
     dialog stays open.
   - Click **Cancel**. **Expect:** the Check in is aborted -- NO commit-message
     prompt, no push.
   - Click **Check in anyway**. **Expect:** the normal commit-message prompt
     follows and Check in proceeds exactly as section 4. The plugin never
     prevents the push -- the warning is informational only.
3. **No warning off-trunk:** Switch branch to a non-default branch (e.g.
   `feature/x`), then Check in. **Expect:** NO trunk warning -- straight to the
   commit-message prompt. The detection is best-effort: if `nrb branch` cannot
   be read, Check in proceeds with no warning rather than being blocked.

## 5. Who has it?

1. Click **Who has it?**.
2. **Expect:** a table dialog (Path / Locked by / Expires / Message) listing the
   active locks for this document's repo, or *"No active locks for
   `<owner>/<repo>`."* if none.
3. With no linked document active, click it, then expect it falls back to
   **your** locks (`nrb locks --my`).

## 5a. Switch branch

Set the working branch of the active document's clone from inside FreeCAD.
Needs a linked, checked-in document (sections 2 + 4) with at least two branches
in its repo (create a second one in the web UI's branch selector, or via the
**[+ Create a new branch...]** entry below).

1. With the linked (in-clone) document active and the working tree CLEAN
   (everything committed via Check in), click **Switch branch**.
2. **Expect:** a *"Switch branch"* picker listing the clone's local branches,
   the current one tagged `(current)`, plus a final **[+ Create a new
   branch...]** entry. The current branch is pre-selected.
3. Pick a DIFFERENT existing branch. **Expect:** the switch runs (`nrb status`
   then `nrb switch`), an info dialog *"Switched to branch '<name>'."*, and the
   **active document reloads** from disk so the viewport shows that branch's
   version. Confirm out-of-band with `nrb branch` in the clone -- the `* `
   marker is on the branch you picked.
4. **Create-and-switch:** click **Switch branch** again, choose **[+ Create a
   new branch...]**, type a new name (e.g. `feature/test`). **Expect:** an info
   dialog *"Created and switched to branch '<name>'."*. No reload happens (the
   new branch starts at the current commit, so the file is unchanged). `nrb
   branch` now lists and marks the new branch.
5. **Dirty-tree guard:** modify the model and **do NOT** Check in. Click
   **Switch branch** and pick a different existing branch. **Expect:** a warning
   *"You have uncommitted changes"* with the remedy to commit (Check in) or
   discard first, and the report view shows `nrb status` ran but **`nrb switch`
   did NOT** -- HEAD was not touched. Check in (or discard), then retry: the
   switch now succeeds. (Create-and-switch is allowed even when dirty, since the
   working tree is preserved.)
6. **Not-linked guard:** with an unlinked saved document active, click **Switch
   branch**. **Expect:** *"Document is not linked"* pointing you at Link & clone
   / Open project.
7. **Missing-on-branch case (optional):** switch to a branch where the active
   document does not exist. **Expect:** the switch still succeeds and the
   success dialog appends *"This document does not exist on the new branch;
   nothing reopened."* (the stale document is closed, none reopened).

## 6. Not-logged-in surfacing

1. `nrb logout` in a terminal (clears `~/.nurbly/credentials.json`).
2. Click **Check out** (or any repo action).
3. **Expect:** a warning *"You are not signed in"* with the remedy to run Sign
   in. (Drives off the CLI's exit code + message, not a crash.)

## 7. Settings & Diagnostics

1. Click **Settings**.
2. **Expect:** a *"Nurbly settings"* dialog with an editable **nrb binary** field
   (prefilled with your saved override, or the auto-located path if none is
   saved), a **Browse...** button, a **Status** line (*Located: ...* or *Not
   found: ...*), a **Show active config** button, and Save / Cancel.
3. Edit the path / use **Browse...**, then click **Save**. **Expect:** the chosen
   path is persisted to `~/.nurbly/nurbly-plugin/config.json` (`"nrb_path"`), and the
   next action uses it. Clearing the field and saving removes the override (the
   runner falls back to `$NRB_BINARY` / `PATH` / `~/.cargo/bin`).
4. Click **Show active config**. **Expect:** a read-only scrollable text dialog
   showing the verbatim `nrb config list` output, including `auth_url` /
   `api_url` / `git_url`. If `nrb` can't be reached, expect *"Could not read nrb
   config"* with the error text.
5. Click **Diagnostics**.
6. **Expect:** a read-only text dialog showing the **last** `nrb` command the
   plugin ran (argv + exit code + raw stdout/stderr). The *Show active config*
   run from step 4 should be the most recent entry. On a brand-new config with no
   recorded run, expect the empty-state message *"No nrb command has been recorded
   yet..."* instead.

## 8. Console window (Windows only)

During any action, confirm **no black console window flashes**. The runner sets
`CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW`. A flashing console means that path
regressed.

## 9. Output-format drift check (do this whenever nrb is upgraded)

`repo list`, `lock`, and `locks` are read via `nrb --json`, so their parsing is
pinned to the response DTO field names rather than human wording. A label or
spacing change can't break them. Two things can still drift:

1. `nrb commit` has no `--json` mode, so `extract_commit_sha` still scrapes its
   `[<sha>] message` line. If that changes, the Check in detail pane loses the
   short-sha. Re-run the `output_parser` tests.
2. A renamed `--json` field (e.g. `locked_by_username`) makes the parser yield
   empty rows silently. Run `nrb lock <owner>/<repo> <path> --json`,
   `nrb locks <owner>/<repo> --json`, and `nrb repo list --json` and confirm the
   keys still match the fixtures in `tests/test_output_parser.py`.

---

### What a passing smoke test demonstrates

- The workbench loads and registers all nine commands (the five MVP actions
  plus Open project, Switch branch, Settings + Diagnostics) via InitGui +
  workbench.
- Switch branch lists the clone's branches, switches (or creates-and-switches)
  via `nrb switch`, refuses an existing-branch switch on a dirty tree before
  touching HEAD, and reloads the active document onto the new branch.
- STEP export works against a real document (the one per-CAD piece).
- Each action runs the right `nrb` sequence and surfaces success/failure in a
  dialog with the CLI's verbatim message.
- Per-part Check out locks the whole in-tree closure (assembly + each in-tree
  part) and Check in releases every cached lock together.
- Assembly v2 relocates an out-of-tree linked part into the clone (the consent
  flow copies it to `<clone>/parts`), re-paths the link, and commits the
  relocated part with the assembly (validating the byte-compile-only re-path).
- Settings persists the nrb path and shows the active `nrb config`. Diagnostics
  shows the last command's raw output.
- The doc-to-repo link and the per-part lock ids persist across the check-out
  to check-in loop (the whole in-tree closure is locked and released together).
- The three mandated error dialogs (lock conflict, push rejected, not logged
  in) fire on their real triggers.