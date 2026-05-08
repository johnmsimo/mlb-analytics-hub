# This file documents the two fixes applied directly in app.py via the patch commit.
# Bug 1: api_player_performance_badges called `svbatter(player_name)` which is
#         undefined in app.py scope -> NameError -> endpoint always returned {badges:[], metrics:{}}.
#         Fix: replace with `_brain_sv_batter(player_name) or {}`
#
# Bug 2: api_pitcher_performance_badges used _safe_num() on inningsPitched which
#         returns MLB API format strings like '45.2' (meaning 45 and 2/3 innings),
#         treating '.2' as a decimal fraction instead of outs.
#         Fix: added _parse_ip() helper that converts '45.2' -> 45.667 correctly.
#
# Both fixes are live in app.py as of this commit.
