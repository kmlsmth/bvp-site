"""
Constants measured from real 2026 data (every regular-season plate
appearance, 183,849 PAs / 2,429 games, from Baseball Savant; totals match
MLB's official 2026 team stats). See scripts/backtest.py and the project
doc "backtest-results".

STARTER_PA_GAMES: how many plate appearances a STARTING hitter actually got,
by lineup spot and home/away -- {side: {spot: {PAs: games}}}. Starters get
fewer PAs than "team PAs / 9" suggests (pinch hitters, defensive subs,
blowouts, and the home team often not batting in the 9th), and using the
real spread rather than the average matters for "at least one" odds.

PLATOON_FACTOR: league rate when the hitter bats from the SAME side as the
pitcher throws ("same", e.g. a righty vs a righty) or the OPPOSITE side
("opp"), divided by the overall league rate. Platoon effects are stable from
year to year; refresh these from the archive each season.
"""

STARTER_PA_GAMES = {
    "away": {
        1: {1: 5, 2: 17, 3: 126, 4: 890, 5: 1221, 6: 160, 7: 10},
        2: {1: 8, 2: 22, 3: 111, 4: 1053, 5: 1125, 6: 103, 7: 7},
        3: {1: 5, 2: 27, 3: 127, 4: 1222, 5: 965, 6: 79, 7: 4},
        4: {1: 6, 2: 34, 3: 156, 4: 1373, 5: 801, 6: 58, 7: 1},
        5: {1: 6, 2: 57, 3: 245, 4: 1449, 5: 629, 6: 42, 7: 1},
        6: {1: 7, 2: 128, 3: 344, 4: 1425, 5: 496, 6: 29},
        7: {1: 6, 2: 158, 3: 464, 4: 1395, 5: 384, 6: 22},
        8: {1: 3, 2: 250, 3: 623, 4: 1247, 5: 293, 6: 13},
        9: {1: 9, 2: 287, 3: 759, 4: 1150, 5: 215, 6: 9},
    },
    "home": {
        1: {1: 9, 2: 22, 3: 125, 4: 1160, 5: 1048, 6: 62, 7: 3},
        2: {1: 8, 2: 25, 3: 117, 4: 1365, 5: 865, 6: 47, 7: 2},
        3: {1: 6, 2: 33, 3: 149, 4: 1516, 5: 684, 6: 40, 7: 1},
        4: {1: 3, 2: 45, 3: 223, 4: 1594, 5: 530, 6: 32, 7: 2},
        5: {1: 8, 2: 73, 3: 323, 4: 1607, 5: 395, 6: 22, 7: 1},
        6: {1: 9, 2: 130, 3: 448, 4: 1538, 5: 286, 6: 18},
        7: {1: 9, 2: 184, 3: 633, 4: 1394, 5: 191, 6: 18},
        8: {1: 9, 2: 238, 3: 829, 4: 1212, 5: 129, 6: 12},
        9: {1: 14, 2: 318, 3: 990, 4: 1023, 5: 78, 6: 6},
    },
}

PLATOON_FACTOR = {
    "same": {"ob": 0.9679, "so": 1.0270, "hr": 0.9360, "avg": 0.9823, "slg": 0.9586},
    "opp": {"ob": 1.0230, "so": 0.9806, "hr": 1.0460, "avg": 1.0129, "slg": 1.0301},
}
