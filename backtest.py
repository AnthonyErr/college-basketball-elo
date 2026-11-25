'''
Backtest the rating models on every game in the data. Each morning's predictions use only games from earlier
days (how the daily predictions work), then the model updates with that day's results.

Scores: Brier score and log loss of win probabilities, and how far predicted spreads land from the final margin
compared to the closing Vegas spread (MAE), by week of season. Spread errors use games between established teams
(10+ games the previous season), the games Vegas lines are reliable for. ATS 3+ is how often the model's side
covers when it disagrees with the closing line by 3+ points (52.4% breaks even at -110 odds).

Usage: python backtest.py [-m kalman|elo|both] [-s 2021]
  -s: seasons starting in this year or later are reported as the held-out test (the Kalman settings were tuned
      on seasons before 2021)
'''
import argparse
import datetime
import math
import numpy as np
import utils
import elo
import kalman

FIRST_SCORED_SEASON = 2013 #give the models a few seasons of history first
WEEKS = [('wk1', 0, 6), ('wk2', 7, 13), ('wk3', 14, 20), ('wk4-5', 21, 34), ('wk6-8', 35, 55), ('wk9-12', 56, 83), ('wk13+', 84, 999)]

def game_info(data):
	'''
	season start year, day of season, and whether both teams are established, for every row
	'''
	season, day_of_season = [], []
	this_month, year, start = int(data[0][5][4:6]), int(data[0][5][:4]), data[0][5]
	for row in data:
		m = int(row[5][4:6])
		if this_month in [3, 4] and m == 11:
			year, start = int(row[5][:4]), row[5]
		this_month = m
		season.append(year)
		day_of_season.append((datetime.datetime.strptime(row[5], '%Y%m%d') - datetime.datetime.strptime(start, '%Y%m%d')).days)
	counts = {}
	for row, s in zip(data, season):
		for t in (row[1], row[3]): counts[(s, t)] = counts.get((s, t), 0) + 1
	established = [counts.get((s - 1, row[1]), 0) >= 10 and counts.get((s - 1, row[3]), 0) >= 10 for row, s in zip(data, season)]
	return np.array(season), np.array(day_of_season), np.array(established)

def run_kalman(data):
	preds = []
	def on_day(this_sim, rows):
		for r in rows:
			winp, margin, _ = this_sim.predict(r[3], r[1], neutral = r[0] != '0')
			preds.append((winp, margin))
	kalman.sim(data, on_day = on_day)
	return np.array(preds)

def run_elo(data):
	this_sim = elo.ELO_Sim()
	preds = []
	this_month = int(data[0][5][4:6])
	i = 0
	while i < len(data):
		j = i
		while j < len(data) and data[j][5] == data[i][5]: j += 1
		rows, i = data[i:j], j
		m = int(rows[0][5][4:6])
		if this_month in [3, 4] and m == 11:
			this_sim.season_count += 1
			this_sim.season_reset(elo.SEASON_CARRY)
		this_month = m
		for r in rows:
			diff = this_sim.get_elo(r[3]) + (elo.HOME_ADVANTAGE if r[0] == '0' else 0) - this_sim.get_elo(r[1])
			preds.append((elo.winp(diff), diff / -elo.ELO_TO_POINTS_FACTOR))
		for r in rows:
			this_sim.date = r[5]
			elo.step_elo(this_sim, r, elo.K_FACTOR, elo.HOME_ADVANTAGE)
	return np.array(preds)

def score(preds, margin, line, mask):
	p = np.clip(preds[mask, 0], 1e-9, 1 - 1e-9)
	won = (margin[mask] > 0).astype(float)
	brier = np.mean((p - won) ** 2)
	logloss = -np.mean(won * np.log(p) + (1 - won) * np.log(1 - p))
	return brier, logloss

def spread_errors(preds, margin, line, mask):
	mk = mask & ~np.isnan(line)
	mae, vegas_mae = np.mean(np.abs(preds[mk, 1] - margin[mk])), np.mean(np.abs(line[mk] - margin[mk]))
	bets = mk & (np.abs(preds[:, 1] - line) >= 3) & (margin != line)
	ats = np.mean((preds[bets, 1] > line[bets]) == (margin[bets] > line[bets])) if bets.any() else float('nan')
	return mae, vegas_mae, ats, bets.sum()

def report(name, preds, info, margin, line, seasons):
	season, dos, est = info
	in_seasons = np.isin(season, seasons)
	print(f'\n=== {name}: seasons {min(seasons)}-{str(max(seasons) + 1)[2:]}')
	print(f"{'week':7} {'games':>6} {'brier':>7} | {'MAE':>5} {'Vegas':>5} {'gap':>6} | {'ATS 3+':>6} {'bets':>5}")
	for w, a, b in WEEKS + [('ALL', 0, 999)]:
		mk = in_seasons & (dos >= a) & (dos <= b)
		brier, logloss = score(preds, margin, line, mk)
		mae, vmae, ats, nb = spread_errors(preds, margin, line, mk & est)
		print(f'{w:7} {mk.sum():6d} {brier:7.4f} | {mae:5.2f} {vmae:5.2f} {mae - vmae:+6.2f} | {ats:6.3f} {nb:5d}' + (f'   log loss {logloss:.4f}' if w == 'ALL' else ''))

def main(models, holdout):
	data = utils.read_csv(utils.get_latest_data_filepath())
	info = game_info(data)
	margin = np.array([int(r[4]) - int(r[2]) for r in data])
	line = np.array([kalman._parse_line(r[6]) if kalman._parse_line(r[6]) is not None else np.nan for r in data])
	years = sorted(set(info[0]))
	train = [y for y in years if FIRST_SCORED_SEASON <= y < holdout]
	test = [y for y in years if y >= holdout]
	for model in models:
		preds = run_kalman(data) if model == 'kalman' else run_elo(data)
		for seasons in (train, test):
			if seasons: report(model, preds, info, margin, line, seasons)

def parseArguments():
	parser = argparse.ArgumentParser(description = 'Backtest the rating models on all historical games')
	parser.add_argument('-m', '--model', default = 'both', choices = ['kalman', 'elo', 'both'], help = 'Which model(s) to backtest. Default is both')
	parser.add_argument('-s', '--split', default = 2021, type = int, help = 'Seasons starting this year or later are reported separately as the held-out test. Default 2021')
	return parser.parse_args()

if __name__ == '__main__':
	args = parseArguments()
	main(['kalman', 'elo'] if args.model == 'both' else [args.model], args.split)
