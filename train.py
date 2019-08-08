import os
import gc
import json
import time
import argparse
import importlib
import torch
import torch.utils.data
import torch.utils.tensorboard
import torch.nn as nn
import torch.nn.functional as F
import dataset
import transforms
import decoders
import models
import optimizers
try:
	import apex
except:
	pass

def traintest(args):
	print(args)
	args.id = args.id.format(model = args.model, train_batch_size = args.train_batch_size, optimizer = args.optimizer, lr = args.lr, weight_decay = args.weight_decay, time = time.strftime('%Y-%m-%d_%H-%M-%S'), noise_level = 0 if not args.noise_data_path else args.noise_level if isinstance(args.noise_level, float) else '-'.join(map(str, args.noise_level)), name = args.name).replace('e-0', 'e-').rstrip('_')
	if args.dry:
		print()
		print('Experiment id:', args.id)
		print()
		return
	args.experiment_dir = args.experiment_dir.format(experiments_dir = args.experiments_dir, id = args.id)
	os.makedirs(args.experiment_dir, exist_ok = True)

	for set_random_seed in [torch.manual_seed] + ([torch.cuda.manual_seed_all] if args.device == 'cuda' else []):
		set_random_seed(args.seed)
	tensorboard = torch.utils.tensorboard.SummaryWriter(os.path.join(args.experiment_dir, 'tensorboard'))

	lang = importlib.import_module(args.lang)
	labels = dataset.Labels(lang.LABELS, preprocess_text = lang.preprocess_text, preprocess_word = lang.preprocess_word)
	val_data_loaders = {os.path.basename(val_data_path) : torch.utils.data.DataLoader(dataset.SpectrogramDataset(val_data_path, sample_rate = args.sample_rate, window_size = args.window_size, window_stride = args.window_stride, window = args.window, labels = labels, num_input_features = args.num_input_features, noise_data_path = args.noise_data_path if not args.train_data_path else None, noise_level = args.noise_level), num_workers = args.num_workers, collate_fn = dataset.collate_fn, pin_memory = True, shuffle = False, batch_size = args.val_batch_size) for val_data_path in args.val_data_path}
	model = getattr(models, args.model)(num_classes = len(labels.char_labels), num_input_features = args.num_input_features)
	criterion = nn.CTCLoss(blank = labels.chr2idx(dataset.Labels.epsilon), reduction = 'sum').to(args.device)
	if args.train_data_path:
		train_dataset = dataset.SpectrogramDataset(args.train_data_path, sample_rate = args.sample_rate, window_size = args.window_size, window_stride = args.window_stride, window = args.window, labels = labels, num_input_features = args.num_input_features, transform = transforms.SpecAugment() if args.augment else None, noise_data_path = args.noise_data_path, noise_level = args.noise_level)
		train_sampler = dataset.BucketingSampler(train_dataset, batch_size=args.train_batch_size)
		train_data_loader = torch.utils.data.DataLoader(train_dataset, num_workers = args.num_workers, collate_fn = dataset.collate_fn, pin_memory = True, batch_sampler = train_sampler)
		optimizer = torch.optim.SGD(model.parameters(), lr = args.lr, momentum = args.momentum, weight_decay = args.weight_decay, nesterov = args.nesterov) if args.optimizer == 'SGD' else torch.optim.AdamW(model.parameters(), lr = args.lr, betas = args.betas, weight_decay = args.weight_decay) if args.optimizer == 'AdamW' else None
		if args.fp16:
			model, optimizer = apex.amp.initialize(model, optimizer, opt_level = args.fp16, keep_batchnorm_fp32 = args.fp16_keep_batchnorm_fp32)
		scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, gamma = args.decay_gamma, milestones = args.decay_milestones) if args.scheduler == 'MultiStepLR' else PolynomialDecayLR(optimizer, power = args.decay_power, decay_steps = len(train_data_loader) * args.decay_epochs, end_learning_rate = args.decay_lr_end) if args.scheduler == 'PolynomialDecayLR' else torch.optim.lr_scheduler.StepLR(optimizer, step_size = args.decay_step_size, gamma = args.decay_gamma) if args.scheduler == 'StepLR' else torch.optim.lr_scheduler.LambdaLR(optimizer, lambda epoch: args.lr)
	else:
		optimizer, train_sampler = None, None
	if args.checkpoint:
		models.load_checkpoint(args.checkpoint, model, optimizer, train_sampler)
	model = torch.nn.DataParallel(model).to(args.device)
	decoder = decoders.GreedyDecoder(labels.char_labels)

	def evaluate_model(epoch = None, batch_idx = None, iteration = None):
		training = epoch is not None and batch_idx is not None and iteration is not None
		model.eval()
		with torch.no_grad():
			for val_dataset_name, val_data_loader in val_data_loaders.items():
				logits_, ref_tra_, cer_, wer_, loss_ = [], [], [], [], []
				for i, (inputs, targets, filenames, input_percentages, target_sizes) in enumerate(val_data_loader):
					input_lengths = (input_percentages.cpu() * inputs.shape[-1]).int()
					logits = model(inputs.to(args.device), input_lengths)
					output_lengths = models.compute_output_lengths(model, input_lengths)
					loss = criterion(F.log_softmax(logits.permute(2, 0, 1), dim = -1), targets.to(args.device), output_lengths.to(args.device), target_sizes.to(args.device)) / len(inputs)
					loss_.append(float(loss))
					decoded_output, _ = decoder.decode(F.softmax(logits, dim = 1).permute(0, 2, 1), output_lengths)
					target_strings = decoder.convert_to_strings(dataset.unpack_targets(targets, target_sizes))
					for k in range(len(target_strings)):
						transcript, reference = decoded_output[k][0], target_strings[k][0]
						wer, cer, wer_ref, cer_ref = dataset.get_cer_wer(decoder, transcript, reference)
						if args.verbose:
							print(val_dataset_name, 'REF: ', reference)
							print(val_dataset_name, 'HYP: ', transcript)
							print()
						wer, cer = wer / wer_ref,  cer / cer_ref
						cer_.append(cer)
						wer_.append(wer)
						ref_tra_.append(dict(reference = reference, transcript = transcript, filename = filenames[k], cer = cer, wer = wer))
						logits_.extend(logits)
				cer_avg = float(torch.tensor(cer_).mean())
				wer_avg = float(torch.tensor(wer_).mean())
				loss_avg = float(torch.tensor(loss_).mean())
				print(f'{args.id} {val_dataset_name} | epoch {epoch} iter {iteration} | Loss: {loss_avg:.02f} | WER:  {wer_avg:.02%} CER: {cer_avg:.02%}')
				print()
				with open(os.path.join(args.experiment_dir, f'transcripts_{val_dataset_name}_epoch{epoch:02d}_iter{iteration:07d}.json') if training else args.transcripts.format(val_dataset_name = val_dataset_name), 'w') as f:
					json.dump(ref_tra_, f, ensure_ascii = False, indent = 2, sort_keys = True)
				torch.save(dict(logits = logits_, ref_tra = ref_tra_), os.path.join(args.experiment_dir, f'logits_{val_dataset_name}_epoch{epoch:02d}_iter{iteration:07d}.pt') if training else args.logits.format(val_dataset_name = val_dataset_name))
				tensorboard.add_scalars(args.id + '_' + val_dataset_name, dict(wer_avg = wer_avg, cer_avg = cer_avg, loss_avg = loss_avg), iteration) if training else None
		model.train()
		models.save_checkpoint(os.path.join(args.experiment_dir, f'checkpoint_epoch{epoch:02d}_iter{iteration:07d}.pt'), model.module, optimizer, train_sampler, epoch, batch_idx) if training else None

	if not args.train_data_path:
		return evaluate_model()

	with open(os.path.join(args.experiment_dir, args.args), 'w') as f:
		json.dump(vars(args), f, sort_keys = True, ensure_ascii = False, indent = 2)

	tic = time.time()
	iteration = 0
	loss_avg, time_ms_avg = 0.0, 0.0
	moving_avg = lambda avg, x, max = 0, K = 50: (1. / K) * min(x, max) + (1 - 1./K) * avg
	for epoch in range(args.epochs if args.train_data_path else 0):
		model.train()
		for batch_idx, (inputs, targets, filenames, input_percentages, target_sizes) in enumerate(train_data_loader):
			toc = time.time()
			input_lengths = (input_percentages.cpu() * inputs.shape[-1]).int()
			logits = model(inputs.to(args.device), input_lengths)
			output_lengths = models.compute_output_lengths(model, input_lengths)
			loss = criterion(F.log_softmax(logits.permute(2, 0, 1), dim = -1), targets.to(args.device), output_lengths.to(args.device), target_sizes.to(args.device)) / len(inputs)
			if not (torch.isinf(loss) or torch.isnan(loss)):
				optimizer.zero_grad()
				if args.fp16:
					with apex.amp.scale_loss(loss, optimizer) as scaled_loss:
						scaled_loss.backward()
				else:
					loss.backward()

				torch.nn.utils.clip_grad_norm_(apex.amp.master_params(optimizer) if args.fp16 else model.parameters(), args.max_norm)
				optimizer.step()
				loss_avg = moving_avg(loss_avg, float(loss), max = 1000)

			time_ms_data, time_ms_model = (toc - tic) * 1000, (time.time() - toc) * 1000
			time_ms_avg = moving_avg(time_ms_avg, time_ms_model, max = 10000)
			print(f'{args.id} | epoch: {epoch:02d} iter: [{batch_idx: >6d} / {len(train_data_loader)} {iteration: >9d}] loss: {float(loss): 7.2f} <{loss_avg: 7.2f}> time: {time_ms_model:8.0f} <{time_ms_avg:4.0f}> ms (data {time_ms_data:.2f} ms)  | lr: {scheduler.get_lr()[0]:.0e}')
			tic = time.time()
			scheduler.step()
			iteration += 1

			if args.val_iteration_interval is not None and iteration > 0 and iteration % args.val_iteration_interval == 0:
				evaluate_model(epoch, batch_idx, iteration)

			if iteration % args.log_iteration_interval == 0:
				tensorboard.add_scalars(args.id + '_' + os.path.basename(args.train_data_path), dict(loss_avg = loss_avg), iteration)

		evaluate_model(epoch, batch_idx, iteration)

if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--optimizer', choices = ['SGD', 'AdamW'], default = 'SGD')
	parser.add_argument('--max-norm', type = float, default = 100)
	parser.add_argument('--lr', type = float, default = 5e-3)
	parser.add_argument('--weight-decay', type = float, default = 1e-5)
	parser.add_argument('--momentum', type = float, default = 0.9)
	parser.add_argument('--nesterov', action = 'store_true')
	parser.add_argument('--betas', nargs = '*', type = float, default = (0.9, 0.999))
	parser.add_argument('--scheduler', choices = ['MultiStepLR', 'PolynomialDecayLR', 'StepLR'], default = None)
	parser.add_argument('--decay-gamma', type = float, default = 0.1)
	parser.add_argument('--decay-milestones', nargs = '*', type = int, default = [25000])
	parser.add_argument('--decay-power', type = float, default = 2.0)
	parser.add_argument('--decay-lr-end', type = float, default = 1e-5)
	parser.add_argument('--decay-epochs', type = int, default = 5)
	parser.add_argument('--decay-step-size', type = int, default = 10000)
	parser.add_argument('--fp16', choices = ['O0', 'O1', 'O2', 'O3'], default = None)
	parser.add_argument('--fp16-keep-batchnorm-fp32', default = None, action = 'store_true')
	parser.add_argument('--epochs', type = int, default = 20)
	parser.add_argument('--num-input-features', default = 64)
	parser.add_argument('--train-data-path')
	parser.add_argument('--val-data-path', nargs = '+')
	parser.add_argument('--noise-data-path')
	parser.add_argument('--noise-level', type = float, nargs = '*', default = [0.2, 0.6])
	parser.add_argument('--sample-rate', type = int, default = 16000)
	parser.add_argument('--window-size', type = float, default = 0.02)
	parser.add_argument('--window-stride', type = float, default = 0.01)
	parser.add_argument('--window', default = 'hann', choices = ['hann', 'hamming'])
	parser.add_argument('--num-workers', type = int, default = 10)
	parser.add_argument('--train-batch-size', type = int, default = 64)
	parser.add_argument('--val-batch-size', type = int, default = 64)
	parser.add_argument('--device', default = 'cuda', choices = ['cuda', 'cpu'])
	parser.add_argument('--checkpoint')
	parser.add_argument('--experiments-dir', default = 'data/experiments')
	parser.add_argument('--experiment-dir', default = '{experiments_dir}/{id}')
	parser.add_argument('--transcripts', default = 'data/transcripts_{val_dataset_name}.json')
	parser.add_argument('--logits', default = 'data/logits_{val_dataset_name}.pt')
	parser.add_argument('--args', default = 'args.json')
	parser.add_argument('--model', default = 'Wav2LetterRu')
	parser.add_argument('--seed', default = 1)
	parser.add_argument('--id', default = '{model}_{optimizer}_lr{lr:.0e}_wd{weight_decay:.0e}_bs{train_batch_size}_noise{noise_level}_{name}')
	parser.add_argument('--name', default = '')
	parser.add_argument('--dry', action = 'store_true')
	parser.add_argument('--lang', default = 'ru')
	parser.add_argument('--val-iteration-interval', type = int, default = None)
	parser.add_argument('--log-iteration-interval', type = int, default = 100)
	parser.add_argument('--augment', action = 'store_true')
	parser.add_argument('--verbose', action = 'store_true')
	traintest(parser.parse_args())
