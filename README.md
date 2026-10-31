# Aegis

A private assistant that does things on your Windows PC. You say what you want, in a sentence or out loud. It does it, on this PC and on the web, and shows you the proof. The AI runs entirely on your computer.

## Start

Double-click **`Aegis.bat`**. That's it.

- Press **Alt+Shift+Space** anywhere and just talk. The overlay opens with the microphone already on, and your words appear as you say them. Start typing instead and the microphone switches off.
- In the overlay, **Space** starts and finishes listening, **Enter** sends, and **Esc** closes it and turns the microphone off. **Alt+Shift+V** always starts listening, and finishes when pressed again. If another app owns those keys, Aegis picks a free pair and shows them on its home screen.
- Once you've asked, the overlay shrinks to one see-through line showing what Aegis is doing, and your keyboard goes back to whatever you were doing. Drag it anywhere. Change how see-through it is in Settings.
- When it needs you, that line grows into a card. Click it, or press **Alt+Shift+Space** and answer with the keyboard.
- Closing the window keeps Aegis in the tray, so reminders and hotkeys keep working. Right-click the tray icon to quit.

The first run on a new machine needs the internet once, to set up Python packages and download the speech model and language model (Aegis offers a button for the language model). After that, it runs with Wi-Fi off.

## What you can ask

| | |
|---|---|
| **The web** | "Go on Wikipedia, get the details of Virat Kohli and save them on my desktop", "who is the CEO of Microsoft? check online", "what's the latest on ISRO?" |
| **Websites** | "Play Buzz by Kr$na on YouTube", "go to this site and fill in the form", "search for shoes on this shop and tell me the cheapest". It drives a real browser, and hands you the window for sign-ins |
| **Any app** | "Send hi to Ritik on WhatsApp", "in this window, press Save". It reads an app's buttons and boxes the way a screen reader does, so apps with no tool of their own still work |
| **Code** | "Write merge sort in new.c on my desktop", "fix the bug in buggy.py". C, C++, Python, JSON and JavaScript are checked before they're saved |
| **Your PC** | "Open Notepad", "put VS Code on the left half", "what's using my memory?", "set the volume to 30", "how much disk space is left?" |
| **Files** | "Find my resume", "what's taking up space in Downloads?", "tidy my Downloads folder", "rename this to final.docx", "zip that folder" |
| **Documents** | "Summarise report.pdf", "convert this to PDF", "write a letter to Sam and save it as a Word file", "mask the emails in this file" |
| **Data** | "Which month had the highest sales in sales.csv?", "make a bar chart from it" |
| **Your screen** | "Take a screenshot", "what's on my screen?", "what does this error mean?", "record my screen for 30 seconds" |
| **Media** | "Merge these two videos", "cut this from 0:30 to 2:15", "remove the silence from this lecture", "transcribe this meeting" |
| **Memory and time** | "Remember I prefer dark roast", "remind me at 5pm to call Sam", "add milk to my to-do list", "every day at 9, list my new downloads" |
| **Your own notes** | "Index my Documents folder", then "what did my notes say about the budget?", with answers citing the file |
| **Anything else** | It can write and run a short script, after showing it to you and getting your OK |

## Why it was rebuilt

The first version was measured before anything changed, on 12 everyday requests against the real app. Its code is in this repository's history, up to commit `786462c`.

| | Before | Now |
|---|---|---|
| Requests done correctly | **2 of 12** | **12 of 12** |
| False success | "Open notepad" took a screenshot and reported **Done**; a question about a CSV ran online research | A reply with no action behind it is challenged, then flagged |
| Time per request | Median 10 s, up to 24 s | Median 4 s, up to 10 s |
| Processes to keep alive | 3, plus Ollama | 1, which runs Ollama itself |

The model was not the main problem. The same small model called natively chose the right tool 5 times out of 5, in 1 to 4 seconds. What failed was the pipeline around it: a router model picked one of seven "sandboxes", a second model call picked a tool from a three-tool shortlist inside a 1,536-token window, and an 1,100-line orchestrator full of regular expressions tried to repair the results. Each hop lost information.

This version replaces all of that with one model, one loop, and real tool calling. The `Levin` design set was a useful input: verification before completion, one process, one choke point for permissions, per-action grants. These are built now, in Python, instead of after a 14-month Rust rewrite.

## How it works

```
You ──> Agent loop ──> core tools + what fits the request ──> model (Ollama, on the GPU)
            ^                                                    │
            │  result, or the exact error and what to try next   │ tool call
            │                                                    v
            └── receipt <── verified result <── permission check <┘
                   │
                   └──> does this match what was asked? If not, back to work.
```

- **Honest by construction.** Tools report what they checked: a screenshot is reopened and measured, a file is read back, a merged video's length is compared to its inputs, a moved window's position is re-read. If there is no way to prove something, such as typed keystrokes, the receipt says so. A model reply of "I've done it" with no tool call behind it is sent back, and if it persists it's flagged as unconfirmed.
- **Permission where it matters.** Looking at things just happens. Changes that are easy to reverse run and offer an **Undo** button. Deleting, overwriting, closing apps, typing into windows and running code always ask first, and show exactly what will happen. "Always allow" applies to one kind of action and can be revoked in Settings. Scheduled tasks can never approve anything.
- **The model is forgiven, not trusted.** Wrong argument names, wrong types and guessed paths like `C:/Users/user/Desktop` are repaired before they reach a tool. Screenshot scope and reminder delays are checked against your actual words. So "in 10 seconds" stays 10 seconds even when the model says 10 minutes.
- **It keeps going.** Passing faults are retried by themselves. "I can't" gets pushed back with other ways to try. Finished work is compared with the request, and anything missing goes back to the model.
- **It works things out.** A question from the model goes back once, telling it to choose a sensible default. If it asks again, it really needs you, and you get a clear card with the choices.
- **You can see it working.** While it works, the overlay shrinks to one line showing the current step, and your keyboard goes back to what you were doing. It only grows when it has something to say.
- **Files and pages are data.** Tool output is marked as such for the model, and anything risky still has to pass your approval.

## Asking you

| | |
|---|---|
| **Just happens** | Reading and looking. Changes with an **Undo** button, including replacing a file you named |
| **Asks** | Replacing a file you didn't name, deleting, closing apps. Answer with **Enter** or **Esc**, or say yes for the whole task |
| **Always asks** | Sending anything to other people or sites, and running code. These can't be set to "Always", because Undo can't reach them |
| **Your job** | Passwords and "are you a human" checks. Aegis asks you to do these in the browser window, then carries on |

After a request has read web pages, an earlier "Always" no longer covers anything that reaches outside this PC, because a page could be trying to steer it.

## What stays on this PC

**The AI always runs here.** Your requests, files and conversations are processed by the local model and stored on this PC. No cloud AI is involved.

**The internet is used like you would use it.** Web access is on by default, so Aegis can read pages, search, and use websites for you. Only what a task needs goes out: the address of a page, a search, or what you asked it to type into a site. Searches go to Bing and Wikipedia.

**Offline mode** (Settings) removes all of that. While it's on:

- Aegis's own process cannot open any connection except to loopback, enforced by an interpreter audit hook rather than by each tool behaving. Scripts it runs get the same guard, and blocked attempts are counted in Settings.
- The web tools and browser control disappear, and it says plainly that it can't read websites.
- Links open in your own browser, and the model server runs on a private port with Ollama's cloud features off.

## The model

Tested on this laptop (Core Ultra 5 125H, Arc iGPU, 16 GB) with `tests/eval_real.py`: 42 pieces of real work, such as research saved to a file, writing and fixing code, using websites and apps, and knowing when to ask. Each is scored by what actually happened, not by what the model said:

| Model | Correct | Median | Slowest | Memory in use | Notes |
|---|---|---|---|---|---|
| **`gemma4:e4b`** (default) | **40-42/42 over three runs** | **15 s** | 148 s | 8.2 GB | The best at multi-step work and at choosing the right tool. Can see images |
| `gemma4:e2b` (used when memory is short) | 38/42 | 15 s | 56 s | 6.0 GB | Quicker per word, but makes more wrong turns. Can see images |
| `qwen3.5:4b` | 39/41 | 30 s | 301 s | not measured | Rereads its whole prompt on every step, so it is slow |

Aegis picks `gemma4:e4b` when at least 8.4 GB is free, and otherwise `gemma4:e2b`, so it doesn't push your open apps into the page file. Choosing a model in Settings overrides this.

The Arc GPU matters: Ollama skips integrated GPUs unless told otherwise, and the first version was running on the CPU the whole time. Aegis runs its own Ollama server with the GPU enabled, and your normal Ollama is left alone.

The model is unloaded after 10 idle minutes, so the memory comes back on its own, and the next request takes a few seconds longer while it reloads.

## Speech recognition

Tested on 90 recordings of real people on their own microphones from Mozilla Common Voice (60 speakers with Indian accents, 30 with other accents), plus 24 spoken commands at normal volume and again quiet with room noise. Lower is better. Word error rates count "thirty percent" written as "30%" as a mistake, so the command columns overstate the errors.

| Model | Indian accents | Other accents | Commands | Quiet, noisy commands | Speed | Memory |
|---|---|---|---|---|---|---|
| **Parakeet TDT 0.6 v2** (default) | **5.6%** | **4.3%** | 6.3% | **11.7%** | **13x real time** | About 1 GB |
| `large-v3-turbo` | 7.1% | 7.4% | 8.1% | 14.4% | Under 1x, too slow to use | About 1 GB |
| `distil-large-v3` | 8.2% | 13.2% | 8.1% | 16.2% | About 1x, too slow to use | About 1 GB |
| Parakeet TDT 0.6 v3 | 7.7% | 11.7% | 5.4% | 15.3% | 6x real time | About 1 GB |
| `small.en` | 9.0% | 10.9% | 7.2% | 16.2% | 4x real time | About 0.5 GB |
| `tiny.en` (the old default) | 18.8% | 27.2% | 9.0% | 30.6% | 22x real time | About 0.2 GB |

Speeds other than Parakeet v2's and `tiny.en`'s were measured while another model was also running, so treat them as lower bounds.

- **It runs on the CPU, in its own process.** Loading the model freezes a Python process for about 2.5 seconds, which would lose your first words and hang the window, so it can't be in the main one. That process exits after 15 idle minutes and returns its memory; the next request starts it again in about 3.5 seconds, without losing anything you've said, because the microphone doesn't wait for it.
- **A 6-second sentence takes about half a second** to transcribe. That's fast enough to show your words while you talk.
- **Only what's clearly heard is sent automatically.** On those same recordings, 99% of real speech passed the confidence check. Speech from across a room (simulated), unintelligible speech and noise never did, though several people talking at once passed 30% of the time. Anything doubtful waits in the box for you to read. A recording you didn't mention needs your approval too.
- **Common mishearings get repaired**, narrowly: "close crow" becomes "close Chrome", "report dot pdf" becomes "report.pdf". Ordinary sentences are left alone.
- **English only.** Parakeet v2 doesn't do other languages, and Hindi mixed into a sentence will come out as similar-sounding English.

## Where things are

| | |
|---|---|
| Your data (conversations, memory, reminders, log) | `%LOCALAPPDATA%\Aegis2\aegis.db` |
| Logs | `%LOCALAPPDATA%\Aegis2\logs\aegis.log` |
| Screenshots | `Pictures\Screenshots` |
| Recordings | `Videos\Aegis Recordings`, `Music\Aegis Recordings` |
| Charts | `Pictures\Aegis Charts` |
| Other new files | `Documents\Aegis`, or wherever you say |

The only thing Aegis writes outside those places is a small per-user registry entry that gives it its own name on notifications.

## Limits

- **Windows only.** The first version's macOS and Linux branches were not carried over.
- **Removed from the first version:** Google Drive transfers, LAN scanning and Wake-on-LAN, profiles, and the "sandbox" and "memory layer" framing. Profiles and sandboxes described isolation that never existed.
- **Browser sign-ins:** Aegis's browser uses a profile of its own, so you sign in to each site once there. It never types passwords and never answers "are you a human" checks; it asks you to.
- **WhatsApp:** sending works through the desktop app's accessibility interface, so a WhatsApp update could break it. If it does, Aegis says so and sends nothing.
- **Reading the current web page** works by looking at your screen, not by reading the page's text.
- **Web access** (`web_search`, `web_fetch`, off by default) was only tested against a local server, because the test environment had no internet. Treat it as untested against real sites.
- **Voice** accuracy was measured on recordings, not on your own voice. **Settings > Voice > Try it** shows exactly what Aegis hears. If your laptop's built-in microphone is noisy, choose a headset there.
- **Listening stops by itself** after 1.5 seconds of silence, or 3.5 seconds if your sentence ended on a word like "and". A longer pause sends what you've said so far.
- **Screen recording audio** can include your microphone but not sound playing on the PC. FFmpeg cannot capture that without a virtual audio device.
- **A small model still makes mistakes.** It can misread a long document or pick a clumsy approach. Receipts exist so you can see what it did, and approvals exist so its mistakes stay small. A file it reads could also try to talk it into something; changes that don't need approval, like moving a file, can be undone.
- **The Activity log** detects edits made after the fact. It does not stop someone with access to your account from replacing the whole database.

## Development

```bash
python tests/test_aegis.py
```

```bash
python tests/test_control.py
```

```bash
python tests/eval_real.py
```

```bash
python tests/eval_live.py
```

The first two run fast, using real files, real FFmpeg, real Windows OCR, real speech recognition and a real browser on a local test site, with only the language model scripted. `eval_real.py` gives the real model real work on a throwaway Desktop and judges it by what actually happened; it uses the internet for research requests, but sends nothing to anyone. `eval_live.py` covers everyday commands; it opens and closes Notepad and briefly changes the volume, then restores your volume and clipboard. Add `--model NAME` to either to try another model.

The interface lives in `web/` and is already built into `web/dist`, so Node is only needed to change it:

```bash
cd web && npm install && npm run build
```

`tests/serve_dev.py` runs the server without the native window, using a separate data folder, for testing in a browser.

```
aegis/
  agent.py       the loop, the honesty rules, permissions, retries, checking the result
  browser.py     a real browser Aegis can see and drive
  uia.py         reading and operating any Windows app
  checks.py      checking the code and documents the model wrote
  toolkit.py     declaring tools, repairing arguments, choosing tools
  llm.py         private Ollama server, streaming, tolerant call parsing
  netguard.py    offline enforcement
  store.py       SQLite: conversations, memory, reminders, audit chain
  paths.py       turning what people say into real paths
  server.py      local API, guarded against other websites
  shell.py       window, quick-ask overlay, tray, hotkeys
  voice.py       microphone, speech detection, live transcription, text-to-speech
  speech.py      speech models: download, worker process, mishearing repair
  scheduler.py   reminders and scheduled requests
  tools/         82 tools: desktop, files, docs, media, knowledge, code, web, control
web/             React interface
tests/           test suites, and evaluations against the real model
```
