# CC String Merge

Faster Sim menus in **The Sims 3** when you have a lot of custom content.

Every CC object brings its own string tables, usually 23 of them (one per language). With a few thousand objects the
game holds tens of thousands of tiny tables, and every name lookup gets slower. The Sim's pie menu sorts its entries
by name and does thousands of those lookups per click, so clicking a Sim takes a noticeable moment.

CC String Merge moves all those strings into **one table per language**. The game finds strings by their key, not by
the table they are in, so no text changes in game.

**Measured** on a library with about 130,000 string tables: building the Sim's pie menu took 415-460 ms before and
100-170 ms after.
<!-- TODO PLAY-12: before/after for the default strings mode on a separate-files library -->

## How to use it

1. Close The Sims 3. The tool refuses to run while the game is open.
2. Download `CCStringMerge.exe` and check its SHA-256 against `SHA256SUMS.txt` on the release page.
3. Double-click it. It uses `Documents\Electronic Arts\The Sims 3\Mods\Packages`. Or drop any folder of CC onto it.
4. It shows what it would change. Type `Y` and press Enter to go ahead.

It does this:

- Takes the string tables out of every CC package and writes them into one `CCStringMerge_Strings.package` in that
  folder. Every other resource is copied byte for byte, and your files keep their names and folders, so you can
  still remove one item by deleting its file.
- Moves the originals into a backup folder **outside Mods** (`The Sims 3\CCStringMerge backups\<date>`), so the
  game never loads them twice. Nothing is deleted.
- Checks every resource and every string against the backup afterwards. On any mismatch it puts everything back by
  itself.

**New CC later?** It works right away, just without the speed-up. Run the tool again and it adds the new strings to
the strings package.

**Removed CC?** Its strings stay in the strings package. That does no harm.

### Merge mode

`CCStringMerge.exe --merge` also merges all CC packages into a few big `CCStringMerge_Merged_NNN.package` files
(1 GB each; change it with `--part-size <MB>`). The game then has fewer files to open too, but you can no longer
remove one item by deleting its file. If two packages hold the same resource, the first one in name order is kept,
and the report lists every case.

### What it leaves alone

- Script mods (packages with a `.dll` assembly inside, such as NRaas), packages from other games, and anything that
  does not read cleanly. The report lists each one and the reason.
- `.sims3pack` files and the launcher's `DCCache`: only `.package` files in the folder you give it are read.

## Undo

Drop the run's backup folder (`The Sims 3\CCStringMerge backups\<date>`) onto `CCStringMerge.exe`, or run
`CCStringMerge.exe --undo "<that folder>"`. Undo newer runs before older ones.

Before changing anything, the undo checks that the files it wrote are still unchanged. If you edited or replaced one
since then, it stops and changes nothing. With `--force` it moves those files to `<backup>\changed-since` and undoes
anyway. If you deleted a CC file since then, the undo doesn't bring it back; its original stays in the backup.

## All options

```
CCStringMerge.exe [folder] [--merge] [--part-size MB] [--dry-run] [--undo RUN_FOLDER] [--force] [--yes]
```

`--dry-run` shows the report and changes nothing. `--yes` skips the question and the "press Enter" at the end.

## Windows warnings

The exe is not code-signed. Windows SmartScreen may say "Windows protected your PC": click *More info*, then
*Run anyway*. Some antivirus programs flag every PyInstaller exe at first. If you prefer, run the source instead
(`python ccstringmerge.py`, Python 3.8 or newer, no other packages needed).

## Please link, don't re-upload

Share the link to this page instead of uploading the exe elsewhere. That way everyone gets fixes and can check the
checksum.

## Licence

MIT, see [LICENSE](LICENSE). Not made by or affiliated with Electronic Arts. The Sims is a trademark of Electronic
Arts Inc.

## Building

```
python -m venv .venv
.venv\Scripts\python -m pip install pyinstaller
powershell -File build.ps1
```

`build.ps1` runs the tests (`python -m unittest discover -s tests`), builds `dist\CCStringMerge.exe` and writes
`dist\SHA256SUMS.txt`.
