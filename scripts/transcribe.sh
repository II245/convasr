python3 transcribe.py $@ \
  --checkpoint data/experiments/JasperNetBig_NovoGrad_lr1e-2_wd1e-3_bs256____fp16O2/checkpoint_epoch05_iter0040000.pt \
  -i data/echomsk/2020-01-21-status-2105.mp3 #data/kontur_fullrecs  #

#  --checkpoint data/speechcore/best_finetune_12022020.pt \

#  --data-path data/2019.10.19_calls --output-path data/2019.10.19_calls_ \

#  --decoder BeamSearchDecoder --beam-width 5000 --lm chats_05_prune.binary  #charlm/chats_06_noprune_char.binary # #--lm data/ru_wiyalen_no_punkt.arpa.binary 
#python3 transcribe.py \
#  --output-path data/5ZjeFwlRB.wav.transcribe \
#  --data-path data/5ZjeFwlRB.wav \
#  --checkpoint data/experiments/JasperNet_NovoGrad_lr1e-2_wd1e-3_bs80___jasperbig/checkpoint_epoch02_iter0062500.pt 
