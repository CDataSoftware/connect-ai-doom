#!/usr/bin/env python3
"""Connect AI × DOOM on Windows: Claude scouts your data through the Connect AI MCP server, and
the Doom level is built from what Connect AI let it reach. (On a Mac: ./raid.sh.)
  python raid.py new                 ask Claude the default question (about a minute), then play
  python raid.py new -q "<question>" ask Claude your own question, then play
  python raid.py replay              play the last level again (no new Claude run)
  python raid.py booth               replay over and over: the next game starts a few seconds after one ends
  python raid.py booth offline       the same with sample data; booth plays offline anyway without a token
  python raid.py offline             sample data, no network
  python raid.py brief-only [question]  only ask Claude, no game (for rehearsing)
  python raid.py login               save your Connect AI email and token in Windows Credential Manager
  python raid.py doctor              check this PC is ready to play (build, token, network, screen)
  --claude-plays                     Claude plays instead of you; add it to any mode above
  --loop                             auto replay (on unless --no-loop): once the level is clear, you
                                     die, or Claude's run ends, every record comes back and the game
                                     starts over, all day
  --no-loop                          auto replay off: play the level once and stop there
  --offline-plays                    Claude plays offline: a scripted player that narrates with
                                     Claude's lines from earlier runs, no network, no AI tokens
  --god-mode                         players can't die
  --saved-data                       play the level's last saved records; nothing asks Connect AI
  --no-music, --no-sound-effects     start with Freedoom's soundtrack, or the sound effects, off
All seven switches are also in the game's Options menu (Esc > Options). With no mode,
python raid.py is python raid.py new. Arguments after the switches go to the game (e.g. -warp 2).
The side panel (http://localhost:8666) opens in your browser; NO_PANEL=1 skips it. Its question
box ends the game, briefs the new question and plays its level, in the same raid.
NOGORE=1: hits throw bullet puffs instead of blood, and a kill vanishes in teleport fog.
Kills write nothing back: each is logged and its record read again live through Connect AI.
"""
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import compat

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
BIN = ROOT / "vendor" / "bin"
PSDOOM = BIN / "psdoom.exe"
ZDBSP = BIN / "zdbsp.exe"
WAD = ROOT / "wads" / "freedoom2.wad"
BRIDGE = ROOT / "connect_doom.py"
MISSION = STATE / "mission.json"
OPTIONS = STATE / "options.json"
PY = sys.executable
# What the game build carries (tools/setup-win.sh stamps the same checksum after building)
BUILD_INPUTS = ["patches/psdoom-ng-connect-doom.patch", "patches/psdoom-ng-windows.patch", "tools/setup-win.sh"]
BUILD_STAMP = ROOT / "vendor" / "src" / "psdoom-ng" / ".connect-doom-patch"
CREDENTIAL = "connect-doom"   # Credential Manager target: connect-doom:<email>

# Modern FPS controls, as in GZDoom: WASD to move, the mouse to turn, left click or Space to
# fire, walk into a door (or E, or right click) to open it, Shift to run, P to pause; the arrows
# turn too. Written fresh each launch (values are keyboard scancodes), so a booth PC always plays the same.
CONTROLS = """key_up 17
key_down 31
key_strafeleft 30
key_straferight 32
key_left 75
key_right 77
key_fire 57
key_use 18
key_speed 42
key_strafe 56
use_mouse 1
mouseb_fire 0
mouseb_strafe -1
mouseb_forward -1
mouse_sensitivity 6
"""
# The mouse turns but doesn't walk; its right button opens doors. show_endoom 0: closing the
# window quits at once, with no ENDOOM text screen waiting for a key.
CONTROLS_EXTRA = "novert 1\ngrabmouse 1\nmouseb_use 1\nshow_endoom 0\n"


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def py(*args, **kw):
    """Run one of this repo's Python scripts with this Python."""
    return subprocess.run([PY, *map(str, args)], cwd=ROOT, **kw)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- Windows: credentials, jobs, sleep

def _credential_api():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, advapi


def credential_write(user, secret):
    ctypes, CREDENTIAL, advapi = _credential_api()
    blob = secret.encode("utf-8")
    cred = CREDENTIAL(Type=1, TargetName=f"{CREDENTIAL}:{user}", Comment="Connect AI PAT (connect-doom)",
                      CredentialBlobSize=len(blob),
                      CredentialBlob=ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                                 ctypes.POINTER(ctypes.c_ubyte)),
                      Persist=2, UserName=user)   # 1: a generic credential; 2: kept on this PC
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "Saving the token in Credential Manager failed")


def credential_read(user):
    ctypes, CREDENTIAL, advapi = _credential_api()
    found = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(f"{CREDENTIAL}:{user}", 1, 0, ctypes.byref(found)):
        return None
    try:
        c = found.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
    finally:
        advapi.CredFree(found)


def contain_children():
    """Put this process in a job that Windows closes when it exits, for any reason: everything
    it started (the game, the panel, the poller, Claude) goes with it, so nothing lingers on the
    screen or holds the panel's port. The browser breaks away from it (see open_panel)."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800   # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # 9: extended limits
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
    return job   # the handle stays open until this process exits; closing it ends the job


def keep_awake():
    """Keep the screen from dimming or locking while the raid runs (until this process exits)."""
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x2 | 0x1)   # CONTINUOUS | DISPLAY | SYSTEM


def screen_size():
    import ctypes
    u32 = ctypes.windll.user32
    u32.SetProcessDPIAware()
    return u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)


# ---------------------------------------------------------------- the build and the token

def build_sum():
    return hashlib.sha256(b"".join((ROOT / p).read_bytes() for p in BUILD_INPUTS)).hexdigest()


def build_current():
    try:
        return BUILD_STAMP.read_text().strip() == build_sum()
    except FileNotFoundError:
        return False


def login():
    user = input("Connect AI email: ").strip()
    pat = getpass.getpass("Connect AI personal access token: ").strip()
    credential_write(user, pat)
    STATE.mkdir(exist_ok=True)
    (STATE / "account").write_text(user + "\n")
    print("Saved. Now run python raid.py new")


def load_token():
    """The token: the environment, then Credential Manager (python raid.py login stores it), then,
    if CONNECT_AI_OP_ITEM names one, 1Password once, copied into Credential Manager."""
    if not os.environ.get("CONNECT_AI_USER") and (STATE / "account").exists():
        os.environ["CONNECT_AI_USER"] = (STATE / "account").read_text().strip()
    user = os.environ.get("CONNECT_AI_USER")
    if not os.environ.get("CONNECT_AI_PAT") and user:
        os.environ["CONNECT_AI_PAT"] = credential_read(user) or ""
    if not os.environ.get("CONNECT_AI_PAT") and os.environ.get("CONNECT_AI_OP_ITEM") and user:
        r = subprocess.run(["op", "--account", "my.1password.com", "read",
                            os.environ["CONNECT_AI_OP_ITEM"] + "/credential"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            os.environ["CONNECT_AI_PAT"] = r.stdout.strip()
            credential_write(user, os.environ["CONNECT_AI_PAT"])
    if not os.environ.get("CONNECT_AI_PAT"):
        os.environ.pop("CONNECT_AI_PAT", None)
    return bool(os.environ.get("CONNECT_AI_PAT"))


# ---------------------------------------------------------------- the switches

def parse(argv):
    """The mode and switches, and what's left over for the game."""
    o = {"mode": "live", "question": "", "plays": False, "loop": None, "booth": False, "god": False,
         "saved": False, "offline": False, "nomusic": False, "nosfx": False}
    flags = {"--claude-plays": ("plays",), "--offline-plays": ("plays", "offline"), "--god-mode": ("god",),
             "--saved-data": ("saved",), "--no-music": ("nomusic",), "--no-sound-effects": ("nosfx",)}
    modes = {"doctor": "doctor", "new": "live", "offline": "demo", "--demo": "demo",
             "replay": "again", "--again": "again"}
    args = list(argv)
    while args:
        a = args[0]
        if a == "login":
            login()
            sys.exit(0)
        elif a in modes:
            o["mode"] = modes[a]
        elif a == "booth":
            o["booth"] = True
        elif a in flags:
            for k in flags[a]:
                o[k] = True
        elif a in ("--loop", "--auto-replay"):
            o["loop"] = True
        elif a == "--no-loop":
            o["loop"] = False
        elif a in ("-q", "--question"):
            if len(args) < 2:
                say("-q needs a question.")
                sys.exit(2)
            o["question"] = args[1]
            args.pop(0)
        elif a in ("brief-only", "brief"):
            o["mode"], o["question"], args = "brief", " ".join(args[1:]), [a]
        elif a in ("-h", "--help", "help") or not a.startswith("-"):
            # A mistyped mode stops here: falling through to `new` would start a paid briefing.
            print(__doc__)
            if a in ("-h", "--help", "help"):
                sys.exit(0)
            say(f"Unknown mode: {a}")
            sys.exit(2)
        else:
            break   # the rest go to the game
        args.pop(0)
    if o["loop"] is None:
        o["loop"] = True   # auto replay is on unless --no-loop: a cleared level or a death starts over
    if o["booth"] and o["mode"] != "demo":
        o["mode"] = "again"   # booth loops the last level, or offline with sample data; never a new briefing
    return o, args


def load_options(o):
    """The Options menu's switches: the flags, or in a booth the last game's (state/options.json).
    A new level asked for from the panel starts from the switches of the game before it, too."""
    saved = read_json(OPTIONS)
    if saved is not None:
        o.update(plays=bool(saved.get("claude_plays")), loop=bool(saved.get("auto_replay")),
                 god=bool(saved.get("god_mode")), saved=bool(saved.get("saved_data")),
                 offline=bool(saved.get("offline_claude")), nomusic=not saved.get("music", 1),
                 nosfx=not saved.get("sound_effects", 1))
    os.environ.update(PSDOOM_OPT_GOD=str(int(o["god"])), PSDOOM_OPT_SAVED=str(int(o["saved"])),
                      PSDOOM_OPT_OFFLINE=str(int(o["offline"])), PSDOOM_OPT_MUSIC=str(int(not o["nomusic"])),
                      PSDOOM_OPT_SFX=str(int(not o["nosfx"])))


def switch_flags(o):
    """The switches as flags again, for the next game of a booth."""
    return ((["--claude-plays"] if o["plays"] else []) + (["--loop"] if o["loop"] else ["--no-loop"])
            + (["--god-mode"] if o["god"] else []) + (["--saved-data"] if o["saved"] else [])
            + (["--no-music"] if o["nomusic"] else []) + (["--no-sound-effects"] if o["nosfx"] else [])
            + (["--offline-plays"] if o["offline"] else []))


# ---------------------------------------------------------------- doctor

def doctor(o):
    """One line per check; an x line says what to run. Exits 1 if anything needed to play is
    missing. Without a token only offline play is checked: offline and booth work without one."""
    bad = False

    def ok(s):
        print(f"  ✓ {s}")

    def no(s):
        nonlocal bad
        bad = True
        print(f"  ✗ {s}")

    def note(s):
        print(f"  - {s}")

    import platform
    print(f"connect-doom on Windows {platform.version()} ({platform.machine()})")
    if PSDOOM.exists() and ZDBSP.exists() and WAD.exists():
        ok("game, map builder and Freedoom art built")
    else:
        no("the game isn't fully built: run setup.cmd (its log is vendor\\build.log)")
    ok(f"Python {platform.python_version()}")
    try:
        import PIL  # noqa: F401
        ok("Pillow")
    except ImportError:
        no("Pillow is missing: run setup.cmd")
    if shutil.which("claude"):
        ok("Claude Code CLI, for python raid.py new")
    else:
        note("no Claude Code CLI: replay and offline work, new needs it")
    answers = False
    if os.environ.get("CONNECT_AI_PAT"):
        ok(f"Connect AI token for {os.environ.get('CONNECT_AI_USER', '?')}")
        try:
            import connect_ai_mcp
            n = len(connect_ai_mcp.Client(timeout=15).connections())
            ok(f"Connect AI answers: {n} connections")
            answers = True
        except Exception as e:
            no(f"Connect AI didn't answer ({e}). Check the network and the token, or play python raid.py offline")
    else:
        note("no Connect AI token: python raid.py offline and python raid.py booth play sample data; "
             "python raid.py login for yours")
    mission = read_json(MISSION)
    if mission:
        ok(f"a level to replay: {mission.get('question', '')[:70]}")
        if answers and any("query" in r for r in mission.get("rooms") or []):
            print(f"  … running this level's queries (up to {os.environ.get('CONNECT_AI_TIMEOUT', '30')} s each)")
            r = py(BRIDGE, "check", capture_output=True, text=True)
            for line in r.stdout.splitlines():
                status, _, text = line.partition("\t")
                {"ok": ok, "error": no}.get(status, note)(text)
    elif os.environ.get("CONNECT_AI_PAT"):
        note("no level yet: run python raid.py new once, then replay plays it")
    if build_current():
        ok("the game build is current")
    else:
        no("the game was built from an older patch: run setup.cmd again")
    port = int(os.environ.get("PANEL_PORT", "8666"))
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        ok(f"port {port} free for the panel")
    except OSError:
        no(f"port {port} is taken by another app: run with PANEL_PORT=8667")
    w, h = screen_size()
    note(f"screen: {w}x{h}")
    if bad:
        print("Not ready: fix the ✗ lines.")
        sys.exit(1)
    if os.environ.get("CONNECT_AI_PAT"):
        print("Ready: python raid.py booth plays the last level game after game, python raid.py new makes a fresh one.")
    else:
        print("Ready to play offline: python raid.py booth plays sample data game after game.")


# ---------------------------------------------------------------- the raid

def booth(o, extra):
    """Back-to-back games. The screen stays awake between games; Ctrl-C stops it. BOOTH_PAUSE sets
    the seconds between games, BOOTH_GAMES a limit. The Options menu's switches carry over from
    game to game (play.py keeps them in state/options.json); a new booth session starts from the flags."""
    if o["mode"] == "again" and not MISSION.exists():
        say("No level on this PC yet (python raid.py new makes one), so booth mode plays offline, with sample data.")
        o["mode"] = "demo"
    game = "offline" if o["mode"] == "demo" else "replay"
    OPTIONS.unlink(missing_ok=True)
    os.environ["CONNECT_DOOM_BOOTH"] = "1"
    games = 0
    while True:
        r = subprocess.run([PY, __file__, game, *switch_flags(o), *extra], cwd=ROOT)
        if r.returncode:
            say("The game didn't start, so booth mode stopped.")
            sys.exit(1)
        games += 1
        os.environ["CONNECT_DOOM_PANEL_OPEN"] = "1"   # the panel's browser tab is already open
        if os.environ.get("BOOTH_GAMES") and games >= int(os.environ["BOOTH_GAMES"]):
            return
        pause = float(os.environ.get("BOOTH_PAUSE", "10"))
        print(f"Next game in {pause:g} seconds. Ctrl-C to stop.")
        time.sleep(pause)


def real_level():
    """mission.json is a real level (its rooms carry queries), not the offline sample."""
    rooms = (read_json(MISSION) or {}).get("rooms") or [{}]
    return any("query" in r for r in rooms) and not all("rows" in r for r in rooms)


class Raid:
    def __init__(self, o, extra):
        self.o, self.extra = o, extra
        self.procs = []        # the panel: for the whole raid
        self.game_procs = []   # the poller and play.py loop: for one game
        self.game = None
        self.port = int(os.environ.get("PANEL_PORT", "8666"))

    def start(self, args, log=None, **kw):
        err = open(STATE / log, "ab") if log else subprocess.DEVNULL
        return subprocess.Popen([PY, *map(str, args)], cwd=ROOT, stderr=err, **kw)

    def cleanup(self):
        for p in [*self.game_procs, *self.procs] + ([self.game] if self.game else []):
            compat.stop(p, timeout=3)
        for f in ("game.json", "psdoom.pid"):
            (STATE / f).unlink(missing_ok=True)
        # an offline game set the real level aside; put it back for the next replay
        if (STATE / "mission-live.json").exists():
            os.replace(STATE / "mission-live.json", MISSION)

    def open_panel(self):
        """The panel in the default browser. The browser breaks away from this raid's job, so
        ending the raid doesn't close it."""
        if os.environ.get("CONNECT_DOOM_PANEL_OPEN"):
            return
        cmd = ["cmd", "/c", "start", "", f"http://localhost:{self.port}"]
        try:
            subprocess.Popen(cmd, creationflags=0x01000000 | subprocess.CREATE_NO_WINDOW)   # CREATE_BREAKAWAY_FROM_JOB
        except OSError:   # no job of our own, and the one we're in doesn't allow breaking away
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)

    def run(self):
        o = self.o
        # An offline game killed before its cleanup left its sample in mission.json and the real
        # level set aside: put the real level back before anything reads it.
        if (STATE / "mission-live.json").exists():
            os.replace(STATE / "mission-live.json", MISSION)
        # A question asked from the panel of a raid that was killed mid-briefing is stale.
        for f in ("ask.json", "asking.json", "ask-failed.json"):
            (STATE / f).unlink(missing_ok=True)
        keep_awake()

        if os.environ.get("NO_PANEL") != "1":
            if not (ROOT / "panel" / "sprites" / "knight.png").exists():
                py("tools/make_sprites.py", WAD, "panel/sprites", "assets/brand")
            # RAID_PID: the panel's question box ends this raid's game so the next is a new level
            self.procs.append(self.start([BRIDGE, "panel", self.port], env=dict(os.environ, RAID_PID=str(os.getpid()))))
            time.sleep(0.5)
            self.open_panel()

        if o["mode"] == "brief":
            sys.exit(py("brief.py", *([o["question"]] if o["question"] else [])).returncode)
        if o["mode"] == "live":
            py("brief.py", *([o["question"]] if o["question"] else []), stdout=subprocess.DEVNULL)
        elif o["mode"] == "demo":
            # Set the real level aside, not the sample: on a PC that never had a real level,
            # mission.json may hold only the sample.
            if real_level():
                shutil.copyfile(MISSION, STATE / "mission-live.json")
            py(BRIDGE, "poll", "--demo")
        if not MISSION.exists():
            say("No level yet: run python raid.py new first.")
            sys.exit(1)

        while True:
            self.play_one()
            # The game is over, or the panel ended it for a new question.
            for p in self.game_procs:
                compat.stop(p, timeout=3)
            self.game_procs = []
            if not (STATE / "ask.json").exists():
                break
            (STATE / "game.json").unlink(missing_ok=True)   # the panel shows the briefing until the next game
            self.new_level()
        py(BRIDGE, "score")

    def play_one(self):
        o = self.o
        # The last game's agent channel goes first: its state.json still holds that game's switches.
        agent = STATE / "agent"
        agent.mkdir(exist_ok=True)
        for f in agent.iterdir():
            f.unlink(missing_ok=True)
        # The records to start with. Without a network, poll --once falls back to the ones this
        # level last fetched; if it never fetched any, play offline.
        if o["mode"] != "demo" and py(BRIDGE, "poll", "--once").returncode:
            say("Can't reach Connect AI, and this level has no saved records yet: playing offline instead.")
            o["mode"] = "demo"
            shutil.copyfile(MISSION, STATE / "mission-live.json")
            py(BRIDGE, "poll", "--demo")

        # The level for this mission: map, signs, keycard messages (and, with NOGORE=1, no blood or corpses).
        steps = [["tools/make_branding.py", WAD, "assets/brand", "state/brand.wad", "--mission", "state/mission.json"],
                 ["tools/make_arena.py", "state/level-raw.wad", "--mission", "state/mission.json",
                  "--records", "state/cache.json", "--brand"]]
        for step in steps:
            py(*step, check=True)
        subprocess.run([str(ZDBSP), "-o", "state/level.wad", "state/level-raw.wad"], cwd=ROOT,
                       stdout=subprocess.DEVNULL, check=True)
        py("tools/make_deh.py", "state/mission.json", "state/level.deh",
           *(["--no-gore"] if os.environ.get("NOGORE") == "1" else []), check=True)
        # Each room's monsters, after its app's theme (tools/themes.py): a record's and an urgent record's.
        os.environ["PSDOOM_MONSTERS"] = py("tools/themes.py", "monsters", "state/mission.json",
                                           capture_output=True, text=True, check=True).stdout.strip()

        if o["mode"] != "demo":
            self.game_procs.append(self.start([BRIDGE, "poll"], log="poll.log"))

        # The game runs the bridge through cmd.exe (popen and system); a batch file holds the
        # Python and script paths so cmd.exe's quoting never has to.
        bridge = STATE / "bridge.cmd"
        bridge.write_text(f'@"{PY}" "{BRIDGE}" %*\r\n')
        os.environ.update(PSDOOMPSCMD=f'"{bridge}" list', PSDOOMKILLCMD=f'"{bridge}" kill',
                          PSDOOMRENICECMD=f'"{bridge}" hit',
                          PSDOOMVAULTCMD=f'"{bridge}" vault',               # the red door asks Connect AI again, live
                          PSDOOM_LABELS=str(STATE / "labels.tsv"))         # the record under the crosshair

        (STATE / "play.jsonl").unlink(missing_ok=True)   # the panel's Claude plays feed is only for this game
        # The agent channel patched into psdoom: Claude's commands in, game state out. It's always
        # open, so Claude plays can be switched on from the Options menu; play.py loop follows the
        # menu's switches and starts or stops Claude.
        os.environ.update(PSDOOM_AGENT_DIR=str(agent), PSDOOM_AGENT_CLAUDE=str(int(o["plays"])),
                          PSDOOM_AGENT_LOOP=str(int(o["loop"])))
        self.game_procs.append(self.start(["play.py", "loop"], log="play.err"))

        (STATE / "controls.cfg").write_text(CONTROLS)
        (STATE / "controls-extra.cfg").write_text(CONTROLS_EXTRA)
        (STATE / "game.json").write_text(json.dumps({"mode": "claude" if o["plays"] else "player",
                                                     "started": int(time.time())}) + "\n")

        # -nopslev: spawn monsters into the three arenas of the level
        # -psallusers: don't filter monsters by user
        cmd = [str(PSDOOM), "-iwad", str(WAD), "-file", "state/level.wad", "state/brand.wad",
               "-deh", "state/level.deh", "-nopslev", "-psallusers", "-warp", "1", "-skill", "1", "-window",
               "-geometry", os.environ.get("GEOMETRY", "1280x800"), "-config", "state/controls.cfg",
               "-extraconfig", "state/controls-extra.cfg", *self.extra]
        # SDL_STDIO_REDIRECT=0: the game's output comes here, not to stdout.txt next to the exe.
        # PSDOOM_LAUNCHER_PID: if this raid dies without cleaning up, the game quits by itself.
        env = dict(os.environ, SDL_STDIO_REDIRECT="0", PSDOOM_LAUNCHER_PID=str(os.getpid()))
        with open(STATE / "game.log", "wb") as log, open(STATE / "psdoom.err", "wb") as err:
            self.game = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.PIPE)
            (STATE / "psdoom.pid").write_text(str(self.game.pid))
            tee = threading.Thread(target=tee_errors, args=(self.game.stderr, err), daemon=True)
            tee.start()
            self.game.wait()
            tee.join(2)
        self.game = None
        (STATE / "psdoom.pid").unlink(missing_ok=True)

    def new_level(self):
        """A question asked from the panel's box: Claude briefs it while the panel shows the
        briefing, and the next game is its level. If the briefing fails or finds no rooms, the
        last level comes back and the panel says so."""
        os.replace(STATE / "ask.json", STATE / "asking.json")
        question = read_json(STATE / "asking.json")["question"]
        say(f"New question from the panel: {question}")
        shutil.copyfile(MISSION, STATE / "mission-before.json")
        with open(STATE / "brief.err", "ab") as err:
            briefed = py("brief.py", question, stdout=subprocess.DEVNULL, stderr=err).returncode == 0
        if briefed and (read_json(MISSION) or {}).get("rooms"):
            for f in ("mission-live.json", "mission-before.json"):   # a real level now, not the offline one
                (STATE / f).unlink(missing_ok=True)
            self.o["mode"] = "live"
        else:
            os.replace(STATE / "mission-before.json", MISSION)
            (STATE / "ask-failed.json").write_text(json.dumps({"question": question, "at": time.time()}))
            say("Claude couldn't build a level for that question; playing the last level again.")
        (STATE / "asking.json").unlink(missing_ok=True)
        load_options(self.o)


def tee_errors(stream, log):
    """The game's stderr to state/psdoom.err, and its KILL and ERROR lines to this console too."""
    for line in iter(stream.readline, b""):
        log.write(line)
        log.flush()
        if b"KILL" in line or b"ERROR" in line:
            sys.stderr.write(line.decode("utf-8", "replace"))
            sys.stderr.flush()


def main(argv):
    if not compat.WINDOWS:
        say("raid.py is the Windows launcher; on a Mac, run ./raid.sh.")
        sys.exit(2)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    STATE.mkdir(exist_ok=True)
    # UTF-8 for every Python this starts: Windows' default code page can't read the panel's files
    # or print every record's name.
    os.environ["PYTHONUTF8"] = "1"
    o, extra = parse(argv)

    if o["mode"] != "doctor":
        if not PSDOOM.exists() or not WAD.exists():
            say("The game isn't built yet: run setup.cmd first.")
            sys.exit(1)
        if BUILD_STAMP.exists() and not build_current():
            # A pull brought a new patch: rebuild just the game (no downloads).
            say("The game patch changed since this PC built it: rebuilding the game (about a minute)...")
            if subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(ROOT / "setup.ps1"), "-Game"], cwd=ROOT).returncode:
                say("Rebuilding the game failed: run setup.cmd (its log is vendor\\build.log).")
                sys.exit(1)
    if o["mode"] in ("live", "brief") and not shutil.which("claude"):
        say("The briefing needs Claude Code's CLI (claude), signed in. Or play the last level: "
            "python raid.py replay, or play offline: python raid.py offline")
        sys.exit(1)

    if o["mode"] != "demo" and not load_token():
        if o["booth"]:
            say("No Connect AI account on this PC, so booth mode plays offline, with sample data.")
            o["mode"] = "demo"
        elif o["mode"] != "doctor":
            say("No Connect AI token: run python raid.py login (or set CONNECT_AI_USER and CONNECT_AI_PAT).")
            say("To play offline with sample data: python raid.py offline")
            sys.exit(1)

    contain_children()
    if o["booth"]:
        keep_awake()
        booth(o, extra)
        return
    if not os.environ.get("CONNECT_DOOM_BOOTH"):
        OPTIONS.unlink(missing_ok=True)
    load_options(o)
    if o["mode"] == "doctor":
        doctor(o)
        return
    raid = Raid(o, extra)
    try:
        raid.run()
    finally:
        raid.cleanup()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        sys.exit(130)
