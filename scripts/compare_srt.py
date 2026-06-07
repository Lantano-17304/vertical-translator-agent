"""Compare two SRT files and print structural/text differences."""
from __future__ import annotations

import re
import sys
from pathlib import Path

TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})$")


def parse_srt(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[dict[str, str]] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        idx = lines[0].strip()
        match = TIME_RE.match(lines[1].strip())
        if not match:
            continue
        body = "\n".join(lines[2:]).strip()
        cues.append(
            {
                "index": idx,
                "start": match.group(1),
                "end": match.group(2),
                "text": body,
            }
        )
    return cues


def to_sec(ts: str) -> float:
    hours, minutes, rest = ts.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def main() -> None:
    old = Path(sys.argv[1])
    new = Path(sys.argv[2])
    old_cues = parse_srt(old)
    new_cues = parse_srt(new)

    print("=== basic stats ===")
    print(f"old cues: {len(old_cues)}")
    print(f"new cues: {len(new_cues)}")
    print(f"cue delta: {len(new_cues) - len(old_cues)}")

    empty_old = sum(1 for cue in old_cues if not cue["text"])
    empty_new = sum(1 for cue in new_cues if not cue["text"])
    print(f"empty cues old/new: {empty_old} / {empty_new}")

    aligned = min(len(old_cues), len(new_cues))
    time_diff: list[int] = []
    text_diff: list[int] = []
    for i in range(aligned):
        old_cue = old_cues[i]
        new_cue = new_cues[i]
        if old_cue["start"] != new_cue["start"] or old_cue["end"] != new_cue["end"]:
            time_diff.append(i)
        if old_cue["text"] != new_cue["text"]:
            text_diff.append(i)

    print(f"time diffs in first {aligned}: {len(time_diff)}")
    print(f"text diffs in first {aligned}: {len(text_diff)}")

    for label, cues in [("old", old_cues), ("new", new_cues)]:
        if cues:
            print(f"{label} last end: {cues[-1]['end']}")

    bad_overlap = 0
    bad_order = 0
    for i in range(len(new_cues) - 1):
        end_sec = to_sec(new_cues[i]["end"])
        next_start = to_sec(new_cues[i + 1]["start"])
        if end_sec > next_start + 0.001:
            bad_overlap += 1
        if next_start < to_sec(new_cues[i]["start"]) - 0.001:
            bad_order += 1
    print(f"new overlap/adjacent issues: overlap={bad_overlap}, order={bad_order}")

    jp_re = re.compile(r"[ぁ-んァ-ヴー]")
    old_jp = sum(1 for cue in old_cues if jp_re.search(cue["text"]))
    new_jp = sum(1 for cue in new_cues if jp_re.search(cue["text"]))
    print(f"japanese remaining old/new: {old_jp} / {new_jp}")

    print("\n=== text diff samples (first 20) ===")
    for i in text_diff[:20]:
        old_cue = old_cues[i]
        new_cue = new_cues[i]
        print(f"--- cue {i + 1} [{old_cue['start']} --> {old_cue['end']}] ---")
        print("OLD:", old_cue["text"][:140].replace("\n", " / "))
        print("NEW:", new_cue["text"][:140].replace("\n", " / "))

    if len(old_cues) != len(new_cues):
        print("\n=== length mismatch tail ===")
        if len(new_cues) > len(old_cues):
            print("extra in new:")
            for cue in new_cues[len(old_cues) : len(old_cues) + 5]:
                print(cue["index"], cue["start"], cue["text"][:80])
        else:
            print("missing in new:")
            for cue in old_cues[len(new_cues) : len(new_cues) + 5]:
                print(cue["index"], cue["start"], cue["text"][:80])

    # categorize text changes
    added = removed = changed = 0
    for i in range(aligned):
        o = old_cues[i]["text"]
        n = new_cues[i]["text"]
        if o == n:
            continue
        if not o and n:
            added += 1
        elif o and not n:
            removed += 1
        else:
            changed += 1
    print("\n=== text change categories (aligned cues) ===")
    print(f"filled empty: {added}, cleared text: {removed}, rewritten: {changed}")


def analyze_quality(new: Path, old: Path | None = None) -> None:
    import re

    new_cues = parse_srt(new)
    jp = re.compile(r"[ぁ-んァ-ヴー]")
    cjk = re.compile(r"[\u4e00-\u9fff]")
    end_punct = set("。！？…!?")

    arrow_lines = [cue for cue in new_cues if "→" in cue["text"] or "->" in cue["text"]]
    print(f"\nannotation arrow lines in new: {len(arrow_lines)}")
    for cue in arrow_lines[:8]:
        print(cue["start"], cue["text"])

    bad_splits: list[tuple[int, str, str, str]] = []
    for i in range(len(new_cues) - 1):
        left = new_cues[i]["text"]
        right = new_cues[i + 1]["text"]
        if not left or not right:
            continue
        if left[-1] in end_punct:
            continue
        if right[0] in "，、的吧呢吗啊呀嘛" or len(left) <= 4:
            bad_splits.append((i + 1, left, right, new_cues[i]["start"]))
    print(f"possible mid-sentence splits in new: {len(bad_splits)}")
    for idx, left, right, start in bad_splits[:12]:
        print(f"--- {start} cue {idx} ---")
        print("A:", left)
        print("B:", right)

    if old is not None:
        old_cues = parse_srt(old)
        old_pure = [
            cue
            for cue in old_cues
            if jp.search(cue["text"]) and len(cjk.findall(cue["text"])) < 5
        ]
        print(f"old mostly-untranslated jp blocks: {len(old_pure)}")


if __name__ == "__main__":
    main()
    if len(sys.argv) >= 4 and sys.argv[3] == "--quality":
        analyze_quality(Path(sys.argv[2]), Path(sys.argv[1]))
