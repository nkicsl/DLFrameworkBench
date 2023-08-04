import os
import json
import sys
import time
import argparse
import ctypes
import subprocess

import torch
from torch.utils.data import (DataLoader, TensorDataset)
from tqdm import *

sys.path.append('..')
from common.squad import (load_squad_features, RawResult, get_answers)
from src.config import config
from src.network import BertForQuestionAnswering, BertConfig
from src.lr_scheduler import LinearWarmUpScheduler


def parse_arg():
    parser = argparse.ArgumentParser()
    ## Required parameters
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                             "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model checkpoints and predictions will be written.")
    parser.add_argument("--predict_file", default=None, type=str, required=True,
                        help="SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json")
    parser.add_argument("--init_checkpoint", default=None, type=str, help="The checkpoint file from pretraining")
    parser.add_argument("--config_file", default=None, type=str, required=False, help="The BERT model config")

    ## Other parameters
    parser.add_argument("--train_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--max_seq_length", default=384, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences "
                             "longer than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--doc_stride", default=128, type=int,
                        help="When splitting up a long document into chunks, how much stride to take between chunks.")
    parser.add_argument("--max_query_length", default=64, type=int,
                        help="The maximum number of tokens for the question. Questions longer than this will "
                             "be truncated to this length.")
    parser.add_argument("--train_batch_size", default=32, type=int, help="Total batch size for training.")
    parser.add_argument("--predict_batch_size", default=8, type=int, help="Total batch size for predictions.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--n_best_size", default=20, type=int,
                        help="The total number of n-best predictions to generate in the nbest_predictions.json "
                             "output file.")
    parser.add_argument("--max_answer_length", default=30, type=int,
                        help="The maximum length of an answer that can be generated. This is needed because the start "
                             "and end predictions are not conditioned on one another.")
    parser.add_argument("--verbose_logging", action='store_true',
                        help="If true, all of the warnings related to data processing will be printed. "
                             "A number of warnings are expected for a normal SQuAD evaluation.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument('--version_2_with_negative',
                        action='store_true',
                        help='If true, the SQuAD examples contain some that do not have an answer.')
    parser.add_argument('--null_score_diff_threshold',
                        type=float, default=0.0,
                        help="If null_score - best_non_null is greater than the threshold predict null.")
    parser.add_argument("--eval_script",
                        help="Script to evaluate squad predictions",
                        default="evaluate-v1.1.py",
                        type=str)
    parser.add_argument('--disable-progress-bar',
                        default=False,
                        action='store_true',
                        help='Disable tqdm progress bar')
    parser.add_argument("--skip_cache",
                        default=False,
                        action='store_true',
                        help="Whether to cache train features")
    parser.add_argument("--cache_dir",
                        default=None,
                        type=str,
                        help="Location to cache train feaures. Will default to the dataset directory")
    parser.add_argument("--is_prof",
                        default=False,
                        action='store_true',
                        help="Whether to profile model.")
    args = parser.parse_args()
    return args

def main():
    
    args = parse_arg()
    print("---------configurations--------------")
    for k, v in vars(args).items():
        print(k,':',v)
    print("-------------------------------------")

    # make output dir if not exist
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    _cuda_tools_ext = ctypes.CDLL("libnvToolsExt.so")
    torch.backends.cudnn.benchmark = True
    
    # device
    device = torch.device("cuda", 0)

    # model
    if args.init_checkpoint:
        config_file = BertConfig.from_json_file(args.config_file)
        # Padding for divisibility by 8
        if config_file.vocab_size % 8 != 0:
            config_file.vocab_size += 8 - (config_file.vocab_size % 8)
        model = BertForQuestionAnswering(config_file)
        checkpoint = torch.load(args.init_checkpoint, map_location='cpu')
        checkpoint = checkpoint["model"] if "model" in checkpoint.keys() else checkpoint
        model.load_state_dict(checkpoint, strict=False)
        print(f"Load model from checkpoint: {args.init_checkpoint}")
    else:
        model = BertForQuestionAnswering.from_pretrained(args.bert_model)
        print(f"Load model from pretrained: {args.bert_model}")
    model = model.to(device)
    for name, param in list(model.named_parameters()):
        mean = param.mean()
        var = param.var()
        print(f"{name}: {mean:.6f}, {var:.6f}")

    # Train DataLoader
    _, train_features = load_squad_features(args, args.train_file, True)
    all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
    all_start_positions = torch.tensor([f.start_position for f in train_features], dtype=torch.long)
    all_end_positions = torch.tensor([f.end_position for f in train_features], dtype=torch.long)
    train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                               all_start_positions, all_end_positions)
    train_dataloader = DataLoader(train_data, batch_size=args.train_batch_size, shuffle=True,
                                  pin_memory=True, num_workers=config['dataset-num-workers'], persistent_workers=True)
    
    step_size = int(len(train_features) / args.train_batch_size)
    num_train_optimization_steps = step_size * args.num_train_epochs
    
    # Eval DataLoader
    eval_examples, eval_features = load_squad_features(args, args.predict_file, False)
    all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
    all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
    eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index)
    eval_dataloader = DataLoader(eval_data, batch_size=args.predict_batch_size, shuffle=False,
                                  pin_memory=True, num_workers=config['dataset-num-workers'], persistent_workers=True)

    # Optimizer
    assert config['optimizer-type'] == 'AdamW'
    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': config['weight-decay']},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    lr = config['lr-base'] * args.train_batch_size / config['lr-batch-denom']
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=lr, betas=(0.9, 0.999), eps=1e-6)

    # LR Scheduler
    scheduler = LinearWarmUpScheduler(optimizer, warmup=config['lr-warmup-proportion'], total_steps=num_train_optimization_steps)
    
    # Record the Total time for train
    total_train_time = 0
    for epoch in range(int(args.num_train_epochs)):
        # Log some infomations
        print(f"--------Epoch: {epoch:03}, " +
            f"lr: {optimizer.param_groups[0]['lr']:f}--------")
        # Training loop
        model.train()
        train_iter = tqdm(train_dataloader, desc="Iteration", disable=args.disable_progress_bar)
        start_time = time.time()
        for batch in train_iter:
            # Move to device
            batch = tuple(t.to(device) for t in batch)
            input_ids, input_mask, segment_ids, start_positions, end_positions = batch
            # Compute prediction and loss
            start_logits, end_logits = model(input_ids, segment_ids, input_mask)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)
            loss_fct = torch.nn.CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            loss = (start_loss + end_loss) / 2
            # Backpropagation
            optimizer.zero_grad()
            loss.backward()
            # Update (TODO: gradient clipping max_grad_norm=1.0)
            optimizer.step()
            scheduler.step()

        final_loss = loss.item()
        total_train_time += time.time() - start_time
        print(f"Train: Loss(last step): {final_loss:>.4e}, " + 
            f"Batch Time: {(time.time() - start_time) * 1e3 /step_size:>.2f}ms")

        if not args.is_prof:
            # Validation process
            model.eval()
            all_results = []
            eval_iter = tqdm(eval_dataloader, desc="Iteration", disable=args.disable_progress_bar)
            for batch in eval_iter:
                # Move to device
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, example_indices = batch
                # Forward computing
                with torch.no_grad():
                    batch_start_logits, batch_end_logits = model(input_ids, segment_ids, input_mask)
                for i, example_index in enumerate(example_indices):
                    start_logits = batch_start_logits[i].detach().cpu().tolist()
                    end_logits = batch_end_logits[i].detach().cpu().tolist()
                    eval_feature = eval_features[example_index.item()]
                    unique_id = int(eval_feature.unique_id)
                    all_results.append(RawResult(unique_id=unique_id,
                                            start_logits=start_logits,
                                            end_logits=end_logits))
            
            # Write results into output file
            answers, _ = get_answers(eval_examples, eval_features, all_results, args)
            output_prediction_file = args.output_dir + "/predictions.json"
            with open(output_prediction_file, "w") as f:
                f.write(json.dumps(answers, indent=4) + "\n")

            # Running the eval script
            eval_script = os.path.join(os.path.dirname(args.predict_file), args.eval_script)
            print('script is {}'.format(eval_script))
            eval_out = subprocess.check_output([sys.executable, eval_script,
                                                args.predict_file, output_prediction_file])
            scores = str(eval_out).strip()
            exact_match = float(scores.split(":")[1].split(",")[0])
            f1 = float(scores.split(":")[2].split(",")[0])
            print(f"Test: exact_match: {exact_match}, F1: {f1}")
        
    # Print the training time
    print("Time used: %.2fs" % (total_train_time))

if __name__ == "__main__":
    main()