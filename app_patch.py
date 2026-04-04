

# ── Player Profile Route (v2 – full modal data) ─────────────────────────────
def _ai_scout_report(name, pos, is_pitcher, season, fg, sv, platoon, l7, gamelog):
    lines = []
    fname = name.split()[-1] if name else "This player"

    def _f(v, d=0.0):
        try:
            if v in (None, "", "N/A", "---"): return d
            return float(v)
        except: return d

    if not is_pitcher:
        avg  = _f(season.get("avg"),  _f(fg.get("fg_avg"),  0.250))
        xba  = _f(sv.get("sv_xba"),  avg)
        xwoba= _f(sv.get("sv_xwoba"),_f(fg.get("fg_woba"), 0.320))
        wrc  = _f(fg.get("fg_wrc"),  100)
        brl  = _f(sv.get("sv_brl_pct"), 5.5)
        ev   = _f(sv.get("sv_ev"),   87.5)
        ops  = _f(season.get("ops"), 0.720)
        l7avg= _f(l7.get("avg"),     0)

        if l7.get("isHot"):
            tag = f"hitting .{int(l7avg*1000):03d}" if l7avg > 0 else "running hot"
            lines.append(f"🔥 **HOT STREAK** — {fname} is locked in ({tag} over last 7 games). Back hits/TB aggressively.")
        elif l7.get("isCold"):
            lines.append(f"❄️ **Cold stretch** — {fname} batting .{int(l7avg*1000):03d} over last 7. Fade until signs of life return.")

        if xba > avg + 0.020:
            lines.append(f"📈 **Positive regression candidate** — xBA ({xba:.3f}) exceeds BA ({avg:.3f}) by {(xba-avg):.3f}. Expect hit-rate uptick.")
        elif avg > xba + 0.020:
            lines.append(f"📉 **Regression risk** — BA ({avg:.3f}) outpaces xBA ({xba:.3f}). Fade the hits prop.")

        if wrc >= 140:
            lines.append(f"⭐ **Elite wRC+ ({int(wrc)})** — Top-tier offensive output. Prioritize in all prop markets.")
        elif wrc >= 115:
            lines.append(f"✅ **Above-avg wRC+ ({int(wrc)})** — Consistent above-average bat worth targeting on hits and TB.")
        elif 0 < wrc < 80:
            lines.append(f"⚠️ **Weak wRC+ ({int(wrc)})** — Below-average production. Lower confidence on hits props.")

        if brl >= 12:
            lines.append(f"💥 **Power threat** — Barrel% of {brl:.1f}% is elite. Target HR and 2+ TB props.")
        elif brl >= 8:
            lines.append(f"💪 **Solid barrel rate ({brl:.1f}%)** — Above-avg power, TB props have value.")

        if ev >= 92:
            lines.append(f"🚀 **Hard-contact machine** — Avg EV of {ev:.1f} mph. Consistent quality contact.")

        vl = platoon.get("vl", {}); vr = platoon.get("vr", {})
        ops_vl = _f(vl.get("ops"), 0); ops_vr = _f(vr.get("ops"), 0)
        if ops_vl > 0 and ops_vr > 0:
            if ops_vl - ops_vr >= 0.060:
                lines.append(f"🔄 **Platoon edge vs LHP** — OPS {ops_vl:.3f} vs LHP vs {ops_vr:.3f} vs RHP. Target when facing a lefty.")
            elif ops_vr - ops_vl >= 0.060:
                lines.append(f"🔄 **Platoon edge vs RHP** — OPS {ops_vr:.3f} vs RHP vs {ops_vl:.3f} vs LHP. Strong vs righties.")

        multi_hit = sum(1 for g in gamelog[-10:] if int(g.get("h", 0)) >= 2)
        if multi_hit >= 5:
            lines.append(f"📊 **Multi-hit machine** — {multi_hit} multi-hit games in last 10. 2+ hits prop has strong historical backing.")

        if not lines:
            lines.append(f"📋 **{fname}** — mid-tier hitter ({avg:.3f} BA, {ops:.3f} OPS). Play hits prop at favorable lines (under -140).")

        if xwoba >= 0.370:
            angle = "Back hits and TB props — strong expected value"
        elif xwoba >= 0.320:
            angle = "Play selectively at favorable prices"
        else:
            angle = "Avoid unless line is heavily discounted"
        lines.append(f"\n**🎯 Betting Angle:** {angle}")

    else:
        era  = _f(season.get("era"),  _f(fg.get("fg_era"),  4.50))
        xera = _f(sv.get("sv_xera"), era)
        fip  = _f(fg.get("fg_fip"),  era)
        k9   = _f(fg.get("fg_k9"),   _f(season.get("strikeoutsPer9Inn"), 8.2))
        war  = _f(fg.get("fg_war"),  0)

        if l7.get("isHot"):
            lines.append(f"🔥 **Dominant stretch** — {fname} allowed only {l7.get('er', '?')} ER in recent starts. K props have value.")
        elif l7.get("isCold"):
            lines.append(f"❄️ **Struggling** — {fname} gave up {l7.get('er', '?')} ER recently. Avoid low-ER props.")

        if era > xera + 0.40:
            lines.append(f"📈 **Positive regression** — ERA ({era:.2f}) exceeds xERA ({xera:.2f}). Performance should improve.")
        elif xera > era + 0.40:
            lines.append(f"📉 **Regression risk** — xERA ({xera:.2f}) far exceeds ERA ({era:.2f}). Decline incoming.")

        if k9 >= 10.5:
            lines.append(f"⚡ **Elite K rate** ({k9:.1f} K/9) — 5+ and 6+ K props are strong plays.")
        elif k9 >= 9.0:
            lines.append(f"✅ **Above-avg K rate** ({k9:.1f} K/9) — 5+ K prop is the primary target.")
        elif 0 < k9 < 7.5:
            lines.append(f"⚠️ **Low K rate** ({k9:.1f} K/9) — Avoid strikeout props.")

        if war >= 2.0:
            lines.append(f"⭐ **Ace-level WAR ({war:.1f})** — Elite starter. High confidence in K props.")
        elif 0 < war < 0.5:
            lines.append(f"⚠️ **Replacement-level WAR ({war:.1f})** — Below-average production.")

        vl = platoon.get("vl", {}); vr = platoon.get("vr", {})
        era_vl = _f(vl.get("era"), 0); era_vr = _f(vr.get("era"), 0)
        if era_vl > 0 and era_vr > 0 and era_vr - era_vl >= 0.80:
            lines.append(f"🔄 **Vulnerable vs RHH** — ERA {era_vr:.2f} vs RHH vs {era_vl:.2f} vs LHH. Watch opposing lineup hand.")

        if not lines:
            lines.append(f"📋 **{fname}** — {era:.2f} ERA / {fip:.2f} FIP. Standard K prop evaluation needed.")

        if k9 >= 9.5:
            angle = "Target 5+ and 6+ K props"
        elif k9 >= 8.0:
            angle = "Target 4+ K only at favorable prices"
        else:
            angle = "Avoid K props — low strikeout upside"
        lines.append(f"\n**🎯 Betting Angle:** {angle}")

    return "\n\n".join(lines)


@app.route("/api/player/<int:player_id>")
def api_player_profile(player_id):
    try:
        year = datetime.now().year

        bio_r = requests.get(f"{MLB_API}/people/{player_id}", timeout=8)
        bio_r.raise_for_status()
        pdata = (bio_r.json().get("people") or [{}])[0]
        name  = pdata.get("fullName", "")
        pos   = pdata.get("primaryPosition", {}).get("abbreviation", "?")
        bats  = pdata.get("batSide", {}).get("code", "S")
        throws= pdata.get("pitchHand", {}).get("code", "R")
        team  = pdata.get("currentTeam", {}).get("name", "")
        headshot = (
            f"https://img.mlbstatic.com/mlb-photos/image/upload/"
            f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
            f"v1/people/{player_id}/headshot/67/current"
        )

        is_pitcher = pos in ("P", "SP", "RP", "CL")
        group = "pitching" if is_pitcher else "hitting"

        # Season stats
        sr = requests.get(f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "season", "group": group, "season": year}, timeout=8)
        sr.raise_for_status()
        season_sp = (sr.json().get("stats") or [{}])[0].get("splits", [])
        season = season_sp[0].get("stat", {}) if season_sp else {}

        # Game log
        lr = requests.get(f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": group, "season": year}, timeout=8)
        lr.raise_for_status()
        all_games = (lr.json().get("stats") or [{}])[0].get("splits", [])
        last15 = all_games[-15:]

        gamelog = []
        if not is_pitcher:
            for sp in last15:
                s = sp.get("stat", {})
                gamelog.append({
                    "date": sp.get("date", ""), "opp": sp.get("opponent", {}).get("abbreviation", ""),
                    "ab": s.get("atBats", 0), "h": s.get("hits", 0), "hr": s.get("homeRuns", 0),
                    "rbi": s.get("rbi", 0), "k": s.get("strikeOuts", 0),
                    "bb": s.get("baseOnBalls", 0), "sb": s.get("stolenBases", 0), "avg": s.get("avg", "---"),
                })
        else:
            for sp in last15:
                s = sp.get("stat", {})
                gamelog.append({
                    "date": sp.get("date", ""), "opp": sp.get("opponent", {}).get("abbreviation", ""),
                    "ip": s.get("inningsPitched", "0"), "er": s.get("earnedRuns", 0),
                    "k": s.get("strikeOuts", 0), "bb": s.get("baseOnBalls", 0),
                    "h": s.get("hits", 0), "hr": s.get("homeRuns", 0),
                })

        # Platoon splits
        pr = requests.get(f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "statSplits", "group": group, "season": year, "sitCodes": "vl,vr"}, timeout=8)
        pr.raise_for_status()
        platoon = {}
        for sp in (pr.json().get("stats") or [{}])[0].get("splits", []):
            code = sp.get("split", {}).get("code", "")
            if code in ("vl", "vr"):
                s = sp.get("stat", {})
                if not is_pitcher:
                    platoon[code] = {
                        "avg": s.get("avg","---"), "obp": s.get("obp","---"),
                        "slg": s.get("slg","---"), "ops": s.get("ops","---"),
                        "pa": s.get("plateAppearances", 0), "hr": s.get("homeRuns", 0),
                        "k": s.get("strikeOuts", 0), "bb": s.get("baseOnBalls", 0),
                    }
                else:
                    platoon[code] = {
                        "era": s.get("era","---"), "whip": s.get("whip","---"),
                        "k": s.get("strikeOuts", 0), "bb": s.get("baseOnBalls", 0),
                        "pa": s.get("battersFaced", 0),
                    }

        # FanGraphs + Savant
        if not is_pitcher:
            fg = fg_batter(name)
            sv = sv_batter(name)
            arsenal = {}
        else:
            fg = fg_pitcher(name)
            sv = sv_pitcher(name)
            arsenal = {
                "pct": sv.get("sv_arsenal_pct", {}),
                "velo": sv.get("sv_arsenal_velo", {}),
            }

        # L7 summary
        last7g = all_games[-7:]
        if not is_pitcher and last7g:
            l7_ab  = sum(int(sp.get("stat",{}).get("atBats",0) or 0) for sp in last7g)
            l7_h   = sum(int(sp.get("stat",{}).get("hits",0) or 0) for sp in last7g)
            l7_hr  = sum(int(sp.get("stat",{}).get("homeRuns",0) or 0) for sp in last7g)
            l7_rbi = sum(int(sp.get("stat",{}).get("rbi",0) or 0) for sp in last7g)
            l7_k   = sum(int(sp.get("stat",{}).get("strikeOuts",0) or 0) for sp in last7g)
            l7_bb  = sum(int(sp.get("stat",{}).get("baseOnBalls",0) or 0) for sp in last7g)
            l7_avg = round(l7_h / l7_ab, 3) if l7_ab > 0 else 0
            l7 = {"avg": l7_avg, "hr": l7_hr, "rbi": l7_rbi, "k": l7_k, "bb": l7_bb,
                  "isHot": l7_avg >= 0.310 or l7_hr >= 2,
                  "isCold": l7_avg < 0.150 and l7_ab >= 12}
        elif is_pitcher and last7g:
            l7_er = sum(int(sp.get("stat",{}).get("earnedRuns",0) or 0) for sp in last7g)
            l7_k  = sum(int(sp.get("stat",{}).get("strikeOuts",0) or 0) for sp in last7g)
            l7 = {"er": l7_er, "k": l7_k,
                  "isHot": l7_er <= 4 and len(last7g) >= 2,
                  "isCold": l7_er >= 9 and len(last7g) >= 2}
        else:
            l7 = {}

        scout = _ai_scout_report(name, pos, is_pitcher, season, fg, sv, platoon, l7, gamelog)

        return jsonify({
            "success": True,
            "player": {"id": player_id, "name": name, "pos": pos, "bats": bats,
                       "throws": throws, "team": team, "headshot": headshot},
            "season": season, "fg": fg,
            "savant": {k: v for k, v in sv.items() if k not in ("sv_arsenal_pct","sv_arsenal_velo")},
            "gamelog": gamelog, "platoon": platoon, "l7": l7,
            "arsenal": arsenal, "isPitcher": is_pitcher, "scout": scout,
        })
    except Exception as ex:
        print("[api_player_profile]", traceback.format_exc())
        return jsonify({"success": False, "error": str(ex)}), 500
