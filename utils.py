import csv
import re
from os import listdir
import plotly.graph_objects as go
from datetime import datetime, timedelta

DATA_FOLDER = 'Data/'
OUTPUTS_FOLDER = 'Outputs/'

def save_data(filepath, data):
	with open(filepath, "w") as f:
		wr = csv.writer(f)
		for row in data:
			wr.writerow(row)

def read_csv(filepath):
	with open(filepath, encoding = 'utf-8-sig') as csvfile:
		return list(csv.reader(csvfile))

def shift_dstring(day_string, days):
	return (datetime.strptime(day_string, "%Y%m%d") + timedelta(days = days)).strftime('%Y%m%d')

def get_latest_data_filepath():
	r = re.compile("[0-9]{8}-[0-9]{8}.csv")
	elligible_data = list(filter(r.match, listdir(DATA_FOLDER)))
	return DATA_FOLDER + sorted(elligible_data, key = lambda x: x[9:17], reverse = True)[0]

def table_output(df, table_title, order = None):
	if order != None:
		df = df[order]
	df.to_csv(OUTPUTS_FOLDER + table_title + '.csv', index = False)
	fig = go.Figure(data=[go.Table(
	    header=dict(values=list(df.columns),
	                fill_color='paleturquoise',
	                align='left'),
	    cells=dict(values=[df[col].to_list() for col in list(df)],
	               # fill_color='lavender',
	               align='left'))
	])
	fig.update_layout(title = {'text': table_title, 'xanchor': 'center', 'x': .5})
	fig.show()