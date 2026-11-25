import elo
import difflib
import random
import pandas as pd
import utils
import argparse
import scraper
import datetime
import spread_enricher
import os

DATA_FOLDER = utils.DATA_FOLDER
ALL_ROUNDS = ['first', 'second', 'sixteen', 'eight', 'four', 'final', 'champion']

# common names for teams whose name in the data is different (the single-game predictor accepts these, any case)
TEAM_ALIASES = {
	'North Carolina': 'UNC', 'Connecticut': 'UConn', 'Southern California': 'USC', 'Brigham Young': 'BYU',
	'Louisiana State': 'LSU', 'Texas Christian': 'TCU', 'Southern Methodist': 'SMU', 'Central Florida': 'UCF',
	'Virginia Commonwealth': 'VCU', 'Mississippi': 'Ole Miss', 'Pittsburgh': 'Pitt', 'Massachusetts': 'UMass',
	'UMass Lowell': 'UMass-Lowell', 'Nevada-Las Vegas': 'UNLV', 'Alabama-Birmingham': 'UAB', 'Texas-El Paso': 'UTEP',
	'Texas-San Antonio': 'UTSA', 'Texas-Arlington': 'UT Arlington', 'UTRGV': 'Texas-Rio Grande Valley',
	'East Tennessee State': 'ETSU', 'Long Island': 'LIU', 'Long Island University': 'LIU', 'Illinois-Chicago': 'UIC',
	'Maryland-Baltimore County': 'UMBC', 'Virginia Military Institute': 'VMI', 'North Carolina State': 'NC State',
	'Miami': 'Miami (FL)', 'Miami Florida': 'Miami (FL)', 'Miami Ohio': 'Miami (OH)', "St. John's": "St. John's (NY)",
	"Saint John's": "St. John's (NY)", "Saint Joseph's": "St. Joseph's", "Saint Peter's": "St. Peter's",
	'Loyola Chicago': 'Loyola (IL)', 'Loyola Maryland': 'Loyola (MD)', 'Albany': 'Albany (NY)', 'UC Davis': 'UC-Davis',
	'UC Irvine': 'UC-Irvine', 'UC Riverside': 'UC-Riverside', 'UC San Diego': 'UC-San Diego', 'UC Santa Barbara': 'UCSB',
	'Cal': 'California', 'SIUE': 'SIU-Edwardsville', 'SIU Edwardsville': 'SIU-Edwardsville', 'UT Martin': 'UT-Martin',
	'Tennessee-Martin': 'UT-Martin', 'UAPB': 'Arkansas-Pine Bluff', 'UMES': 'Maryland-Eastern Shore',
	'Louisiana-Lafayette': 'Louisiana', 'ULM': 'Louisiana-Monroe', 'FIU': 'Florida International',
	'FAU': 'Florida Atlantic', 'FGCU': 'Florida Gulf Coast', 'FDU': 'Fairleigh Dickinson', 'SFA': 'Stephen F. Austin',
	'Purdue Fort Wayne': 'Purdue-Fort Wayne', 'UMKC': 'Kansas City', 'Detroit Mercy': 'Detroit',
	'Sam Houston': 'Sam Houston State', 'McNeese': 'McNeese State', 'Nicholls': 'Nicholls State',
	'Texas A&M Corpus Christi': 'Texas A&M-Corpus Christi', 'Pennsylvania': 'Penn', 'Prairie View A&M': 'Prairie View',
	'Saint Francis': 'Saint Francis (PA)', 'Queens': 'Queens (NC)', 'Grambling State': 'Grambling',
	'Middle Tennessee State': 'Middle Tennessee', 'Southern Mississippi': 'Southern Miss',
	'Bowling Green': 'Bowling Green State', 'Charleston': 'College of Charleston', 'Wisconsin-Milwaukee': 'Milwaukee',
	'Wisconsin-Green Bay': 'Green Bay', 'Nebraska-Omaha': 'Omaha', 'Arkansas-Little Rock': 'Little Rock',
	'Cal State Long Beach': 'Long Beach State', 'UNCW': 'UNC Wilmington', 'UNCG': 'UNC Greensboro',
	'UNCA': 'UNC Asheville',
}

def resolve_team_name(known_teams, name):
	'''
	returns the data's name for 'name': exact match, then case-insensitive or alias match, then the closest spelling
	(a typo of an alias counts too). Prints a note whenever it switches names. Returns None if nothing is close
	'''
	if name in known_teams: return name
	candidates = {t.lower(): t for t in known_teams}
	candidates.update({alias.lower(): team for alias, team in TEAM_ALIASES.items() if team in known_teams and alias.lower() not in candidates})
	query = name.lower()
	match = candidates.get(query)
	if match is None:
		#closest spelling; typos rarely change the first letter, so that breaks ties (e.g. "Dook" is as close to "Ohio" as to "Duke")
		matcher = difflib.SequenceMatcher(b = query)
		def score(candidate):
			matcher.set_seq1(candidate)
			return matcher.ratio() + (0.05 if candidate[:1] == query[:1] else 0)
		best = max(candidates, key = score)
		matcher.set_seq1(best)
		if matcher.ratio() < 0.4: return None
		match = candidates[best]
	print(f'Auto team switch: "{name}" -> "{match}"')
	return match

def matchups_from_list(team_list):
	'''
	convert a list of team names to a list of tuples that will match up consecutive teams together as opponents
	'''
	ret_list = []
	for i in range(len(team_list)):
		if i % 2 == 0:
			ret_list.append([team_list[i], team_list[i + 1]])
	return ret_list

def is_new_team(elo_state, name):
	'''
	True if the model has no games for this team yet (marked with a * in outputs)
	'''
	if hasattr(elo_state, 'is_new'): return elo_state.is_new(name)
	return elo_state.get_elo(name) == elo.NEW_ELO

def predict_game(elo_state, home, away, pick_mode = False, neutral = False, verbose = False):
	'''
	uses the specified elo_state to predict the outcome of a game between home and away
	pick_mode: 0 -> chooose winners probabilistically, 1 -> always choose the better team, 2 -> choose a random team
	neutral: specifies if the game is played at a neutral site
	returns a winner (either home or away), their win probability, and the home team's predicted spread
	'''

	#to account for conference tournaments where there are byes, the bracket will be formatted to have a team play itself
	if home == away:
		return home, "BYE", "N/A"

	#to account for play-in games, we need to predict those outcomes first and they will have a / in the team name
	if '/' in home:
		pihome, piaway = home.split('/')[0], home.split('/')[1]
		home = predict_game(elo_state, pihome, piaway, pick_mode = pick_mode, neutral = neutral)[0]
	elif '/' in away:
		pihome, piaway = away.split('/')[0], away.split('/')[1]
		away = predict_game(elo_state, pihome, piaway, pick_mode = pick_mode, neutral = neutral)[0]
	
	if hasattr(elo_state, 'predict'):
		#Kalman filter: learned home-court advantage, and win probabilities that account for how uncertain the ratings are
		winp_home, home_margin, _ = elo_state.predict(home, away, neutral = neutral)
	else:
		#Elo: a flat home-court advantage (the old rating-dependent adjustment made held-out predictions worse)
		elo_diff = elo_state.get_elo(home) + (0 if neutral else elo.HOME_ADVANTAGE) - elo_state.get_elo(away)
		winp_home, home_margin = elo.winp(elo_diff), elo_diff / -elo.ELO_TO_POINTS_FACTOR
	home_spread = round(-home_margin, 1)

	if verbose: print(home, "{0:.0%}".format(winp_home), str(home_spread), away, "{0:.0%}".format(1 - winp_home), str(-home_spread))

	if pick_mode == 1:
		winner = home if home_margin > 0 else away
	elif pick_mode == 2:
		winner = random.choice([home, away])
	else:
		winner = random.choices([home, away], weights = (winp_home, 1-winp_home))[0]

	return winner, "{0:.0%}".format(winp_home) if winner == home else "{0:.0%}".format(1 - winp_home), home_spread

def predict_tournament(elo_state, tournamant_teams, pick_mode = 0, verbose = False, rounds = ALL_ROUNDS):
	'''
	uses the specified elo_state to simulate a single tournament for teams in tournament teams
	pick_mode: 0 -> chooose winners probabilistically, 1 -> always choose the better team, 2 -> choose a random team
	outputs a Plotly table summarizing predictions and saves a csv
	'''
	results = {rounds[0]: tournamant_teams}

	remaining = tournamant_teams

	for r in rounds[1:]:
		matchups = matchups_from_list(remaining)
		winners = [predict_game(elo_state, i[0], i[1], pick_mode = pick_mode, neutral = True) for i in matchups]
		remaining = [i[0] for i in winners]
		results[r] = [(i[0], i[1]) for i in winners]

	if verbose:
		output = pd.DataFrame.from_dict(results, orient = 'index').transpose().fillna('').replace('(', '').replace(')', '')
		modes = {0: "Probabilistic Choice", 1: "Better Team", 2: "Random Team"}
		utils.table_output(output, 'Tournament Predictions Based on Ratings through ' + elo_state.date + ' - ' + modes[pick_mode])

	return results

def sim_tournaments(elo_state, tournamant_teams, n, verbose = False, rounds = ALL_ROUNDS):
	'''
	uses the specified elo_state to simulate a tournament (specified by tournament_teams) n times
	each row in the output specifies the share of simulations in which a team made it to the corresponding round
	outputs a Plotly table summarizing predictions and saves a csv
	'''
	sim_results = {}
	for team in tournamant_teams:
		if '/' in team:
			team1, team2 = team.split('/')[0], team.split('/')[1]
			sim_results[team1] = [0 for _ in range(len(rounds) - 1)]
			sim_results[team2] = [0 for _ in range(len(rounds) - 1)]
		else:
			sim_results[team] = [0 for _ in range(len(rounds) - 1)]

	for _ in range(n):
		results = predict_tournament(elo_state, tournamant_teams, rounds = rounds)
		for r in range(1, len(rounds)):
			for team in results[rounds[r]]:
				sim_results[team[0]][r-1] += 1

	if verbose:
		current_rankings = elo_state.get_rankings_dict()
		formatted = [[f'{team} (#{current_rankings[team]})'] + [round(i/n, 4) for i in sim_results[team]] for team in sim_results]
		output = pd.DataFrame(formatted, columns = ['team'] + rounds[1:]).sort_values(rounds[-1], ascending = False).drop_duplicates()
		utils.table_output(output, 'Tournament Predictions Based on Ratings through ' + elo_state.date + ' and ' + str(n) + ' Simulations')
		return output
	return None

def predict_next_day(elo_state, forecast_date, auto):
	'''
	checks scrape source for games on forecast_date and uses the elo state to predict each game's outcome 
	outputs to Datawrapper (interactive tables) and saves a csv
	'''
	import datawrapper_publisher #imported only when publishing: the datawrapper package is slow to import
	scraper.scrape_neutral_data() #set the NEUTRAL_MAP for predictions
	games = scraper.scrape_scores(forecast_date)
	if games == []:
		print("No games scheduled on Sports Reference at this time for " + forecast_date.strftime('%Y%m%d'))
		return
	predictions = []
	for game in games:
		is_neutral = True if game[0] == 1 else False
		winner, prob, home_spread = predict_game(elo_state, game[3], game[1], pick_mode = 1, neutral = is_neutral)
		if game[1] == winner:
			predictions.append([game[0], game[1], prob, -home_spread, game[3], "{0:.0%}".format(1 - (float(prob[:-1])/100)), home_spread])
		else:
			predictions.append([game[0], game[1], "{0:.0%}".format(1 - (float(prob[:-1])/100)), -home_spread, game[3], prob, home_spread])
	predictions, timestamp = spread_enricher.add_spreads_to_todays_preds(predictions, forecast_date)
	output = pd.DataFrame(predictions, columns = ['Neutral', 'Away', 'Away Win Prob.', 'Away Pred. Spread', 'Live Away Spread', 'Home', 'Home Win Prob.', 'Home Pred. Spread'])
	
	# Add column for absolute difference between live and predicted spread
	def calc_spread_diff(row):
		if row['Live Away Spread'] == 'NL':
			return 'N/A'
		try:
			return round(abs(float(row['Live Away Spread']) - float(row['Away Pred. Spread'])), 1)
		except (ValueError, TypeError):
			return 'N/A'
	output['Abs. Pred. Diff.'] = output.apply(calc_spread_diff, axis=1)
	
	#add a * to the team names that are brand new to the simulation. Take those predictions with a grain of salt
	output['Away'] = output.apply(lambda x: x['Away'] + "*" if is_new_team(elo_state, x['Away']) else x['Away'], axis = 1)
	output['Home'] = output.apply(lambda x: x['Home'] + "*" if is_new_team(elo_state, x['Home']) else x['Home'], axis = 1)
	
	spreads_string = ''
	if timestamp != 'N/A':
		spreads_string = ' with Spreads as of '
		spreads_string +=  (timestamp - datetime.timedelta(hours = 5)).strftime('%Y%m%d at %H%M') if auto else timestamp.strftime('%Y%m%d at %H%M')
	
	# Still save CSV for backup
	utils.table_output(output, forecast_date.strftime('%Y%m%d') + ' Game Predictions Based on Ratings through ' + elo_state.date + spreads_string)
	
	# Prepare data for Datawrapper
	date_str = forecast_date.strftime('%Y-%m-%d')
	new_top_100 = pd.DataFrame(elo_state.get_top(100), columns = ['Team', 'Elo Rating', '7 Day Change'])
	
	# Generate markdown content for embedding
	markdown_content = utils.get_markdown_content(output, new_top_100, date_str)
	
	probs_id = os.environ.get('DW_PROBABILITIES_CHART_ID')
	probs_url = f"https://datawrapper.dwcdn.net/{probs_id}/1/" if probs_id else None
	# Try to publish to Datawrapper, fallback to Markdown if it fails
	try:
		print("\n📊 Publishing to Datawrapper...")
		pred_chart_id, pred_url = datawrapper_publisher.create_or_update_predictions_table(output, date_str, elo_state.date)
		rank_chart_id, rank_url = datawrapper_publisher.create_or_update_rankings_table(new_top_100)
		datawrapper_publisher.save_datawrapper_embeds(pred_url, rank_url, date_str, markdown_content, probabilities_url=probs_url)
		print(f"✓ Successfully published to Datawrapper")
		print(f"  Predictions chart: {pred_url}")
		print(f"  Rankings chart: {rank_url}")
		
		# Also ensure index.md is updated
		utils.save_markdown_df(output, new_top_100, date_str)
	except Exception as e:
		print(f"\n⚠ Warning: Failed to publish to Datawrapper: {e}")
		print("Falling back to Markdown output")
		# Fallback to original Markdown output
		utils.save_markdown_df(output, new_top_100, date_str)
		
		# Also save a fallback HTML version so index.html is not stale
		# This uses local HTML tables instead of Datawrapper iframes
		try:
			datawrapper_publisher.save_fallback_html(output, new_top_100, date_str, markdown_content, probabilities_url=probs_url)
		except Exception as html_e:
			print(f"⚠ Warning: Failed to save fallback HTML: {html_e}")

def main(auto = False, forecast_date = False, matchup = False, neutral = False, sim_mode = False, stop_short = '99999999', bracket = False, pick_mode = 0, bracket_round_start = 0, sim_output_path = None, sim_publish_datawrapper = False):
	'''
	Retrieves an elo simulation through the specified 'stop_short' date then cascades through options:
	1. if a 'matchup' of two teams is provided, print out predictions for that matchup - factoring in 
	whether the matchup is at a 'neutral' site or not
	2. if 'sim_mode' is specified [a filepath to a bracket, a number of simulations], then run the specified 
	number of bracket simulations on the specified bracket
	3. if just 'bracket', which is a filepath to a bracket, is specified, deliver a one-time prediction for that 
	bracket based on the starting teams. 'pick_mode' chooses probabilistically (0), the better team (1), or randomly (2)
	4. if nothing is specified, make predictions for today's games
	'''
	elo_state = elo.main(stop_short = stop_short)
	if matchup != False:
		home, away = matchup
		#an unknown name would silently become a brand-new (weak) team, so switch it to the closest known name
		def resolve(name):
			parts = []
			for part in name.split('/'):
				match = resolve_team_name(elo_state.teams, part)
				if match is None:
					print(f'Unknown team "{part}" and no close match found')
					return None
				parts.append(match)
			return '/'.join(parts)
		home, away = resolve(home), resolve(away)
		if home is None or away is None:
			return
		print('Ratings through ' + elo_state.date)
		if neutral:
			print(home, "vs.", away, "@ neutral site")
		else:
			print(away, "@", home)
		predict_game(elo_state, home, away, neutral = neutral, verbose = True)
	elif sim_mode != False:
		file, simulations = sim_mode
		tournamant_teams = list(pd.read_csv(file).iloc[:,bracket_round_start].dropna())
		rounds = list(pd.read_csv(file).columns)
		output = sim_tournaments(elo_state, tournamant_teams, n = int(simulations), verbose = True, rounds = rounds[bracket_round_start:])
		if output is not None:
			if sim_output_path:
				d = os.path.dirname(sim_output_path)
				if d:
					os.makedirs(d, exist_ok = True)
				output.to_csv(sim_output_path, index = False)
				print(f"✓ Saved probabilities to {sim_output_path}")
			if sim_publish_datawrapper:
				try:
					import datawrapper_publisher
					_, url = datawrapper_publisher.create_or_update_probabilities_table(
						output, elo_date = elo_state.date, n_sims = int(simulations)
					)
					print(f"✓ Published to Datawrapper: {url}")
				except Exception as e:
					print(f"⚠ Datawrapper publish failed: {e}")
	elif bracket != False:
		tournamant_teams = list(pd.read_csv(bracket).iloc[:,bracket_round_start].dropna())
		rounds = list(pd.read_csv(bracket).columns)
		predict_tournament(elo_state, tournamant_teams, pick_mode = pick_mode, verbose = True, rounds = rounds[bracket_round_start:])
	else:
		forecast_date = utils.current_basketball_date() if forecast_date == False else datetime.datetime.strptime(forecast_date, "%Y%m%d")
		# -A shifts a UTC clock into US Eastern for the spreads timestamp below.
		# The forecast calendar day is already the US Pacific date, so do not roll it backward.
		if auto and isinstance(forecast_date, datetime.datetime):
			forecast_date = forecast_date - datetime.timedelta(hours = 5)
		predict_next_day(elo_state, forecast_date, auto)
	utils.clean_up_old_outputs_and_data()

def parseArguments():
	parser = argparse.ArgumentParser(description = 'This script allows the user to predict results of individual games, create a bracket prediction for a tournament, or simulate most likely outcomes for a bracket')
	parser.add_argument('-F', '--ForecastDate', default = False, type = str, help = 'Use if trying to predict games for a particular date in the future, set -f equal to the day you want to predict. If games are scheduled on Sports Reference, the output will be predictions for all games. Enter date as YYYYMMDD (e.g. 20190315)') 
	parser.add_argument('-G', '--GamePredictor', default = False, nargs = 2, type = str, help = 'Use to predict a single game. List home team as a string and away team as a string. Use -n flag to indicate a neutral site')
	parser.add_argument('-n', '--neutral', action = 'store_true', help = 'Use if predicting a single game at a neutral site')
	parser.add_argument('-S', '--SimMode', default = False, nargs = 2, help = 'Use this to run monte carlo simulations for a tournament and see in what share of simulations a team makes it to each round. Enter the filename storing the tournament participants as a string and an integer number of simulations to run')
	parser.add_argument('-d', '--dateSim', type = str, default = '99999999', help = 'Use if predicting games or tournament as of a date in the past. Enter date as YYYYMMDD (e.g. 20190315). Can be specified in any mode to get outputs as of the specified date')
	parser.add_argument('-P', '--PredictBracket', default = False, type = str, help = "Use to predict results of a tournament (i.e. generate a single bracket). Enter the filename storing the tournament participants in the first column. Use the -m flag to specify how each matchup should be decided. Don't forget to use -d if predicting this tournament as of a date in the past")
	parser.add_argument('-m', '--mode', default = 0, choices = [0, 1, 2], type = int, help = "By default, the winner for each matchup in a tournament prediction is selected probabilistically (mode 0). Use 1 to have the model always pick the 'better' team according to Elo ratings. Use 2 to decide each matchup with a coinflip (random selection)")
	parser.add_argument('-r', '--round', default = 0, type = int, help = "When using -P PredictBracket or -S SimMode, optionally specify which round of the tournament file to start making predictions from (e.g. 0 = predict advancements from the 0th column onwards in the tournament file)")
	parser.add_argument('-o', '--output', default = None, type = str, dest = 'sim_output', help = 'When using -S SimMode, save the probabilities CSV to this path (e.g. Outputs/2026/2026 Pre-Tournament Probabilities.csv)')
	parser.add_argument('--datawrapper', action = 'store_true', dest = 'sim_datawrapper', help = 'When using -S SimMode, publish the probabilities table to Datawrapper (requires DATAWRAPPER_API_TOKEN and optionally DW_PROBABILITIES_CHART_ID)')
	parser.add_argument('-A', '--auto', action = 'store_true', help = 'Used only by github actions to account for the time difference on the virtual machine')
	return parser.parse_args()

if __name__ == '__main__':
	args = parseArguments()
	main(auto = args.auto, forecast_date = args.ForecastDate, matchup = args.GamePredictor, neutral = args.neutral, sim_mode = args.SimMode,
		stop_short = args.dateSim, bracket = args.PredictBracket, pick_mode = args.mode, bracket_round_start = args.round,
		sim_output_path = args.sim_output, sim_publish_datawrapper = args.sim_datawrapper)