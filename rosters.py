'''
Roster production for the Kalman filter's preseason ratings (see ROSTER_COEFS in kalman.py).

Player season stats come from Bart Torvik (barttorvik.com, one CSV per season). For each team, the new season's
preseason rating is predicted from last season's rating and conference, plus:
- returning share: the share of last season's minutes played by players who are back
- returning value: whether those returners were better or worse than the team as a whole
- transfer share and transfer value: the same for players who transferred in, measured against their NEW team
- recruits: incoming freshmen, graded by recruiting rating (Torvik's 0-100 score, about the top 25 recruits are 95+)

A player's level is his old team's rating, adjusted by how his box plus-minus compared to his teammates'. So a
10-point scorer on a top-10 team who moves to a bottom-100 team is a big upgrade there, while the same player
staying put changes little.

Usage:
  python rosters.py --download 2009 2026   re-download player data for these seasons (start years)
  python rosters.py --fit                  refit ROSTER_COEFS on the 2013-14 through 2020-21 seasons
'''
import argparse
import csv
import io
import os
import time
import numpy as np
import requests

PLAYERS_FOLDER = 'Data/players/'
MAPPING_FILE = 'Data/torvik_mapping.csv'
TORVIK_URL = 'https://barttorvik.com/getadvstats.php?year={year}&csv=1'
# columns of Torvik's player CSV (it has no header row); its year is the season's end year
COL_TEAM, COL_MIN_PER, COL_CLASS, COL_PID, COL_RECRUIT, COL_BPM = 1, 4, 25, 32, 34, 50
FIELDS = ['pid', 'team', 'class', 'min_per', 'bpm', 'recruit']
# box plus-minus is points per 100 possessions vs. an average player. A team's margin per game is about
# 5 players x 0.68 (possessions per game / 100) x its minutes-weighted BPM, so 3.4 turns BPM into our points scale
BPM_TO_POINTS = 3.4
RECRUIT_FLOOR = 70        # recruiting ratings below this count as 0
FEATURES = ['returning_share', 'returning_value', 'transfer_share', 'transfer_value', 'recruits']

def season_path(season):
	return f'{PLAYERS_FOLDER}{season}.csv'

def load_mapping():
	with open(MAPPING_FILE, newline = '', encoding = 'utf-8') as f:
		return {r['torvik_name']: r['team'] for r in csv.DictReader(f)}

def convert(text, season):
	'''
	keeps the fields used here from a Torvik player CSV, with team names mapped to ours, and saves them
	'''
	mapping = load_mapping()
	rows, unmapped = [], set()
	for r in csv.reader(io.StringIO(text)):
		if len(r) <= COL_BPM: continue
		team = mapping.get(r[COL_TEAM])
		if team is None:
			unmapped.add(r[COL_TEAM])
			continue
		rows.append([r[COL_PID], team, r[COL_CLASS], r[COL_MIN_PER], r[COL_BPM], r[COL_RECRUIT]])
	if unmapped:
		print(f'Warning... {len(unmapped)} Torvik team name(s) not in {MAPPING_FILE}, skipped: ' + ', '.join(sorted(unmapped)))
	out = io.StringIO()
	w = csv.writer(out, lineterminator = '\n')
	w.writerow(FIELDS)
	w.writerows(rows)
	path = season_path(season)
	if os.path.exists(path):
		with open(path, newline = '', encoding = 'utf-8') as f:
			if f.read() == out.getvalue(): return len(rows) #unchanged: keep the file (and any cached ratings) as is
	os.makedirs(PLAYERS_FOLDER, exist_ok = True)
	with open(path, 'w', newline = '', encoding = 'utf-8') as f:
		f.write(out.getvalue())
	_cache.pop(season, None)
	return len(rows)

def download(season):
	'''
	fetches one season (start year, e.g. 2025 for 2025-26) of player stats from Torvik into Data/players
	'''
	response = requests.get(TORVIK_URL.format(year = season + 1), headers = {'User-Agent': 'Mozilla/5.0'}, timeout = 60)
	response.raise_for_status()
	response.encoding = 'utf-8'
	if response.text.count('\n') < 100: #before a season starts the file is empty; keep what we have
		raise ValueError(f'only {response.text.count(chr(10))} player rows for {season}')
	return convert(response.text, season)

def refresh_current_season(data, max_age_hours = 6):
	'''
	re-downloads the season in progress (rosters fill in as players appear in games), unless it was refreshed in
	the last max_age_hours. Never fails the run
	'''
	last = data[-1][5]
	season = int(last[:4]) if int(last[4:6]) >= 7 else int(last[:4]) - 1
	path = season_path(season)
	if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_hours * 3600: return
	try:
		n = download(season)
		print(f'player data for {season}-{str(season + 1)[2:]} refreshed ({n} players)')
	except Exception as e:
		print(f'Warning... could not refresh player data for {season}: {e}. Using the saved copy')

_cache = {}
def load(season):
	'''
	list of (pid, team, class, minutes %, BPM, recruiting rating or None) for a season, or None if not saved
	'''
	if season not in _cache:
		rows = None
		if os.path.exists(season_path(season)):
			rows = []
			with open(season_path(season), newline = '', encoding = 'utf-8') as f:
				for r in csv.DictReader(f):
					try:
						recruit = float(r['recruit']) if r['recruit'] not in ('', 'NA') else None
						rows.append((r['pid'], r['team'], r['class'], float(r['min_per']), float(r['bpm']), recruit))
					except ValueError:
						continue
		_cache[season] = rows
	return _cache[season]

def data_signature():
	'''
	identifies the saved player data, so cached rating runs are redone when it changes
	'''
	if not os.path.isdir(PLAYERS_FOLDER): return ()
	return tuple((f, os.path.getsize(PLAYERS_FOLDER + f), os.path.getmtime(PLAYERS_FOLDER + f)) for f in sorted(os.listdir(PLAYERS_FOLDER)))

def features(season, last_ratings):
	'''
	roster features (FEATURES order) for each team with player data in both `season` and the season before
	last_ratings: {team: rating in points vs. an average D1 team at the end of last season}
	'''
	prev, cur = load(season - 1), load(season)
	if not prev or not cur: return {}
	by_team = {}
	for pid, team, cls, min_per, bpm, recruit in prev:
		by_team.setdefault(team, []).append((pid, min_per, bpm))
	share, level, old_team = {}, {}, {}
	for team, players in by_team.items():
		total = sum(m for _, m, _ in players)
		if team not in last_ratings or total <= 0: continue
		team_bpm = sum(m * b for _, m, b in players) / total
		for pid, m, b in players:
			share[pid] = m / total
			level[pid] = last_ratings[team] + BPM_TO_POINTS * (b - team_bpm)
			old_team[pid] = team
	out = {}
	for pid, team, cls, min_per, bpm, recruit in cur:
		if team not in last_ratings: continue
		f = out.setdefault(team, [0.0] * len(FEATURES))
		if pid in level:
			value = share[pid] * (level[pid] - last_ratings[team])
			if old_team[pid] == team:
				f[0] += share[pid]; f[1] += value
			else:
				f[2] += share[pid]; f[3] += value
		elif cls == 'Fr' and recruit is not None:
			f[4] += max(0.0, recruit - RECRUIT_FLOOR) / (100 - RECRUIT_FLOOR)
	return out

def conference_averages(ratings, conferences):
	groups = {}
	for team, points in ratings.items():
		if conferences[team] != 'Other': groups.setdefault(conferences[team], []).append(points)
	return {conf: float(np.mean(v)) for conf, v in groups.items()}

def fit(first = 2013, last = 2020):
	'''
	least-squares fit of the preseason model on seasons first..last (start years): the target is each team's
	end-of-season rating from the Kalman filter run without roster adjustments
	'''
	import utils, kalman
	data = utils.read_csv(utils.get_latest_data_filepath())
	ends = {}
	orig_reset, orig_coefs = kalman.KalmanSim.season_reset, kalman.ROSTER_COEFS
	def record(sim, date):
		ends[int(date[:4]) - 1] = {t.name: (t.points, t.conference, t.season_game_count) for t in sim.teams.values()}
		return orig_reset(sim, date)
	kalman.KalmanSim.season_reset, kalman.ROSTER_COEFS = record, None
	try:
		kalman.sim(data)
	finally:
		kalman.KalmanSim.season_reset, kalman.ROSTER_COEFS = orig_reset, orig_coefs
	X, y = [], []
	for season in range(first, last + 1):
		if season - 1 not in ends or season not in ends: continue
		before = {t: v for t, v in ends[season - 1].items() if v[2] >= kalman.GAMES_REQUIRED}
		ratings = {t: v[0] for t, v in before.items()}
		conf_avg = conference_averages(ratings, {t: v[1] for t, v in before.items()})
		for team, f in features(season, ratings).items():
			points, conf, _ = before[team]
			after = ends[season].get(team)
			if conf == 'Other' or after is None or after[2] < kalman.GAMES_REQUIRED: continue
			X.append([1.0, points, conf_avg[conf]] + f)
			y.append(after[0])
	coef, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond = None)
	resid = np.array(y) - np.array(X) @ coef
	names = ['intercept', 'last_rating', 'conference_avg'] + FEATURES
	print(f'fit on {len(y)} team-seasons, {first}-{last}; residual sd {resid.std():.2f} pts')
	print('ROSTER_COEFS = {' + ', '.join(f"'{n}': {c:.3f}" for n, c in zip(names, coef)) + '}')
	return dict(zip(names, coef))

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description = 'Download player data or refit the preseason roster model')
	parser.add_argument('--download', nargs = 2, type = int, metavar = ('FIRST', 'LAST'), help = 'download seasons FIRST..LAST (start years)')
	parser.add_argument('--fit', action = 'store_true', help = 'refit ROSTER_COEFS on 2013-14 through 2020-21')
	args = parser.parse_args()
	if args.download:
		for s in range(args.download[0], args.download[1] + 1):
			print(s, download(s), 'players')
	if args.fit:
		fit()
