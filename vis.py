import os
import collections
import glob
import json
import io
import sys
import time
import random
import itertools
import argparse
import base64
import subprocess
import matplotlib.pyplot as plt
import seaborn
import altair
import torch
import torch.nn.functional as F
import audio
import datasets
import ru
import metrics
import models
import ctc
import transcripts

def transcript(html_path, sample_rate, mono, transcript):
	if isinstance(transcript, str):
		transcript = json.load(open(transcript))

	has_hyp = True
	has_ref = any(t['alignment']['ref'] for t in transcript)

	audio_path = transcript[0]['audio_path']
	signal, sample_rate = audio.read_audio(audio_path, sample_rate = sample_rate, mono = mono, dtype = torch.int16, normalize = False)
	
	fmt_link = lambda word, channel, begin, end, i = '', j = '': f'<a onclick="return play({channel},{begin},{end})" title="#{channel}: {begin:.04f} - {end:.04f} | {i} - {j}" href="#" target="_blank">' + (word if isinstance(word, str) else f'{begin:.02f}' if word == 0 else f'{end:.02f}' if word == 1 else f'{end - begin:.02f}') + '</a>'
	fmt_words = lambda rh: ' '.join(fmt_link(**w) for w in rh)
	fmt_begin_end = 'data-begin="{begin}" data-end="{end}"'.format

	html = open(html_path, 'w')
	html.write('<html><head><meta charset="UTF-8"><style>.m0{margin:0px} .top{vertical-align:top} .channel0{background-color:violet} .channel1{background-color:lightblue;' + ('display:none' if len(signal) == 1 else '') + '} .reference{opacity:0.4} .channel{margin:0px}</style></head><body>')
	html.write(f'<div style="overflow:auto"><h4 style="float:left">{os.path.basename(audio_path)}</h4><h5 style="float:right">0.000000</h5></div>')
	html.writelines(f'<figure class="m0"><figcaption>channel #{c}:</figcaption><audio ontimeupdate="ontimeupdate_(event)" onpause="onpause_(event)" id="audio{c}" style="width:100%" controls src="data:audio/wav;base64,{base64.b64encode(wav).decode()}"></audio></figure>' for c, wav in enumerate(audio.write_audio(io.BytesIO(), signal[channel], sample_rate).getvalue() for channel in ([0, 1] if len(signal) == 2 else []) + [...]))
	html.write(f'<pre class="channel"><h3 class="channel0 channel">hyp #0:<span class="subtitle"></span></h3></pre><pre class="channel"><h3 class="channel0 reference channel">ref #0:<span class="subtitle"></span></h3></pre><pre class="channel" style="margin-top: 10px"><h3 class="channel1 channel">hyp #1:<span class="subtitle"></span></h3></pre><pre class="channel"><h3 class="channel1 reference channel">ref #1:<span class="subtitle"></span></h3></pre><hr/><table style="width:100%">')
	html.write('<tr>' + ('<th>begin</th><th>end</th><th>dur</th><th style="width:50%">hyp</th>' if has_hyp else '') + ('<th style="width:50%">ref</th><th>begin</th><th>end</th><th>dur</th>' if has_ref else '') + '</tr>')
	html.writelines(f'<tr class="channel{c}">'+ (f'<td class="top">{fmt_link(0, **transcripts.summary(hyp, ij = True))}</td><td class="top">{fmt_link(1, **transcripts.summary(hyp, ij = True))}</td><td class="top">{fmt_link(2, **transcripts.summary(hyp, ij = True))}</td><td class="top hyp" data-channel="{c}" {fmt_begin_end(**transcripts.summary(hyp, ij = True))}>{fmt_words(hyp)}<template>{colorize_alignment(t["words"], tag = "", hyp = True)}</template></td>' if has_hyp else '') + (f'<td class="top reference ref" data-channel="{c}" {fmt_begin_end(**transcripts.summary(ref, ij = True))}>{fmt_words(ref)}<template>{colorize_alignment(t["words"], tag = "", ref = True)}</template></td><td class="top">{fmt_link(0, **transcripts.summary(ref, ij = True))}</td><td class="top">{fmt_link(1, **transcripts.summary(ref, ij = True))}</td><td class="top">{fmt_link(2, **transcripts.summary(ref, ij = True))}</td>' if ref else ('<td></td>' * 4 if has_ref else '')) + f'</tr>' for t in transcripts.sort(transcript) for c, ref, hyp in [(t['channel'], t['alignment']['ref'], t['alignment']['hyp'])] )
	html.write('''</tbody></table><script>
		function play(channel, begin, end)
		{
			Array.from(document.querySelectorAll('audio')).map(audio => audio.pause());
			const audio = document.querySelector(`#audio${channel}`);
			audio.currentTime = begin;
			audio.dataset.endTime = end;
			audio.play();
			return false;
		}
		
		function subtitle(segments, time, channel)
		{
			return (segments.find(([rh, c, b, e]) => c == channel && b <= time && time <= e ) || ['', channel, null, null])[0];
		}

		function onpause_(evt)
		{
			evt.target.dataset.endTime = null;
		}

		function ontimeupdate_(evt)
		{
			const time = evt.target.currentTime, endtime = evt.target.dataset.endTime;
			if(endtime && time > endtime)
				return evt.target.pause();

			document.querySelector('h5').innerText = time.toString();
			const [spanhyp0, spanref0, spanhyp1, spanref1] = document.querySelectorAll('span.subtitle');
			[spanhyp0.innerHTML, spanref0.innerHTML, spanhyp1.innerHTML, spanref1.innerHTML] = [subtitle(hyp_segments, time, 0), subtitle(ref_segments, time, 0), subtitle(hyp_segments, time, 1), subtitle(ref_segments, time, 1)];
		}

		const make_segment = td => [td.querySelector('template').innerHTML, td.dataset.channel, td.dataset.begin, td.dataset.end];
		const hyp_segments = Array.from(document.querySelectorAll('.hyp')).map(make_segment), ref_segments = Array.from(document.querySelectorAll('.ref')).map(make_segment);
	</script></body></html>''')
	return html_path

def logits(logits, audio_file_name, MAX_ENTROPY = 1.0):
	good_audio_file_name = set(map(str.strip, open(audio_file_name)) if audio_file_name is not None else [])
	labels = dataset.Labels(ru)
	tick_params = lambda ax, labelsize = 2.5, length = 0, **kwargs: ax.tick_params(axis = 'both', which = 'both', labelsize = labelsize, length = length, **kwargs) or [ax.set_linewidth(0) for ax in ax.spines.values()]
	logits_path = logits + '.html'
	html = open(logits_path, 'w')
	html.write('''<html><meta charset="utf-8"/><body><script>
		function onclick_(evt)
		{
			const img = evt.target;
			const dim = img.getBoundingClientRect();
			const t = (evt.clientX - dim.left) / dim.width;
			const audio = img.nextSibling;
			audio.currentTime = t * audio.duration;
			audio.play();
		}
	</script>''')
	for r in torch.load(logits)[:5]:
		logits = r['logits']
		if good_audio_file_name and r['audio_file_name'] not in good_audio_file_name:
			continue
		
		ref_aligned, hyp_aligned = r['alignment']['ref'], r['alignment']['hyp']
		
		log_probs = F.log_softmax(logits, dim = 0)
		entropy = models.entropy(log_probs, dim = 0, sum = False)
		log_probs_ = F.log_softmax(logits[:-1], dim = 0)
		entropy_ = models.entropy(log_probs_, dim = 0, sum = False)
		margin = models.margin(log_probs, dim = 0)
		#energy = features.exp().sum(dim = 0)[::2]

		alignment = ctc.ctc_loss(log_probs.unsqueeze(0).permute(2, 0, 1), r['y'].unsqueeze(0).long(), torch.LongTensor([log_probs.shape[-1]]), torch.LongTensor([len(r['y'])]), blank = len(log_probs) - 1, alignment = True)[0, :, :len(r['y'])]
		
		plt.figure(figsize = (6, 2))
		
		prob_top1, prob_top2 = log_probs.exp().topk(2, dim = 0).values
		plt.hlines(1.0, 0, entropy.shape[-1] - 1, linewidth = 0.2)
		artist_prob_top1, = plt.plot(prob_top1, 'b', linewidth = 0.3)
		artist_prob_top2, = plt.plot(prob_top2, 'g', linewidth = 0.3)
		artist_entropy, = plt.plot(entropy, 'r', linewidth = 0.3)
		artist_entropy_, = plt.plot(entropy_, 'yellow', linewidth = 0.3)
		plt.legend([artist_entropy, artist_entropy_, artist_prob_top1, artist_prob_top2], ['entropy', 'entropy, no blank', 'top1 prob', 'top2 prob'], loc = 1, fontsize = 'xx-small', frameon = False)
		bad = (entropy > MAX_ENTROPY).tolist()
		#runs = []
		#for i, b in enumerate(bad):
		#	if b:
		#		if not runs or not bad[i - 1]:
		#			runs.append([i, i])
		#		else:
		#			runs[-1][1] += 1
		#for begin, end in runs:
		#	plt.axvspan(begin, end, color='red', alpha=0.2)

		plt.ylim(0, 3.0)
		plt.xlim(0, entropy.shape[-1] - 1)
		xlabels = list(map('\n'.join, zip(*labels.split_candidates(labels.decode(log_probs.topk(5, dim = 0).indices.tolist(), replace_blank = '.', replace_space = '_', replace_repeat = False)))))
		#xlabels_ = labels.decode(log_probs.argmax(dim = 0).tolist(), blank = '.', space = '_', replace2 = False)
		plt.xticks(torch.arange(entropy.shape[-1]), xlabels, fontfamily = 'monospace')
		tick_params(plt.gca())

		ax = plt.gca().secondary_xaxis('top')
		ref, ref_ = labels.decode(r['y']), alignment.argmax(dim = 0)
		ax.set_xticklabels(ref)
		ax.set_xticks(ref_)
		tick_params(ax, colors = 'red')

		k = 0
		for i, c in enumerate(ref + ' '):
			if c == ' ':
				plt.axvspan(ref_[k] - 1, ref_[i - 1] + 1, facecolor = 'gray', alpha = 0.2)
				k = i + 1

		plt.subplots_adjust(left = 0, right = 1, bottom = 0.12, top = 0.95)

		buf = io.BytesIO()
		plt.savefig(buf, format = 'jpg', dpi = 600)
		plt.close()
		
		html.write('<h4>{audio_file_name} | cer: {cer:.02f}</h4>'.format(**r))
		html.write(colorize_alignment(r['words']))
		html.write('<img onclick="onclick_(event)" style="width:100%" src="data:image/jpeg;base64,{encoded}"></img>'.format(encoded = base64.b64encode(buf.getvalue()).decode()))	
		html.write('<audio style="width:100%" controls src="data:audio/wav;base64,{encoded}"></audio><hr/>'.format(encoded = base64.b64encode(open(r['audio_path'], 'rb').read()).decode()))
	html.write('</body></html>')
	print('\n', logits_path)

def errors(ours, theirs = None, audio_file_name = None, audio = False, output_file_name = None):
	good_audio_file_name = set(map(str.strip, open(audio_file_name)) if audio_file_name is not None else [])
	read_transcript = lambda path: list(filter(lambda r: not good_audio_file_name or r['audio_file_name'] in good_audio_file_name, json.load(open(path)))) if path is not None else []
	ours_, theirs_ = read_transcript(ours), {r['audio_file_name'] : r for r in read_transcript(theirs)}
	# https://stackoverflow.com/questions/14267781/sorting-html-table-with-javascript
	output_file_name = output_file_name or (ours + (audio_file_name.split('subset')[-1] if audio_file_name else '') + '.html')
	open(output_file_name , 'w').write('<html><meta charset="utf-8"><style>.br{border-right:2px black solid} td {border-top: 1px solid black} .nowrap{white-space:nowrap}</style><body><table style="border-collapse:collapse; width: 100%"><tr><th>audio</th><th></th><th>cer</th><th>mer</th><th></th></tr>' + '\n'.join(f'<tr><td>' + (f'<audio controls src="data:audio/wav;base64,{base64.b64encode(open(r["audio_path"], "rb").read()).decode()}"></audio>' if audio and i == 0 else '') + f'<div class="nowrap">{r["audio_file_name"] if i == 0 else ""}</div></td>' + f'<td>{ours if i == 0 else theirs}</td><td>{r_["cer"]:.02%}</td><td class="br">{r_["mer"]:.02%}</td><td>{colorize_alignment(r_["words"])}</td>' + '</tr>' for r in ours_ for i, r_ in enumerate(filter(None, [r, theirs_.get(r['audio_file_name'])]))) + '</table></body></html>')
	print(output_file_name)

def cer(experiments_dir, experiment_id, entropy, loss, cer10, cer15, cer20, cer30, cer40, cer50, per, wer, json_, bpe, der):
	labels = dataset.Labels(ru)
	if experiment_id.endswith('.json'):
		reftra = json.load(open(experiment_id))
		for reftra_ in reftra:
			hyp = labels.postprocess_transcript(labels.normalize_text(reftra_.get('hyp', ref_tra_.get('transcript'))     ))
			ref = labels.postprocess_transcript(labels.normalize_text(reftra_.get('ref', ref_tra_.get('reference'))      ))
			reftra_['cer'] = metrics.cer(hyp, ref)
			reftra_['wer'] = metrics.wer(hyp, ref)

		cer_, wer_ = [torch.tensor([r[k] for r in reftra]) for k in ['cer', 'wer']]
		cer_avg, wer_avg = float(cer_.mean()), float(wer_.mean())
		print(f'CER: {cer_avg:.02f} | WER: {wer_avg:.02f}')
		loss_ = torch.tensor([r.get('loss', 0) for r in reftra])
		loss_ = loss_[~(torch.isnan(loss_) | torch.isinf(loss_))]
		#min, max, steps = 0.0, 2.0, 20
		#bins = torch.linspace(min, max, steps = steps)
		#hist = torch.histc(loss_, bins = steps, min = min, max = max)
		#for b, h in zip(bins.tolist(), hist.tolist()):
		#	print(f'{b:.02f}\t{h:.0f}')

		plt.figure(figsize = (8, 4))
		plt.suptitle(os.path.basename(experiment_id))
		plt.subplot(211)
		plt.title('cer PDF')
		#plt.hist(cer_, range = (0.0, 1.2), bins = 20, density = True)
		seaborn.distplot(cer_, bins = 20, hist = True)
		plt.xlim(0, 1)
		plt.subplot(212)
		plt.title('cer CDF')
		plt.hist(cer_, bins = 20, density = True, cumulative = True)
		plt.xlim(0, 1)
		plt.xticks(torch.arange(0, 1.01, 0.1))
		plt.grid(True)

		#plt.subplot(223)
		#plt.title('loss PDF')
		#plt.hist(loss_, range = (0.0, 2.0), bins = 20, density = True)
		#seaborn.distplot(loss_, bins = 20, hist = True)
		#plt.xlim(0, 3)
		#plt.subplot(224)
		#plt.title('loss CDF')
		#plt.hist(loss_, bins = 20, density = True, cumulative = True)
		#plt.grid(True)
		plt.subplots_adjust(hspace = 0.4)
		plt.savefig(experiment_id + '.png', dpi = 150)
		return

	res = collections.defaultdict(list)
	experiment_dir = os.path.join(experiments_dir, experiment_id)
	for f in sorted(glob.glob(os.path.join(experiment_dir, f'transcripts_*.json'))):
		eidx = f.find('epoch')
		iteration = f[eidx:].replace('.json', '')
		val_dataset_name = f[f.find('transcripts_') + len('transcripts_'):eidx]
		checkpoint = os.path.join(experiment_dir, 'checkpoint_' + f[eidx:].replace('.json', '.pt')) if not json_ else f
		val = torch.tensor([j['wer' if wer else 'entropy' if entropy else 'loss' if loss else 'per' if per else 'der' if der else 'cer'] for j in json.load(open(f)) if j['labels'].startswith(bpe)] or [0.0])
		val = val[~(torch.isnan(val) | torch.isinf(val))]

		if cer10 or cer20 or cer30 or cer40 or cer50:
			val = (val < 0.1 * [False, cer10, cer20, cer30, cer40, cer50].index(True)).float()
		if cer15:
			val = (val < 0.15).float()

		res[iteration].append((val_dataset_name, float(val.mean()), checkpoint))
	val_dataset_names = sorted(set(val_dataset_name for r in res.values() for val_dataset_name, cer, checkpoint in r))
	print('iteration\t' + '\t'.join(val_dataset_names))
	for iteration, r in res.items():
		cers = {val_dataset_name : f'{cer:.04f}' for val_dataset_name, cer, checkpoint in r}
		print(f'{iteration}\t' + '\t'.join(cers.get(val_dataset_name, '') for val_dataset_name in val_dataset_names) + f'\t{r[-1][-1]}')

def words(train_data_path, val_data_path):
	train_cnt = collections.Counter(w for l in open(train_data_path) for w in l.split(',')[1].split())
	val_cnt = collections.Counter(w for l in open(val_data_path) for w in l.split(',')[1].split())

	for w, c1 in val_cnt.most_common():
		c2 = train_cnt[w]
		if c1 > 1 and c2 < 1000:
			print(w, c1, c2)

def meanstd(logits):
	cov = lambda m: m @ m.t()
	L = torch.load(logits, map_location = 'cpu')

	batch_m = [b.mean(dim = -1) for b in L['features']]
	batch_mean = torch.stack(batch_m).mean(dim = 0)
	batch_s = [b.std(dim = -1) for b in L['features']]
	batch_std = torch.stack(batch_s).mean(dim = 0)
	batch_c = [cov(b - m.unsqueeze(-1)) for b, m in zip(L['features'], batch_m)]
	batch_cov = torch.stack(batch_c).mean(dim = 0)

	conv1_m = [b.mean(dim = -1) for b in L['conv1']]
	conv1_mean = torch.stack(conv1_m).mean(dim = 0)
	conv1_s = [b.std(dim = -1) for b in L['conv1']]
	conv1_std = torch.stack(conv1_s).mean(dim = 0)
	conv1_c = [cov(b - m.unsqueeze(-1)) for b, m in zip(L['conv1'], conv1_m)]
	conv1_cov = torch.stack(conv1_c).mean(dim = 0)
	
	plt.subplot(231)
	plt.imshow(batch_mean[:10].unsqueeze(0), origin = 'lower', aspect = 'auto')
	plt.subplot(232)
	plt.imshow(batch_std[:10].unsqueeze(0), origin = 'lower', aspect = 'auto')
	plt.subplot(233)
	plt.imshow(batch_cov[:20, :20], origin = 'lower', aspect = 'auto')

	plt.subplot(234)
	plt.imshow(conv1_mean.unsqueeze(0), origin = 'lower', aspect = 'auto')
	plt.subplot(235)
	plt.imshow(conv1_std.unsqueeze(0), origin = 'lower', aspect = 'auto')
	plt.subplot(236)
	plt.imshow(conv1_cov, origin = 'lower', aspect = 'auto')
	plt.subplots_adjust(top = 0.99, bottom=0.01, hspace=0.8, wspace=0.4)
	plt.savefig(logits + '.jpg', dpi = 150)

def histc_vega(tensor, min, max, bins):
	bins = torch.linspace(min, max, bins)
	hist = tensor.histc(min = bins.min(), max = bins.max(), bins = len(bins)).int()
	return altair.Chart(altair.Data(values = [dict(x = b, y = v) for b, v in zip(bins.tolist(), hist.tolist())])).mark_bar().encode(x = altair.X('x:Q'), y = altair.Y('y:Q')).to_dict()

def colorize_alignment(r, ref = None, hyp = None, tag = '<pre>'):
	span = lambda word, t = None: '<span style="{style}">{word}</span>'.format(word = word, style = 'background-color:' + dict(ok = 'green', missing = 'red', typo_easy = 'lightgreen', typo_hard = 'pink')[t] if t is not None else '')
	
	hyp_ = ' '.join(span(w['ref'], w['type'] if w['type'] == 'ok' else None) for w in r)
	ref_ = ' '.join(span(w['hyp'], w['type']) for w in r)
	contents = hyp_ if hyp is True else ref_ if ref is True else f'ref: {ref_}\nhyp: {hyp_}'
	return tag + contents + tag.replace('<', '</')

if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	subparsers = parser.add_subparsers()

	cmd = subparsers.add_parser('transcript')
	cmd.add_argument('--transcript', '-i')
	cmd.add_argument('--mono', action = 'store_true')
	cmd.add_argument('--sample-rate', type = int, default = 8_000)
	cmd.add_argument('--html-path', '-o')
	cmd.set_defaults(func = transcript)

	cmd = subparsers.add_parser('errors')
	cmd.add_argument('ours', default = 'data/transcripts.json')
	cmd.add_argument('--theirs')
	cmd.add_argument('--audio-file-name')
	cmd.add_argument('--audio', action = 'store_true')
	cmd.add_argument('--output-file-name', '-o')
	cmd.set_defaults(func = errors)

	cmd = subparsers.add_parser('meanstd')
	cmd.add_argument('--logits', default = 'data/logits.pt')
	cmd.set_defaults(func = meanstd)

	cmd = subparsers.add_parser('cer')
	cmd.add_argument('experiment_id')
	cmd.add_argument('--experiments-dir', default = 'data/experiments')
	cmd.add_argument('--entropy', action = 'store_true')
	cmd.add_argument('--loss', action = 'store_true')
	cmd.add_argument('--per', action = 'store_true')
	cmd.add_argument('--wer', action = 'store_true')
	cmd.add_argument('--cer10', action = 'store_true')
	cmd.add_argument('--cer15', action = 'store_true')
	cmd.add_argument('--cer20', action = 'store_true')
	cmd.add_argument('--cer30', action = 'store_true')
	cmd.add_argument('--cer40', action = 'store_true')
	cmd.add_argument('--cer50', action = 'store_true')
	cmd.add_argument('--json', dest = "json_", action = 'store_true')
	cmd.add_argument('--bpe', default = 'char')
	cmd.add_argument('--der', action = 'store_true')
	cmd.set_defaults(func = cer)

	cmd = subparsers.add_parser('words')
	cmd.add_argument('train_data_path')
	cmd.add_argument('val_data_path')
	cmd.set_defaults(func = words)

	cmd = subparsers.add_parser('logits')
	cmd.add_argument('logits')
	cmd.add_argument('--audio-file-name')
	cmd.set_defaults(func = logits)

	args = vars(parser.parse_args())
	func = args.pop('func')
	func(**args)
