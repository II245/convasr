import os
import sys
import time
import json
import random
import itertools
import subprocess

def expjson(root_dir, experiment_id, epoch = None, iteration = None, columns = {}, meta = {}, tag = '', name = None, git_revision = True, git_http = None):
	if git_revision is True:
		try:
			git_revision, git_comment = map(lambda b: b.decode('utf-8'), subprocess.check_output(['git', 'log', '--format=%h%x00%s', '--no-decorate', '-1']).split(b'\x00'))
		except:
			git_revision, git_comment = 'error', 'error'
	else:
		git_revision, git_comment = ''

	obj = dict(
		experiment_id = experiment_id, 
		iteration = f'epoch{epoch:02d}_iter{iteration:07d}' if epoch is not None and iteration is not None else 'test', 
		columns = columns, 
		time = int(time.time()), 
		meta = meta, 
		git_revision = git_revision, 
		git_comment = git_comment, 
		git_http = git_http.replace('%h', git_revision) if git_http else None,
		tag = tag
	)
	
	json_dir = os.path.join(root_dir, 'json')
	os.makedirs(json_dir, exist_ok = True)
	name = f'{int(time.time())}.{random.randint(10, 99)}.json' if name is None else name
	json_path = os.path.join(json_dir, name)
	json.dump(obj, open(json_path, 'w'), sort_keys = True, indent = 2, ensure_ascii = False)

def exphtml(root_dir, html_dir = 'public', strftime = '%Y-%m-%d %H:%M:%S', repeat = 5, timeout = 5):
	json_dir = os.path.join(root_dir, 'json')
	html_dir = os.path.join(root_dir, html_dir)
	os.makedirs(html_dir, exist_ok = True)
	html_path = os.path.join(html_dir, 'index.html')
	generated_time = time.strftime(strftime, time.gmtime())

	def json_load(path):
		try:
			j = json.load(open(path))
			j['path'] = path
			j['meta'], j['tag'], j['iteration'], j['git_http'], j['git_revision'], j['git_comment'] = j.get('meta', {}), j.get('tag', ''), j.get('iteration', ''), j.get('git_http', ''), j.get('git_revision', ''), j.get('git_comment', '')
			return j
		except:
			return {}

	groupby = lambda items, key: [(list(g), k) for k, g in itertools.groupby(sorted(items, key = key), key = key)]
	list_map = lambda *args: list(map(*args))
	map0 = lambda func, items: [(func(elem0), *_) for elem0, *_ in items]
	strip_hidden = lambda s: s.lstrip('.')
	hide = lambda s: '.' + strip_hidden(s)

	events = list(filter(None, (json_load(os.path.join(json_dir, json_file)) for json_file in os.listdir(json_dir))))
	by_experiment_id = lambda e: e['experiment_id']
	by_tag = lambda e: e['tag']
	by_time = lambda e: e['time']
	by_iteration = lambda e: (e['iteration'], e['time'])
	by_max_event_time = lambda exp: max(map(by_time, exp[0]))
	columns_union = lambda experiments: set(sum((list_map(strip_hidden, e['columns']) for events in experiments for e in events), []))
	fields_union = lambda experiments: set(sum((list_map(strip_hidden, c) for events in experiments for e in events for c in e['columns'].values()), []))
	tags_union = lambda experiments: set(e['tag'] for events in experiments for e in events)
	last_event_by_column = lambda events, c: [e for e in events if c in map(strip_hidden, e['columns'])][-1]
	last_event_by_field = lambda events, f: [e for e in events if f in sum(map(list, e['columns'].values()), [])][-1]
	
	experiments, experiments_id = zip(*sorted(map0(lambda events: sorted(events, key = by_iteration), groupby(events, by_experiment_id)), key = by_max_event_time, reverse = True))

	columns = sorted(columns_union(experiments))
	fields = sorted(fields_union(experiments))
	tags = sorted(tags_union(experiments))

	experiment_recent = experiments[0]
	columns_recent = columns_union([experiment_recent])
	fields_recent = columns_union([experiment_recent])

	columns_checked = {c : not ( c not in columns_recent or hide(c) in last_event_by_column(experiment_recent, c) ) for c in columns}
	fields_checked =  {f : not ( f not in fields_recent or hide(f) in last_event_by_field(experiment_recent, f) ) for f in fields}

	with open(html_path, 'w') as html:
		html.write('<html>')
		html.write('<head>')
		html.write(f'<title>Results @ {generated_time}</title>')
		html.write('''
			<meta http-equiv="refresh" content="600" />
			<script src="https://cdn.jsdelivr.net/npm/vega@5.8.1"></script>
			<script src="https://cdn.jsdelivr.net/npm/vega-lite@4.0.0-beta.12"></script>
			<script src="https://cdn.jsdelivr.net/npm/vega-embed@6.1.0"></script>
			<style>
				.nowrap {white-space:nowrap}
				.m0 {margin:0px}
				.textleft {text-align:left}
				.mr{margin-right:3px}
				.sepright {border-right: 1px solid black}
				.git {background-color:lightblue}
				.meta {background-color:lightgray}
			</style>
		''')
		html.write('</head>')
		html.write('<body onload="onload()">')
		html.write('''
		<script>
			var toggle = className => Array.from(document.querySelectorAll(`.${className}`)).map(e => {e.hidden = !e.hidden});

			function onload()
			{
				const hash = window.location.hash.replace('#', '');
				const parts = hash.length > 0 ? hash.split(';') : [];
				
				parts
					.map(p => p.split('='))
					.map(([prefix, selector]) =>
					{
						if(selector)
						{
							Array.from(document.querySelectorAll(`input[value^=${prefix}]:not([name*=${selector}])`)).map(checkbox => checkbox.click());
							document.getElementById(`filter_${prefix}`).value = selector;
						}
					});
			}

			function filter_(event)
			{
				const filter_field = document.getElementById('filter_field').value, filter_col = document.getElementById('filter_col').value, filter_exp = document.getElementById('filter_exp').value;
				window.location.hash = `field=${filter_field};col=${filter_col};exp=${filter_exp} `.replace('field=;', '').replace('col=;', '').replace('exp= ', '').trimEnd();
				
				window.location.reload();
				event.preventDefault();
				return false;
			}
		</script>''')
		html.write(f'<h1>Generated at {generated_time}</h1>')
		html.write('<table width="100%">')
	
		def render_header_line(name, names, checked):
			return f'<tr><th class="textleft">{name}</th><td><div id="searchbox"><form action="." class="m0"><label class="nowrap"><input type="text" name="search" placeholder="Filter"></label></form></div></td><td>' + (''.join(f'<label class="nowrap"><input type="checkbox" name="{c}" {"checked" if checked is True or checked[c] else ""}/>{c}</label>' for c in names) if checked is not False else '') + '</td></tr>'

		def render_experiment(events, experiment_id):
			generated_time = time.strftime(strftime, time.localtime(events[-1]['time']))
			res = f'''<tr><td title="{generated_time}"><strong>{experiment_id}</strong></td>''' + ''.join(f'<td><strong>{c}</strong></td>' for c in columns) + '</tr>'
			for i, e in enumerate(events):
				generated_time = time.strftime(strftime, time.localtime(e['time']))
				meta = json.dumps(e['meta'], sort_keys = True, indent = 2, ensure_ascii = False)
				res += '<tr><td title="{generated_time}" class="sepright">{iteration}</td>'.format(generated_time = generated_time, **e)
				res += ''.join('<td>' + ''.join(f'<span title="{f}" class="mr">{render_cell(e, c, f)}</span>' for f in fields) + '</td>' for c in columns)
				res += '</tr>'
				res += '<tr class="git"><td><a href="{git_http}">@{git_revision}</a></td><td colspan="100">{git_comment}</td></tr>\n'.format(**e)
				res += f'<tr class="meta"><td colspan="100"><pre>{meta}</pre></td></tr>\n'
			return res

		def render_cell(event, column, field):
			return event['columns'].get(column, {}).get(field, '')

		html.write(render_header_line('fields', fields, fields_checked))
		html.write(render_header_line('columns', columns, columns_checked))
		html.write(render_header_line('experiments', experiments_id, False))
		html.write(render_header_line('tags', tags, True))
		html.write('</table><hr/>')
		
		html.write('<table cellpadding="2px" cellspacing="0">')
		for events, experiment_id in zip(experiments, experiments_id):
			html.write(render_experiment(events, experiment_id))
		html.write('</table></body></html>')
		
		try:
			subprocess.check_call(['git', 'add', '-A'], cwd = root_dir)
			subprocess.check_call(['git', 'commit', '-a', '--allow-empty-message', '-m', ''], cwd = root_dir)
			for i in range(repeat):
				try:
					subprocess.check_call(['git', 'pull'], cwd = root_dir)
					subprocess.check_call(['git', 'push'], cwd = root_dir)
					break
				except:
					print(sys.exc_info())
		except:
			print(sys.exc_info())

if __name__ == '__main__':
	exphtml(sys.argv[1])
