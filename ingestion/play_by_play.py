"""Turn a cached Euroleague/EuroCup play-by-play file into events and stints.

A *stint* is a stretch of game time in which one team has the same five
players on the floor. Stints are what on-floor questions need ("X's
plus-minus with Y on the floor"): the box score only says who played, not
who played together.

The source has no lineups, only substitutions (IN/OUT events). So we start
each game from the box-score starters and apply every substitution in order.
Everything here is pure (no database, no network) so it can be tested on
small hand-written games; ingestion/euroleague.py does the saving.
"""

QUARTER_KEYS = ["FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter"]
OVERTIME_KEY = "ExtraTime"
QUARTER_SECONDS = 600
OVERTIME_SECONDS = 300
SUBSTITUTIONS = {"IN", "OUT"}
# Markers with no clock or player: begin/end of a period and end of game.
MARKERS = {"BP", "EP", "EG"}


def _text(value):
    """The source pads codes with spaces ('PAN       ') and uses '' for none."""
    value = (value or "").strip()
    return value or None


def period_start(period):
    """Seconds of game time before `period` starts (1-4 quarters, 5+ overtimes)."""
    if period <= 4:
        return (period - 1) * QUARTER_SECONDS
    return 4 * QUARTER_SECONDS + (period - 5) * OVERTIME_SECONDS


def period_length(period):
    return QUARTER_SECONDS if period <= 4 else OVERTIME_SECONDS


def _clock_seconds(text):
    minutes, seconds = text.split(":")
    return int(minutes) * 60 + int(seconds)


def parse_events(data, player_code=_text):
    """Return the game's events in order, as plain dicts.

    elapsed: seconds since tip-off, from the period and the clock
      (MARKERTIME counts down). Markers have no clock: a begin marker gets
      the period start, an end marker the period end.
    score_a/score_b: the running score, only on events that changed it.
      A is the source's TeamA, which is the home team.
    player_code: turns the source's player ID into ours (it fixes the few
      players the source lists under two IDs).
    """
    blocks = [(number, data.get(key) or []) for number, key in enumerate(QUARTER_KEYS, start=1)]
    # All overtimes share one list. Each one starts right after the previous
    # one's 'EP' (end period): substitutions for the next overtime are listed
    # before its 'BP' (begin period) marker, with the clock at 05:00.
    overtime, period, ended = data.get(OVERTIME_KEY) or [], 5, False
    events = []
    for number, rows in blocks + [(None, overtime)]:
        for row in rows:
            play_type = row["PLAYTYPE"].strip()
            if number is None:
                if ended and play_type != "EG":
                    period, ended = period + 1, False
                ended = ended or play_type == "EP"
                current = period
            else:
                current = number
            clock = _text(row.get("MARKERTIME"))
            if clock is not None:
                elapsed = period_start(current) + period_length(current) - _clock_seconds(clock)
            elif play_type == "BP":
                elapsed = period_start(current)
            else:
                elapsed = period_start(current) + period_length(current)
            player = _text(row.get("PLAYER_ID"))
            events.append({
                "sequence": row["NUMBEROFPLAY"],
                "period": current,
                "elapsed": elapsed,
                "team_code": _text(row.get("CODETEAM")),
                "player_code": player_code(player) if player else None,
                "play_type": play_type,
                "score_a": row.get("POINTS_A"),
                "score_b": row.get("POINTS_B"),
                "info": _text(row.get("PLAYINFO")),
            })
    return events


def build_stints(events, starters, code_a, code_b):
    """Split the game into stints for both teams.

    starters: {team_code: set of the five player codes who started}.
    Returns (stints, problems). Each stint is a dict with team_code, start,
    end (elapsed seconds), players (sorted tuple of five codes), points_for
    and points_against. `problems` lists anything that made a lineup not
    have exactly five players, so bad games can be reported, not hidden.

    Substitutions that happen together (a batch of IN/OUT events with no
    other event between them) are applied as one change, so a lineup can be
    six or four players for a moment without that counting as a stint.
    Points are credited to the lineups on the floor when they were scored.
    """
    lineups = {code: set(players) for code, players in starters.items()}
    other = {code_a: code_b, code_b: code_a}
    problems = []
    for code in (code_a, code_b):
        if len(lineups.get(code, ())) != 5:
            problems.append(f"{code}: {len(lineups.get(code, ()))} starters")
            lineups.setdefault(code, set())

    stints = []
    open_stints = {code: _new_stint(code, lineups[code], 0) for code in (code_a, code_b)}
    score = {code_a: 0, code_b: 0}
    pending = None  # clock time of substitutions not yet turned into stints

    def close_changed(elapsed):
        for code in (code_a, code_b):
            players = tuple(sorted(lineups[code]))
            if players != open_stints[code]["players"]:
                # The clock sometimes steps back a second, or an event is
                # listed late; a stint can't end before it started.
                elapsed = max(elapsed, open_stints[code]["start"])
                if len(players) != 5:
                    problems.append(f"{code}: {len(players)} on the floor at {elapsed}s")
                _finish(stints, open_stints[code], elapsed)
                open_stints[code] = _new_stint(code, lineups[code], elapsed)

    for event in events:
        code, play_type = event["team_code"], event["play_type"]
        if play_type in SUBSTITUTIONS:
            if code not in lineups or not event["player_code"]:
                problems.append(f"substitution without team or player at {event['elapsed']}s")
                continue
            if play_type == "IN":
                lineups[code].add(event["player_code"])
            else:
                lineups[code].discard(event["player_code"])
            pending = event["elapsed"]
            continue
        if pending is not None:
            close_changed(pending)
            pending = None
        if event["score_a"] is not None or event["score_b"] is not None:
            # Early in a game the source may fill in only the side that
            # scored (A None, B 2); the missing side hasn't changed.
            new = {code_a: score[code_a] if event["score_a"] is None else event["score_a"],
                   code_b: score[code_b] if event["score_b"] is None else event["score_b"]}
            for team in (code_a, code_b):
                gained = new[team] - score[team]
                open_stints[team]["points_for"] += gained
                open_stints[other[team]]["points_against"] += gained
            score = new

    end = max((e["elapsed"] for e in events), default=0)
    if pending is not None:
        close_changed(pending)
    for code in (code_a, code_b):
        _finish(stints, open_stints[code], max(end, open_stints[code]["start"]))
    return stints, problems


def _new_stint(code, players, start):
    return {"team_code": code, "start": start, "end": start,
            "players": tuple(sorted(players)), "points_for": 0, "points_against": 0}


def _finish(stints, stint, end):
    stint["end"] = end
    # A zero-length stint matters only if points were scored in it.
    if stint["end"] > stint["start"] or stint["points_for"] or stint["points_against"]:
        stints.append(stint)


def player_totals(stints):
    """{(team_code, player_code): [seconds, plus_minus]} summed over stints."""
    totals = {}
    for stint in stints:
        for player in stint["players"]:
            total = totals.setdefault((stint["team_code"], player), [0, 0])
            total[0] += stint["end"] - stint["start"]
            total[1] += stint["points_for"] - stint["points_against"]
    return totals


def compare_with_box(stints, box):
    """How far the stints are from the box score.

    box: {(team_code, player_code): (seconds_played or None, plus_minus or None)}.
    Returns (minutes_max_diff, plus_minus_mismatches): the largest gap in
    seconds between stint time and box-score minutes, and how many players'
    stint plus-minus differs from the box score's.
    """
    totals = player_totals(stints)
    max_diff, mismatches = 0, 0
    for key in box.keys() | totals.keys():
        seconds, plus_minus = box.get(key, (0, 0))
        on_floor, stint_plus_minus = totals.get(key, (0, 0))
        if seconds is not None:
            max_diff = max(max_diff, abs(on_floor - seconds))
        if plus_minus is not None and stint_plus_minus != plus_minus:
            mismatches += 1
    return max_diff, mismatches
