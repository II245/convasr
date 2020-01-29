import torch
import torch.nn.functional as F

def ctc_loss_(log_probs, targets, input_lengths, target_lengths, blank : int = 0, reduction : str = 'none', alignment : bool = False):
	B = torch.arange(len(targets), device = input_lengths.device)
	targets_ = torch.cat([targets, targets[:, :1]], dim = -1)
	targets_ = torch.stack([torch.full_like(targets_, blank), targets_], dim = -1).flatten(start_dim = -2)
	diff_labels = torch.cat([torch.as_tensor([[False, False]], device = targets.device).expand(len(B), -1), targets_[:, 2:] != targets_[:, :-2]], dim = 1)
	zero = torch.finfo(log_probs.dtype).min	

	zero, zero_padding = torch.tensor(zero, device = log_probs.device, dtype = log_probs.dtype), 2
	padded_t = zero_padding + targets_.shape[-1]
	log_alpha = torch.full((len(B), padded_t), zero, device = log_probs.device, dtype = log_probs.dtype)
	log_alpha[:, zero_padding + 0] = log_probs[0, :, blank]
	log_alpha[:, zero_padding + 1] = log_probs[0, B, targets_[:, 1]]
	log_alpha_ = torch.zeros((len(log_probs), len(B), padded_t), device = log_probs.device, dtype = torch.uint8)
	for t in range(1, len(log_probs)):
		prev = torch.stack([log_alpha[:, 2:], log_alpha[:, 1:-1], torch.where(diff_labels, log_alpha[:, :-2], zero)])
		log_alpha[:, 2:] = log_probs[t].gather(-1, targets_) + prev.logsumexp(dim = 0)
		log_alpha_[t, :, 2:] = prev.argmax(dim = 0)

	l1l2 = log_alpha.gather(-1, torch.stack([zero_padding + target_lengths * 2 - 1, zero_padding + target_lengths * 2], dim = -1)) 
	loss = -torch.logsumexp(l1l2, dim = -1)
	if not alignment:
		return loss
	
	path = torch.zeros(len(log_probs), len(B), device = log_alpha.device, dtype = torch.long)
	path[input_lengths - 1, B] = zero_padding + target_lengths * 2 - 1 + l1l2.argmax(dim = -1)
	for t, indices in reversed(list(enumerate(path))[1:]):
		indices_ = torch.stack([(indices - 2) * diff_labels.gather(-1, (indices - zero_padding).clamp_(min = 0).unsqueeze(-1)).squeeze(-1), (indices - 1).clamp_(min = 0), indices], dim = -1)
		path[t - 1] += (indices - 2 + log_alpha_[t - 1].gather(-1, indices_).argmax(dim = -1)).clamp_(min = 0)

	return torch.zeros_like(log_alpha_).scatter_(-1, path.unsqueeze(-1), 1.0)[..., (zero_padding + 1)::2]

#@torch.jit.script
def ctc_loss(log_probs, targets, input_lengths, target_lengths, blank : int = 0, reduction : str = 'none', alignment : bool = False):
	B = torch.arange(len(targets), device = input_lengths.device)
	targets_ = torch.cat([targets, targets[:, :1]], dim = -1)
	targets_ = torch.stack([torch.full_like(targets_, blank), targets_], dim = -1).flatten(start_dim = -2)
	diff_labels = torch.cat([torch.as_tensor([[False, False]], device = targets.device).expand(len(B), -1), targets_[:, 2:] != targets_[:, :-2]], dim = 1)
	
	# if the -inf is used as neutral element, custom logsumexp must be used
	#zero = float('-inf')
	# to avoid nan grad in torch.logsumexp
	zero = torch.finfo(log_probs.dtype).min	

	zero, zero_padding = torch.tensor(zero, device = log_probs.device, dtype = log_probs.dtype), 2
	log_probs_ = log_probs.gather(-1, targets_.expand(len(log_probs), -1, -1))
	log_alpha = torch.full((len(log_probs), len(B), zero_padding + targets_.shape[-1]), zero, device = log_probs.device, dtype = log_probs.dtype)
	log_alpha[0, :, zero_padding + 0] = log_probs[0, :, blank]
	log_alpha[0, :, zero_padding + 1] = log_probs[0, B, targets_[:, 1]]
	# log_alpha[1:, :, zero_padding:] = log_probs.gather(-1, targets_.expand(len(log_probs), -1, -1))[1:]
	for t in range(1, len(log_probs)):
		log_alpha[t, :, 2:] = log_probs_[t] + logadd(log_alpha[t - 1, :, 2:], log_alpha[t - 1, :, 1:-1], torch.where(diff_labels, log_alpha[t - 1, :, :-2], zero))

	l1l2 = log_alpha[input_lengths - 1, B].gather(-1, torch.stack([zero_padding + target_lengths * 2 - 1, zero_padding + target_lengths * 2], dim = -1)) 
	loss = -torch.logsumexp(l1l2, dim = -1)
	if not alignment:
		return loss
	
	path = torch.zeros(len(log_probs), len(B), device = log_alpha.device, dtype = torch.int64)
	path[input_lengths - 1, B] = zero_padding + target_lengths * 2 - 1 + l1l2.argmax(dim = -1)
	for t, indices in reversed(list(enumerate(path))[1:]):
		indices_ = torch.stack([(indices - 2) * diff_labels[B, (indices - zero_padding).clamp(min = 0)], (indices - 1).clamp(min = 0), indices], dim = -1)
		path[t - 1] += (indices - 2 + log_alpha[t - 1, B].gather(-1, indices_).argmax(dim = -1)).clamp(min = 0)
	return torch.zeros_like(log_alpha).scatter_(-1, path.unsqueeze(-1), 1.0)[..., (zero_padding + 1)::2]

def ctc_alignment_targets(log_probs, targets, input_lengths, target_lengths, blank = 0, ctc_loss = F.ctc_loss, retain_graph = True):
	loss = ctc_loss(log_probs, targets, input_lengths, target_lengths, blank = blank, reduction = 'sum')
	probs = log_probs.exp()
	# to simplify API we inline log_softmax gradient, i.e. next two lines are equivalent to: grad_logits, = torch.autograd.grad(loss, logits, retain_graph = True). gradient formula explained at https://stackoverflow.com/questions/35304393/trying-to-understand-code-that-computes-the-gradient-wrt-to-the-input-for-logsof
	grad_log_probs, = torch.autograd.grad(loss, log_probs, retain_graph = retain_graph)
	grad_logits = grad_log_probs - probs * grad_log_probs.sum(dim = -1, keepdim = True)
	temporal_mask = (torch.arange(len(log_probs), device = input_lengths.device, dtype = input_lengths.dtype).unsqueeze(1) < input_lengths.unsqueeze(0)).unsqueeze(-1)
	return (probs * temporal_mask - grad_logits).detach()

def logadd(x0, x1, x2):
	# produces nan gradients in backward if -inf log-space zero element is used https://github.com/pytorch/pytorch/issues/31829
	return torch.logsumexp(torch.stack([x0, x1, x2]), dim = 0)
	
	# use if -inf log-space zero element is used
	#return LogsumexpFunction.apply(x0, x1, x2)
	
	# produces inplace modification error https://github.com/pytorch/pytorch/issues/31819
	#m = torch.max(torch.max(x0, x1), x2)
	#m = m.masked_fill(torch.isinf(m), 0)
	#res = (x0 - m).exp() + (x1 - m).exp() + (x2 - m).exp()
	#return res.log().add(m)

class LogsumexpFunction(torch.autograd.function.Function):
	@staticmethod
	def forward(self, x0, x1, x2):
		m = torch.max(torch.max(x0, x1), x2)
		m = m.masked_fill_(torch.isinf(m), 0)
		e0 = (x0 - m).exp_()
		e1 = (x1 - m).exp_()
		e2 = (x2 - m).exp_()
		e = (e0 + e1 + e2).clamp_(min = 1e-16)
		self.save_for_backward(e0, e1, e2, e)
		return e.log().add_(m)

	@staticmethod
	def backward(self, grad_output):
		e0, e1, e2, e = self.saved_tensors
		g = grad_output / e
		return g * e0, g * e1, g * e2
