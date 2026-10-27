"""Copy the current level from state/ into web/level/, for the browser build to play: the
mission, the records its last live refresh saved, Claude's briefing log, the leaderboard's runs
on it, and the line Claude wrote about each record it attacked (from the recordings).

Everything here gets published with the web build, so only snapshot a level built on an
account whose data is synthetic.

    python3 web/snapshot.py
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import connect_doom  # noqa: E402
import leaderboard  # noqa: E402
import play  # noqa: E402

OUT = ROOT / "web" / "level"


def main():
    mission = connect_doom.read_json(connect_doom.MISSION)
    if not mission or connect_doom.is_offline(mission):
        sys.exit("No live level in state/: run ./raid.sh new first.")
    saved = connect_doom.read_json(connect_doom.SAVED)
    if not saved or saved.get("mission") != connect_doom.mission_key(mission):
        sys.exit("This level has no saved records: play it once with Wi-Fi.")
    level = leaderboard.level_key(mission)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mission.json").write_text(json.dumps(mission, indent=1))
    (OUT / "saved-records.json").write_text(json.dumps(saved, indent=1))
    shutil.copy(connect_doom.STATE / "run.jsonl", OUT / "run.jsonl")
    runs = [line for line in leaderboard.log_path().read_text().splitlines()
            if line.strip() and json.loads(line).get("level") == level]
    (OUT / "leaderboard.jsonl").write_text("".join(f"{line}\n" for line in runs))
    records = {m["pid"]: m for m in saved["monsters"]}
    said = play.recorded_lines(mission, records)
    (OUT / "said.json").write_text(json.dumps({str(pid): line for pid, line in said.items()}, indent=1))
    print(f"web/level: {mission['question'][:60]}... {len(saved['monsters'])} records, "
          f"{len(runs)} leaderboard runs, Claude's line for {len(said)} records")


if __name__ == "__main__":
    main()
