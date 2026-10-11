# W.A.T.C.H. on Windows: install, try it, tell us how it went

W.A.T.C.H. shows your coding agents (Claude Code, Codex, Cursor) as a room of monitors in your browser, one per agent, as they work. This guide takes you from a Windows PC with nothing installed to that room. It also lists what we would like you to check, because **nobody has run it on a real Windows desktop yet**. So far the tests have passed on Windows in automated CI, but only in CI. Your report is the first real one.

Allow about fifteen minutes. You need no administrator rights unless your installer asks for them, and you install nothing besides Python and Git.

---

## What you need

| | Why | Minimum |
|---|---|---|
| Windows 10 or 11 | | 64-bit |
| **Python** | runs the server; nothing else is installed into it | 3.9 or later (3.12 or 3.13 recommended) |
| **Git** | to download the project (optional: a ZIP download works too) | any recent version |
| A browser | the room is a web page | Chrome or Edge recommended |
| **Windows Terminal** | only for the optional terminal view | included in Windows 11; free in the Microsoft Store for Windows 10 |
| At least one agent that has been used on this PC | the room shows their sessions | Claude Code, Codex CLI or Cursor |

No agent sessions yet? You can still try it: the demo (step 5) needs none.

All the commands below are for **PowerShell**. To open it, press **Start**, type `PowerShell`, and press Enter. Paste one block at a time. Lines starting with `#` are notes, and PowerShell ignores them.

---

## Step 1: Install Python

**Check first.** You may already have it:

```powershell
py --version
```

If that prints `Python 3.9` or higher (for example `Python 3.12.7`), skip to step 2.

If it says `py` is not recognised, or shows a version below 3.9, choose **one** of these:

**Option A: winget** (built into Windows 10 since 2021 and into Windows 11)

```powershell
winget install --id Python.Python.3.12 -e
```

Accept the prompts. When it finishes, **close PowerShell and open a new one**, so that it picks up the new install.

**Option B: the python.org installer**

1. Go to <https://www.python.org/downloads/windows/> and download the latest **Windows installer (64-bit)** for Python 3.12 or 3.13.
2. Run it. On the first screen, **tick "Add python.exe to PATH"** at the bottom, then click **Install Now**.
3. When it says "Setup was successful", close it, and **open a new PowerShell window**.

> Avoid the Microsoft Store's "python" shortcut. If typing `python` opens the Store, that is Windows' placeholder, not Python. Install with option A or B, then use `py`, which is the launcher both options install.

**Check it worked:**

```powershell
py --version
```

---

## Step 2: Install Git (or skip it and use a ZIP)

**Check first:**

```powershell
git --version
```

If that prints a version, skip to step 3. Otherwise:

**Option A: winget**

```powershell
winget install --id Git.Git -e
```

Then **close PowerShell and open a new one**.

**Option B: the installer.** Download it from <https://git-scm.com/download/win>, run it, and accept the defaults. Then open a new PowerShell window.

**Option C: no Git.** Use the ZIP download described in step 3.

---

## Step 3: Download W.A.T.C.H.

Windows support is in a pull request that has not been merged yet ([glo6al/project-watch#2](https://github.com/glo6al/project-watch/pull/2)). **Until it is merged, download it from the branch that holds it:**

```powershell
cd $HOME
git clone -b windows-upstream https://github.com/er587/project-watch.git
cd project-watch
```

After the pull request is merged, use the main project instead:

```powershell
cd $HOME
git clone https://github.com/glo6al/project-watch.git
cd project-watch
```

**Without Git:**
1. Open <https://github.com/er587/project-watch/tree/windows-upstream> (or <https://github.com/glo6al/project-watch> once merged).
2. Click the green **Code** button, then **Download ZIP**.
3. Right-click the downloaded file and choose **Extract All…**, extracting to your home folder (`C:\Users\<you>`).
4. Then:

```powershell
cd "$HOME\project-watch-windows-upstream"    # or project-watch-main, depending on which ZIP you took
```

**Check you are in the right place:**

```powershell
dir src\server.py
```

That should list one file.

---

## Step 4: Check the tests pass on your machine

This step is optional but useful: it is the same check CI runs. It takes a few seconds and does not touch your real settings.

```powershell
py tests\test_parsers.py
```

The last line should be `all ok`. If it says `FAIL …` instead, that line is the first thing to put in your report.

---

## Step 5: Start the server

```powershell
py src\server.py
```

You should see:

```
W.A.T.C.H. on http://127.0.0.1:8793/  — open it and press Unlock (sessions active in the last 20 min)
```

**Leave this window open.** The server runs until you close the window or press **Ctrl+C** in it.

It listens on `127.0.0.1` only, so only this PC can reach it. Windows Firewall should therefore not ask about it. If it does ask, please tell us, and choose **Cancel** or **Don't allow**: nothing else needs to reach it.

Now open <http://127.0.0.1:8793/> in your browser:

- **To see your own agents:** press **Unlock**. The server asks Windows to open a small unlock page in your default browser, and that tab becomes the unlocked room. It shows every Claude Code, Codex or Cursor session that has written to its log in the last 20 minutes.
  - If **Unlock opens an editor instead of a browser** (Notepad, VS Code, …), your default app for `.html` files is not a browser. Please tell us. To fix it: **Settings → Apps → Default apps**, find `.html`, and choose your browser. Then press Unlock again; it allows one try a minute.
- **To try it without any agents:** open <http://127.0.0.1:8793/?demo>. It plays a made-up fleet of agents, and it needs no unlock.

**Moving around:** two-finger scroll, drag, or the arrow keys turn the row. Hover over a monitor to bring it closer, click it to open that agent's desk, and press Escape to step back.

---

## Step 6 (optional): Settings

Settings are environment variables, set in the same PowerShell window **before** starting the server:

```powershell
$env:WINDOW_MIN = "60"     # show sessions active in the last 60 minutes (default 20)
$env:PORT = "8794"         # use another port (default 8793); then open http://127.0.0.1:8794/
py src\server.py
```

These last only for that PowerShell window.

To give a project folder a friendlier name in the room, create `%USERPROFILE%\.live-room\projects.json` containing, for example:

```json
{"my-long-folder-name": "Website"}
```

---

## Step 7 (optional): The terminal view

The room also runs in a terminal. With the server running, open a **second** window in **Windows Terminal** (the older blue PowerShell console may show odd characters instead of redrawing), go to the project folder, and run:

```powershell
py src\watch_tty.py            # a board, one row per agent
py src\watch_tty.py --tail     # one line per event
py src\watch_tty.py --once     # one line, e.g. "2 waiting · 3 working"
```

`--tab` and `--split` are for iTerm2 on macOS; on Windows they say so and stop. If you used `$env:PORT`, set it in this window too.

---

## Where things are

| What | Where |
|---|---|
| The server's key and settings | `%USERPROFILE%\.live-room\` (private to your Windows account) |
| Claude Code's logs (read) | `%USERPROFILE%\.claude\projects\` |
| Codex's logs (read) | `%USERPROFILE%\.codex\sessions\` |
| Cursor's transcripts (read) | `%USERPROFILE%\.cursor\projects\<project>\agent-transcripts\` |

W.A.T.C.H. only **reads** the agents' folders, and it reports nothing anywhere. To check that a tool has logs it can read:

```powershell
dir "$env:USERPROFILE\.claude\projects"
dir "$env:USERPROFILE\.codex\sessions"
dir "$env:USERPROFILE\.cursor\projects"
```

**Agents running inside WSL** (Linux on Windows) keep their logs in the Linux home folder, not in `C:\Users\<you>`, so a server started from PowerShell will not see them. Running the server inside WSL has not been tried.

**Codex with `CODEX_HOME` set** keeps its logs elsewhere. W.A.T.C.H. only looks in `%USERPROFILE%\.codex`.

---

## What does not work on Windows yet

- **Answering permission prompts from the room (Allow / Deny), and the hooks behind it.** The hooks are shell scripts, and how Claude Code runs hook commands on Windows has not been checked. `py scripts\install_hooks.py` says so and changes nothing. The room still watches, and Claude asks in its own app as usual.
- **Rendered previews of pages an agent writes** in some folders. Previews are deliberately refused for files in OneDrive-synced, network, ReFS/Dev Drive or `AppData` folders, for files reached through a link or junction, and for files that pnpm hard-links into `node_modules`. Some of these are refused on purpose for safety; others we simply have not seen working. Please tell us which happen to you.
- **Desktop app details** (session titles, permission mode, when you last looked at a session) come from the Claude desktop app's own records. We assumed they are in `%APPDATA%\Claude`, but have not checked.

---

## Troubleshooting

| You see | Try |
|---|---|
| `py` is not recognised | Install Python (step 1), then open a **new** PowerShell window. |
| `Port 8793 is busy …` | Another copy is running (look for its window), or one just stopped and Windows is still holding the port. Wait a minute, or use `$env:PORT = "8794"`. |
| The room is empty | Use an agent, then reload the page within 20 minutes, or set `$env:WINDOW_MIN = "120"`. Check the agent's log folder exists (see "Where things are"). |
| "W.A.T.C.H. is locked" | Press **Unlock**, and use the **new** tab it opens. |
| Unlock opens an editor | See step 5: set your browser as the default for `.html`. |
| A preview says not found | Probably one of the folders listed under "What does not work on Windows yet". Tell us which kind. |
| Odd characters in the terminal view | Use Windows Terminal. |
| `~/.live-room is not a private directory owned by you` | `%USERPROFILE%\.live-room` is a link or junction. Remove it; the server creates a fresh one. |

**To stop the server**, press **Ctrl+C** in its window (or close the window). **To remove W.A.T.C.H. completely**, delete the project folder and `%USERPROFILE%\.live-room`. It changes nothing else on your PC.

---

## Your feedback

Please tell us what happened, even if it was "everything worked". Comment on [the pull request](https://github.com/glo6al/project-watch/pull/2) while it is open, or [open an issue](https://github.com/glo6al/project-watch/issues/new) titled `Windows: …`.

**Please include:**

1. Windows version (**Settings → System → About**: for example "Windows 11 23H2"), and the output of `py --version`.
2. Which agents you use: Claude Code (terminal, desktop app, or IDE), Codex, or Cursor.
3. The last line of `py tests\test_parsers.py` (step 4).
4. What happened at each step, and where it went wrong, with the exact error text. Copy text rather than screenshots where you can.

**Things only a real Windows PC can answer.** Each one is a yes/no or a short paste:

- [ ] Did Windows Firewall ask about `python.exe` when the server started?
- [ ] Did **Unlock** open your browser, and did the room fill with your agents?
- [ ] Did each agent show the right project folder name? Cursor agents especially: we have not seen how Cursor names folders on Windows. Please paste the output of `dir "$env:USERPROFILE\.cursor\projects"`.
- [ ] Is there a Claude desktop app sessions folder? Paste the output of:
  ```powershell
  dir "$env:APPDATA\Claude" ; dir "$env:APPDATA\Claude\claude-code-sessions" -ErrorAction SilentlyContinue
  ```
- [ ] Did a page an agent wrote show as a rendered preview at its desk? Which folder was it in (local disk, OneDrive, network, Dev Drive)?
- [ ] After **Ctrl+C**, could you start the server again straight away, or did it say the port was busy, and for how long?
- [ ] Did the terminal view draw properly in Windows Terminal?
- [ ] Did "Open in Claude" or "Open in Codex" at a desk open the app?

**Security:** if you find a way for an agent, a page it wrote, or another program to reach something this guide or the README says it cannot (the key in `%USERPROFILE%\.live-room`, files outside a preview's folder, the port), please **do not post it publicly**. Report it privately through the repository's [security advisory page](https://github.com/glo6al/project-watch/security/advisories/new).

Thank you for trying it.
