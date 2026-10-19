
common_config = {
    'data_dir': './datasets/',
    'img_width': 100,
    'img_height': 32,
    'map_to_seq_hidden': 64,
    'rnn_hidden': 256,
    'leaky_relu': False,
}

train_config = {
    'epochs': 10000,
    'train_batch_size': 16,
    'eval_batch_size': 16,
    'lr': 0.0001,
    'show_interval': 10,
    'valid_interval': 500,
    'save_interval': 2000,
    'cpu_workers': 4,
    'reload_checkpoint': 'checkpoints/crnn_synth90k.pt',
    'valid_max_iter': 100,
    'decode_method': 'greedy',
    'beam_size': 10,
    'checkpoints_dir': 'checkpoints/'
}
train_config.update(common_config)

evaluate_config = {
    'eval_batch_size': 8,
    'cpu_workers': 4,
    # 'reload_checkpoint': 'checkpoints/crnn_synth90k.pt',
    'decode_method': 'greedy',
    'beam_size': 10,
}
evaluate_config.update(common_config)
