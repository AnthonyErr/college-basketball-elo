'''
Kalman filter team ratings (the default rating engine, see RATING_MODEL in elo.py).

Each team has a latent strength in points relative to an average team. The filter keeps a mean for every
team plus a full covariance matrix across all teams, so a result also updates the teams each side has played
(e.g. a non-conference upset tells us something about both conferences). Home-court advantage is its own
state and is learned from the data instead of fixed.

Each day:
1. Uncertainty grows a little (teams change during a season: injuries, development, lineup changes).
2. All of the day's games are observed at once: the final margin (noise variance GAME_VAR) and, when there is
   one, the closing Vegas spread (noise variance MARKET_VAR). The market knows about roster turnover and the
   transfer portal, so early in the season, when the filter is uncertain, lines move ratings a lot; later,
   once the filter is confident, they move ratings much less.
Each offseason, ratings regress toward their conference average and uncertainty is added for roster turnover.

Ratings are reported on the Elo scale (ELO_BASE + points * 24.78) so rankings, spreads, and outputs read the
same as the Elo model. Tuned on the 2013-14 through 2020-21 seasons, validated on 2021-22 onward (backtest.py).
'''
import math
import datetime
import numpy as np
from scipy.linalg import solve as la_solve
import utils
import elo
import rosters

GAME_VAR = 110.0          # variance (pts^2) of a final margin around the true strength difference
MARKET_VAR = 40.0         # variance (pts^2) of the closing spread around the true strength difference. None = ignore lines
DAILY_DRIFT_VAR = 0.1     # variance (pts^2) added to every team's strength per day during the season
SEASON_CARRY = 0.75       # share of a team's strength (vs. its conference average) carried into the next season
PRESEASON_SD = 4.0        # uncertainty (pts) added to every team each offseason for roster turnover
NEW_TEAM_MEAN = -35.0     # prior strength (pts vs. the average D1 team) for a team new to the system (mostly non-D1)
NEW_TEAM_SD = 12.0        # prior uncertainty (pts) for a team new to the system
FIRST_SEASON_SD = 10.0    # prior uncertainty (pts) for every team in the first season of data
HOME_ADV = 3.3            # starting home-court advantage (pts); learned from the data after that
HOME_ADV_SD = 0.5         # starting uncertainty of home-court advantage, also added each offseason
OUTCOME_VAR = 100.0       # game-to-game noise (pts^2) used to turn a predicted margin into a win probability
GAMES_REQUIRED = elo.GAMES_REQUIRED  # teams with fewer games in a season are dropped at the season reset
PRUNE_DAYS = 30           # non-D1 teams idle this many days are removed (exact marginalization, keeps it fast)
MAX_LINE = 40             # ignore spreads this large or larger (data errors)
# Preseason roster model (rosters.py). For D1 teams with player data, the preseason rating is predicted from last
# season's rating, the conference average, returning and incoming-transfer production, and recruits, instead of
# SEASON_CARRY alone. Fit on 2013-14 through 2020-21 with `python rosters.py --fit`. None turns it off.
ROSTER_COEFS = {'intercept': -4.547, 'last_rating': 0.707, 'conference_avg': 0.249, 'returning_share': 7.834,
	'returning_value': 0.693, 'transfer_share': 7.364, 'transfer_value': 0.617, 'recruits': 1.084}
ROSTER_PRESEASON_SD = 3.5 # offseason uncertainty (pts) for teams with a roster projection; PRESEASON_SD for the rest
POINTS_TO_ELO = -elo.ELO_TO_POINTS_FACTOR
CONFERENCE_DICT = elo.CONFERENCE_DICT

def _ordinal(date_string):
	return datetime.datetime.strptime(date_string, '%Y%m%d').toordinal()

def _parse_line(value):
	try:
		line = float(value)
	except (TypeError, ValueError):
		return None
	return line if math.isfinite(line) and abs(line) < MAX_LINE else None

class KalmanTeam():
	'''
	view of one team's rating inside a KalmanSim; mirrors the attributes of elo.Team that the rest of the code uses
	'''
	def __init__(self, sim, name, slot):
		self.sim = sim
		self.name = name
		self.slot = slot
		self.conference = CONFERENCE_DICT.get(name, 'Other')
		self.season_game_count = 0
		self.total_games = 0
		self.last_played = sim.day
		self.snapshots = [(sim.date, self.elo)]

	@property
	def points(self):
		'''strength in points relative to the average D1 team'''
		return self.sim.mu[self.slot] - self.sim.center

	@property
	def sd(self):
		return math.sqrt(self.sim.P[self.slot, self.slot])

	@property
	def elo(self):
		return elo.ELO_BASE + self.points * POINTS_TO_ELO

class KalmanSim():
	'''
	Same interface as elo.ELO_Sim (teams, date, get_elo, snapshot, get_top, get_rankings_dict) plus predict()
	'''
	def __init__(self):
		self.teams = {}
		self.season_count = 0
		self.date = ''
		self.day = None
		self.last_prune = None
		self.n = 1                     #slot 0 holds home-court advantage
		self.mu = np.zeros(1024)
		self.P = np.zeros((1024, 1024))
		self.slots = [None]
		self.mu[0] = HOME_ADV
		self.P[0, 0] = HOME_ADV_SD ** 2
		self.center = 0.0              #average strength of D1 teams; ratings are shown relative to it (only differences matter)

	@property
	def home_advantage(self):
		return self.mu[0]

	def _update_center(self):
		d1 = [t.slot for t in self.teams.values() if t.conference != 'Other']
		self.center = float(np.mean(self.mu[d1])) if d1 else 0.0

	def __getstate__(self):
		#pickle only the used part of the arrays
		state = dict(self.__dict__)
		state['mu'], state['P'] = self.mu[:self.n].copy(), self.P[:self.n, :self.n].copy()
		return state

	def _grow(self):
		cap = 2 * len(self.mu)
		mu, P = np.zeros(cap), np.zeros((cap, cap))
		mu[:self.n] = self.mu[:self.n]
		P[:self.n, :self.n] = self.P[:self.n, :self.n]
		self.mu, self.P = mu, P

	def add_team(self, name):
		if self.n == len(self.mu): self._grow()
		s = self.n
		self.n += 1
		self.mu[s] = 0.0 if self.season_count == 0 else self.center + NEW_TEAM_MEAN
		self.P[s, :self.n] = 0
		self.P[:self.n, s] = 0
		self.P[s, s] = (FIRST_SEASON_SD if self.season_count == 0 else NEW_TEAM_SD) ** 2
		self.slots.append(name)
		self.teams[name] = KalmanTeam(self, name, s)

	def get_elo(self, name):
		if name not in self.teams: self.add_team(name)
		return self.teams[name].elo

	def is_new(self, name):
		return name not in self.teams or self.teams[name].total_games == 0

	def predict(self, home, away, neutral = False):
		'''
		returns (home win probability, predicted home scoring margin, variance of that margin's estimate)
		'''
		for t in (home, away):
			if t not in self.teams: self.add_team(t)
		h, a, nn = self.teams[home].slot, self.teams[away].slot, 0 if neutral else 1
		mu, P = self.mu, self.P
		margin = mu[h] - mu[a] + nn * mu[0]
		var = P[h, h] + P[a, a] - 2 * P[h, a] + nn * (P[0, 0] + 2 * P[0, h] - 2 * P[0, a])
		winp = 0.5 * (1 + math.erf(margin / math.sqrt(2 * (OUTCOME_VAR + var))))
		return winp, margin, var

	def snapshot(self):
		for team in self.teams.values():
			team.snapshots.append((self.date, team.elo))

	def _keep_slots(self, keep):
		'''
		reorders the state to just the given slots (slot 0 first). Dropping a state from a Gaussian is exact.
		'''
		keep = np.asarray(keep)
		k = len(keep)
		self.mu[:k] = self.mu[keep]
		self.P[:k, :k] = self.P[np.ix_(keep, keep)]
		kept_names = [self.slots[s] for s in keep[1:]]
		for name in set(self.slots[1:]) - set(kept_names):
			del self.teams[name]
		self.slots = [None] + kept_names
		for i, name in enumerate(kept_names, start = 1):
			self.teams[name].slot = i
		self.n = k
		self._update_center()

	def advance_to(self, date):
		'''
		move the filter forward to the start of date: add drift for the days that passed and prune idle non-D1 teams
		'''
		d = _ordinal(date)
		if self.day is not None and d > self.day:
			ii = np.arange(1, self.n)
			self.P[ii, ii] += DAILY_DRIFT_VAR * (d - self.day)
		self.day = d
		if PRUNE_DAYS is not None and (self.last_prune is None or d - self.last_prune >= 7):
			self.last_prune = d
			idle = {t.slot for t in self.teams.values() if t.conference == 'Other' and t.last_played is not None and d - t.last_played > PRUNE_DAYS}
			if idle:
				self._keep_slots([s for s in range(self.n) if s not in idle])

	def process_day(self, rows):
		'''
		update the ratings with all games played on one date
		rows: list of [neutral flag, away team, away score, home team, home score, date, away spread]
		'''
		for r in rows:
			for t in (r[3], r[1]):
				if t not in self.teams: self.add_team(t)
		oh, oa, onn, oy, oR = [], [], [], [], []
		for r in rows:
			h, a, nn = self.teams[r[3]].slot, self.teams[r[1]].slot, 1.0 if r[0] == '0' else 0.0
			oh.append(h); oa.append(a); onn.append(nn); oy.append(int(r[4]) - int(r[2])); oR.append(GAME_VAR)
			line = _parse_line(r[6]) if len(r) > 6 and MARKET_VAR is not None else None
			if line is not None: #the away team's spread is the market's predicted home margin
				oh.append(h); oa.append(a); onn.append(nn); oy.append(line); oR.append(MARKET_VAR)
		oh, oa, onn, oy = np.array(oh), np.array(oa), np.array(onn), np.array(oy, dtype = float)
		n, mu = self.n, self.mu
		Pv = self.P[:n, :n]
		PHt = Pv[:, oh] - Pv[:, oa] + onn[None, :] * Pv[:, [0]]
		S = PHt[oh, :] - PHt[oa, :] + onn[:, None] * PHt[0:1, :]
		S[np.diag_indices_from(S)] += oR
		innovation = oy - (mu[oh] - mu[oa] + onn * mu[0])
		try:
			X = la_solve(S, np.column_stack([innovation, PHt.T]), assume_a = 'pos', check_finite = False)
		except np.linalg.LinAlgError as e:
			#S is P projected onto the day's games plus at least MARKET_VAR on the diagonal, so it can only fail here if
			#the covariance was corrupted (non-finite values, or a BLAS library returning wrong matrix products)
			raise np.linalg.LinAlgError('%s on %s (%i observations): S finite=%s, min eigenvalue=%.3g, P finite=%s'
				% (e, rows[0][5], len(oy), np.isfinite(S).all(), np.linalg.eigvalsh(np.nan_to_num(S)).min(), np.isfinite(Pv).all())) from e
		mu[:n] += PHt @ X[:, 0]
		Pv -= PHt @ X[:, 1:]
		for r in rows:
			for t in (r[3], r[1]):
				team = self.teams[t]
				team.season_game_count += 1
				team.total_games += 1
				team.last_played = self.day
		self.date = rows[-1][5]
		self._update_center()

	def season_reset(self, date):
		'''
		drop teams with too few games, regress the rest toward their conference average, and add offseason uncertainty
		'''
		self.snapshot()
		keep = [0] + [t.slot for t in self.teams.values() if t.season_game_count >= GAMES_REQUIRED]
		keep.sort()
		self._keep_slots(keep)
		k = self.n
		projected = self._roster_projections(int(date[:4])) if ROSTER_COEFS else {}
		groups = {}
		for i in range(1, k):
			groups.setdefault(self.teams[self.slots[i]].conference, []).append(i)
		A = np.zeros((k, k))
		A[0, 0] = 1.0
		for members in groups.values():
			for i in members:
				A[i, members] += (1 - SEASON_CARRY) / len(members)
				A[i, i] += SEASON_CARRY
		self.mu[:k] = A @ self.mu[:k]
		P = A @ self.P[:k, :k] @ A.T
		P = (P + P.T) / 2
		added = np.full(k, PRESEASON_SD ** 2)
		for slot in projected: added[slot] = ROSTER_PRESEASON_SD ** 2
		P[np.arange(1, k), np.arange(1, k)] += added[1:]
		P[0, 0] += HOME_ADV_SD ** 2
		self.P[:k, :k] = P
		for slot, points in projected.items(): #the regression above keeps the D1 average, so self.center still applies
			self.mu[slot] = self.center + points
		d = _ordinal(date)
		self.day = d #offseason uncertainty comes from PRESEASON_SD, so no daily drift across the offseason
		for team in self.teams.values():
			team.season_game_count = 0
			team.last_played = d
		self._update_center()

	def _roster_projections(self, season):
		'''
		{slot: preseason rating in points vs. an average D1 team} for D1 teams with player data (see rosters.py)
		'''
		ratings = {t.name: t.points for t in self.teams.values()}
		conf_avg = rosters.conference_averages(ratings, {t.name: t.conference for t in self.teams.values()})
		c = ROSTER_COEFS
		out = {}
		for name, f in rosters.features(season, ratings).items():
			team = self.teams[name]
			if team.conference == 'Other': continue
			out[team.slot] = (c['intercept'] + c['last_rating'] * team.points + c['conference_avg'] * conf_avg[team.conference]
				+ sum(c[n] * x for n, x in zip(rosters.FEATURES, f)))
		return out

	def get_top(self, x):
		return sorted([(t.name, round(t.elo, 0), "{0:+.0f}".format(t.elo - t.snapshots[-1][-1])) for t in self.teams.values()], key = lambda x: x[1], reverse = True)[:x]

	def get_rankings_dict(self):
		ordered_teams = self.get_top(len(self.teams))
		return {ordered_teams[i][0]: i + 1 for i in range(len(ordered_teams))}

def sim(data, stop_short = '99999999', last_snap = 7, on_day = None):
	'''
	creates a KalmanSim, steps it through the data one date at a time up until stop_short, and returns it
	last_snap: take a snapshot this many days before the last date so get_top can report rating changes
	on_day: optional function(sim, rows) called before each date's games are processed (used for backtesting)
	'''
	this_sim = KalmanSim()
	last_day = data[-1][5] if stop_short > data[-1][5] else stop_short
	early = (datetime.datetime.strptime(last_day, '%Y%m%d') - datetime.timedelta(days = last_snap)).strftime('%Y%m%d')
	set_early = False
	this_month = int(data[0][5][4:6])
	i = 0
	while i < len(data):
		date = data[i][5]
		j = i
		while j < len(data) and data[j][5] == date: j += 1
		rows, i = data[i:j], j
		if date > early and not set_early:
			this_sim.snapshot() #save each team's rating here so we can calculate change in the last few days at the end
			set_early = True
		if date > stop_short: break
		row_month = int(date[4:6])
		if this_month in [3, 4] and row_month == 11: #when the data jumps from March/April to November, it's a new season
			this_sim.season_count += 1
			this_sim.season_reset(date)
		this_month = row_month
		this_sim.advance_to(date)
		if on_day is not None: on_day(this_sim, rows)
		this_sim.process_day(rows)
	return this_sim

CACHE_FOLDER = '.cache/'

def _settings():
	return (GAME_VAR, MARKET_VAR, DAILY_DRIFT_VAR, SEASON_CARRY, PRESEASON_SD, NEW_TEAM_MEAN, NEW_TEAM_SD, FIRST_SEASON_SD,
		HOME_ADV, HOME_ADV_SD, OUTCOME_VAR, GAMES_REQUIRED, PRUNE_DAYS, MAX_LINE, POINTS_TO_ELO,
		tuple(sorted(ROSTER_COEFS.items())) if ROSTER_COEFS else None, ROSTER_PRESEASON_SD, rosters.data_signature())

def cached_sim(data_filepath, data, stop_short = '99999999', last_snap = 7):
	'''
	kalman.sim with the result cached per data file, so repeated runs on the same data (e.g. several -G predictions)
	load in a second or two instead of re-running every season. New data, different settings, an edited
	conferences.csv, or an edited kalman.py all start fresh.
	'''
	import os, pickle, hashlib
	path = None
	try:
		key = repr((os.path.abspath(data_filepath), os.path.getsize(data_filepath), os.path.getmtime(data_filepath),
			os.path.getmtime('Data/conferences.csv'), stop_short, last_snap, _settings())).encode()
		with open(__file__, 'rb') as f: key += f.read()
		path = os.path.join(CACHE_FOLDER, 'kalman_' + hashlib.sha1(key).hexdigest()[:16] + '.pkl')
		if os.path.exists(path):
			with open(path, 'rb') as f: return pickle.load(f)
	except Exception:
		pass
	this_sim = sim(data, stop_short, last_snap)
	if path is not None:
		try:
			os.makedirs(CACHE_FOLDER, exist_ok = True)
			old = sorted((os.path.join(CACHE_FOLDER, f) for f in os.listdir(CACHE_FOLDER) if f.startswith('kalman_')), key = os.path.getmtime)
			for f in old[:-4]: os.remove(f) #keep only the few most recent
			with open(path, 'wb') as f: pickle.dump(this_sim, f)
		except Exception:
			pass
	return this_sim
