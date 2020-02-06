#TODO:

#batch by vad

# if given transcript, always align
# support stereo inputs with or without vad, with or without alignment
# fix up timestamps by segment begin point
# figure out stft shift
# disable repeat deduplication
# fix 1.0400128364562988 11.540141105651855 cut
# mix vad output for filtering full output
# filter vad output

#segments = [[c, b_, e_, r, t_] for (b, e, c), d, r in zip(cutpoints, decoded, ref) for b_, e_, t_ in segment_transcript(labels, d, b, e, args.max_segment_seconds)] if args.vad is not False else [[c, b, e, r, labels.postprocess_transcript(labels.decode(d)[0])] for (b, e, c), d, r in zip(cutpoints, decoded, ref)]

import os
import io
import time
import json
import base64
import argparse
import webrtcvad
import importlib
import torch
import torch.nn.functional as F
import dataset
import models
import metrics
import decoders
import ctc
import vad

def resegment(c, r, h, rh, max_segment_seconds):
	rh_ = lambda rh, i, w, first, last: [(k, u) for k, u in enumerate(rh) if (first or i is None or u['begin'] >= rh[i]['end']) and (last or u['end'] <= w['end'])]
	rhk = [r, h].index(rh)
	i = [None, None]
	for j, w in enumerate(rh):
		if j == len(rh) - 1 or w['end'] - rh[i[rhk] or 0]['end'] > max_segment_seconds:
			first_last = dict(first = i[rhk] is None, last = j == len(rh) - 1)
			r_ = rh_(r, i[0], rh[j], **first_last); rk, r_ = zip(*r_) if r_ else ([i[0]], [])
			h_ = rh_(h, i[1], rh[j], **first_last); hk, h_ = zip(*h_) if h_ else ([i[1]], [])
			i = (rk[-1], hk[-1])
			yield [c, list(r_), list(h_)]

@torch.no_grad()
def main(args):
	os.makedirs(args.output_path, exist_ok = True)
	
	checkpoint = torch.load(args.checkpoint, map_location = 'cpu')
	args.sample_rate, args.window_size, args.window_stride, args.window, args.num_input_features = map(checkpoint['args'].get, ['sample_rate', 'window_size', 'window_stride', 'window', 'num_input_features'])
	batch_collater = dataset.BatchCollater(args.batch_time_padding_multiple)
	frontend = models.LogFilterBankFrontend(args.num_input_features, args.sample_rate, args.window_size, args.window_stride, args.window)
	labels = dataset.Labels(importlib.import_module(checkpoint['args']['lang']), name = 'char')
	model = getattr(models, checkpoint['args']['model'])(args.num_input_features, [len(labels)], frontend = frontend, dict = lambda logits, log_probs, output_lengths, **kwargs: (logits[0], output_lengths[0]))
	model.load_state_dict(checkpoint['model_state_dict'], strict = False) #TODO: figure out problems with frontend
	model = model.to(args.device)
	model.eval()
	model.fuse_conv_bn_eval()
	#model = models.data_parallel(model)

	decoder = decoders.GreedyDecoder() if args.decoder == 'GreedyDecoder' else decoders.BeamSearchDecoder(labels, lm_path = args.lm, beam_width = args.beam_width, beam_alpha = args.beam_alpha, beam_beta = args.beam_beta, num_workers = args.num_workers, topk = args.decoder_topk)
	
	example = lambda audio_path, signal, b, e, sample_rate, channel, ref_normalized, targets: (os.path.basename(os.path.dirname(audio_path)), audio_path, ref_normalized, signal[None, int(b * sample_rate):int(e * sample_rate), channel], targets)	

	audio_paths = [args.data_path] if os.path.isfile(args.data_path) else [os.path.join(args.data_path, f) for f in os.listdir(args.data_path) if any(map(f.endswith, args.ext))]
	for audio_path in audio_paths[:1]:
		batch, cutpoints, audio = [], [], []
		ref_path, transcript_path = audio_path + '.txt', audio_path + '.json'

		#if os.path.exists(transcript_path) and args.align:
		#	signal_normalized, sample_rate = dataset.read_audio(audio_path, sample_rate = args.sample_rate, mono = False, normalize = True, dtype = torch.float32)
		#	transcript = json.load(open(transcript_path))
		#	for b, e, channel, ref in [map(r.get, ['begin', 'end', 'channel', 'ref']) for r in transcript]:
		#		cutpoints.append((b, e, channel))
		#		batch.append(example(audio_path, signal_normalized, b, e, sample_rate, channel, *labels.encode(ref)))
		#else:
		
		signal, sample_rate = dataset.read_audio(audio_path, sample_rate = args.sample_rate, mono = False, normalize = False, dtype = torch.int16)

		signal_normalized = models.normalize_signal(signal, dim = 0)
		ref = labels.postprocess_transcript2(labels.normalize_text(open(ref_path).read())) if os.path.exists(ref_path) else ''
		speech = []
		for channel, signal_ in enumerate(signal.t()):
			speech.append(vad.detect_speech(signal_, sample_rate, args.window_size, aggressiveness = args.vad))
			audio.append(dataset.write_audio(io.BytesIO(), sample_rate, signal_))
			b, e = 0, len(signal) / sample_rate
			cutpoints.append((b, e, channel))
			batch.append(example(audio_path, signal_normalized, b, e, sample_rate, channel, *labels.encode(ref)))

		tic = time.time()
		_, _, ref, x, xlen, y, ylen = batch_collater(batch)
		x, xlen, y, ylen = [t.to(args.device) for t in [x, xlen, y, ylen]]
		x, y, ylen = x.squeeze(1), y.squeeze(1), ylen.squeeze(1)
		log_probs, output_lengths = model(x, xlen)
	
		speech = torch.stack([s.reshape(-1, 160).min(dim = -1).values for s in speech])
		speech = F.pad(speech_, [0, log_probs.shape[-1] - speech.shape[-1]]).to(log_probs.device)
		log_probs.masked_fill_(models.silence_space_mask(log_probs, speech, space_idx = labels.space_idx, blank_idx = labels.blank_idx), float('-inf'))

		decoded = decoder.decode(log_probs, output_lengths)
		
		print(args.checkpoint, os.path.basename(audio_path))
		print('Time: audio {audio:.02f} sec | voice {voice:.02f} sec | processing {processing:.02f} sec'.format(audio = signal.numel() / sample_rate, voice = sum(e - b for b, e, c in cutpoints), processing = time.time() - tic))

		
		ts = torch.linspace(0, 1, steps = log_probs.shape[-1]) * (x.shape[-1] / sample_rate)
		
		begin_end = lambda rh: (min(w['begin'] for w in rh), max(w['end'] for w in rh)) if len(rh) > 0 else (0, 0) #TODO: add i, j
	
		#cut = lambda d, b, e, duration = len(signal) / sample_rate: d[int(len(d) * b / duration):(1 + int(len(d) * e / duration))]
		#segments = [[c, r, h] for c, r, h in [[c, [], labels.decode(cut(d, b, e), cut(ts, b, e), channel = c, replace_blank = True, replace_repeat = True, replace_space = False) ] for c, (cutpoints, d, r) in enumerate(zip(speech, decoded, ref)) for b, e in cutpoints] if h]
		
		segments = [[c, r, labels.decode(d, ts, channel = c, replace_blank = True, replace_repeat = True, replace_space = False)] for (b, e, c), d, r in zip(cutpoints, decoded, ref)]

		ref = ' '.join(ref)
		hyp = ' '.join(w['word'] for c, r, h in segments for w in h)
		open(os.path.join(args.output_path, os.path.basename(audio_path) + '.txt'), 'w').write(hyp)
		if args.verbose:
			print('HYP:', hyp)
		
		if ref and args.align:
			cer = metrics.cer(hyp, ref)#, edit_distance = metrics.levenshtein)
			print('Input time steps:', log_probs.shape[-1], '| Target time steps:', y.shape[-1])
			tic = time.time()
			alignment = ctc.ctc_loss_(log_probs.permute(2, 0, 1), y.long(), output_lengths, ylen, blank = labels.blank_idx, alignment = True)
			print('Alignment time: {:.02f} sec'.format(time.time() - tic))
			for i in range(len(y)):
				segments[i][-2] = labels.decode(y[i, :ylen[i]].tolist(), ts, alignment[i])
			print(f'CER: {cer:.02%}')
		else:
			for i in range(len(y)):
				segments[i][-2] = []
		
		segments = sum([list(resegment(*s, rh = s[-2] or s[-1], max_segment_seconds = args.max_segment_seconds)) for s in segments], [])
		segments = list(sorted(segments, key = lambda s: begin_end(s[-1] + s[-2]) + (s[0],)))

		html_path = os.path.join(args.output_path, os.path.basename(audio_path) + '.html')
		with open(html_path, 'w') as html:
			fmt_link = lambda word, channel, begin, end, i = '', j = '': f'<a onclick="return play({channel},{begin},{end})" title="#{channel}: {begin:.04f} - {end:.04f} | {i} - {j}" href="#" target="_blank">' + (word if isinstance(word, str) else f'{begin:.02f}' if word == 0 else f'{end:.02f}') + '</a>'
			fmt_words = lambda rh: rh if isinstance(rh, str) else ' '.join(fmt_link(**w) for w in rh) if len(rh) > 0 and isinstance(rh[0], dict) else ' '.join(rh)
			fmt_begin_end = 'data-begin="{}" data-end="{}"'.format

			html.write('<html><head><meta charset="UTF-8"><style>.m0{margin:0px} .top{vertical-align:top} .channel0{background-color:violet} .channel1{background-color:lightblue} .reference{opacity:0.4} .channel{margin:0px}</style></head><body>')
			html.write(f'<div style="overflow:auto"><h4 style="float:left">{os.path.basename(audio_path)}</h4><h5 style="float:right">0.000000</h5></div>')
	
			if len(audio) > 0:
				html.write('<figure class="m0"><figcaption>both channels</figcaption><audio style="width:100%" controls src="data:audio/wav;base64,{encoded}"></audio></figure>'.format(encoded = base64.b64encode(open(audio_path, 'rb').read()).decode()))
			
			html.write(''.join('<figure class="m0"><figcaption>channel #{c}:</figcaption><audio id="audio{c}" style="width:100%" controls src="data:audio/wav;base64,{encoded}"></audio></figure>'.format(c = c, encoded = base64.b64encode(buf.getvalue()).decode()) for c, buf in enumerate(audio)))
			html.write(f'<pre class="channel"><h3 class="channel0 channel">hyp #0:<span></span></h3></pre><pre class="channel"><h3 class="channel0 reference channel">ref #0:<span></span></h3></pre><pre class="channel" style="margin-top: 10px"><h3 class="channel1 channel">hyp #1:<span></span></h3></pre><pre class="channel"><h3 class="channel1 reference channel">ref #1:<span></span></h3></pre><hr/>')
			html.write('<table style="width:100%"><thead><th>begin</th><th>end</th><th style="width:50%">hyp</th><th style="width:50%">ref</th><th>begin</th><th>end</th></tr></thead><tbody>')
			html.write(''.join(f'<tr class="channel{c}"><td class="top">{fmt_link(0, c, *begin_end(h))}</td><td class="top">{fmt_link(1, c, *begin_end(h))}</td><td class="top hyp" data-channel="{c}" {fmt_begin_end(*begin_end(h))}>{fmt_words(h)}</td>' + (f'<td class="top reference ref" data-channel="{c}" {fmt_begin_end(*begin_end(r))}>{fmt_words(r)}</td><td class="top">{fmt_link(0,c, *begin_end(r))}</td><td class="top">{fmt_link(1, c, *begin_end(r))}</td>' if r else '<td></td>' * 3) + f'</tr>' for c, r, h in segments))
			html.write('''</tbody></table><script>
				function play(channel, begin, end)
				{
					Array.from(document.querySelectorAll('audio')).map(audio => audio.pause());
					const audio = document.querySelector(`#audio${channel}`);
					audio.currentTime = begin;
					audio.dataset.endTime = end;
					audio.play();
					return false;
				};

				Array.from(document.querySelectorAll('audio')).map(audio => 
				{
					audio.ontimeupdate = evt =>
					{
						const time = evt.target.currentTime, endtime = evt.target.dataset.endTime;
						if(time > endtime)
						{
							evt.target.pause();
							return;
						}

						document.querySelector('h5').innerText = time.toString();
						const find = (segments, time, channel) => (segments.find(([rh, c, b, e]) => c == channel && b <= time && time <= e) || ['', channel, null, null])[0];
						const [spanhyp0, spanref0, spanhyp1, spanref1] = document.querySelectorAll('span');
						[spanhyp0.innerText, spanref0.innerText, spanhyp1.innerText, spanref1.innerText] = [find(hyp_segments, time, 0), find(ref_segments, time, 0), find(hyp_segments, time, 1), find(ref_segments, time, 1)];
					};
				});

				const make_segment = td => [td.innerText, td.dataset.channel, td.dataset.begin, td.dataset.end];
				const hyp_segments = Array.from(document.querySelectorAll('.hyp')).map(make_segment), ref_segments = Array.from(document.querySelectorAll('.ref')).map(make_segment);
			</script></body></html>''')
		print(html_path)

if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--checkpoint', required = True)
	parser.add_argument('-i', '--data-path', required = True)
	parser.add_argument('-o', '--output-path', required = True)
	parser.add_argument('--device', default = 'cuda', choices = ['cpu', 'cuda'])
	parser.add_argument('--max-segment-seconds', type = float, default = 2)
	parser.add_argument('--num-workers', type = int, default = 32)
	parser.add_argument('--ext', default = ['wav', 'mp3'])
	parser.add_argument('--decoder', default = 'GreedyDecoder', choices = ['GreedyDecoder', 'BeamSearchDecoder'])
	parser.add_argument('--decoder-topk', type = int, default = 1)
	parser.add_argument('--beam-width', type = int, default = 5000)
	parser.add_argument('--beam-alpha', type = float, default = 0.3)
	parser.add_argument('--beam-beta', type = float, default = 1.0)
	parser.add_argument('--lm')
	parser.add_argument('--batch-time-padding-multiple', type = int, default = 128)
	parser.add_argument('--vad', type = int, choices = [0, 1, 2, 3], default = False, nargs = '?')
	parser.add_argument('--align', action = 'store_true')
	parser.add_argument('--verbose', action = 'store_true')
	parser.add_argument('--stereo', action = 'store_true')
	args = parser.parse_args()
	args.vad = args.vad if isinstance(args.vad, int) else 3
	
	main(args)
