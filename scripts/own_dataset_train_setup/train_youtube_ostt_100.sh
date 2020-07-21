python3 train.py $@ \
  --githttp https://github.com/vadimkantorov/convasr/commit/%h \
  --verbose --lang ru \
  --model JasperNetBig \
  --train-batch-size 256 --val-batch-size 128 \
  --scheduler MultiStepLR --decay-milestones 30000 40000 \
  --lr 1e-2 \
  --optimizer NovoGrad \
  --train-data-path data/splits/youtube_100h_train.csv.json \
  --val-data-path data/clean_val.csv.json data/mixed_val.csv.json kontur_calls_micro/kontur_calls_micro.csv.json kontur_calls_micro/kontur_calls_micro.0.csv.json kontur_calls_micro/kontur_calls_micro.1.csv.json youtube/cut/cut_microval.json data/splits/youtube_100h_val.csv.json \
  --analyze kontur_calls_micro.csv \
  --val-iteration-interval 2500 \
  --fp16 O2 \
  --experiment-name youtube_osst_100h \
  --epochs 150 --exphtml= #\

