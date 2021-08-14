import argparse
import csv
import random
import sys
import urllib.request
from math import ceil
from typing import Tuple

import tensorflow as tf
from transformers import AutoTokenizer, TFAutoModel

from transformers_tf_finetune.losses import PearsonCorrelationLoss
from transformers_tf_finetune.metrics import (
    BinaryF1Score,
    PearsonCorrelationMetric,
    SpearmanCorrelationMetric,
    pearson_correlation_coefficient,
    spearman_correlation_coefficient,
)
from transformers_tf_finetune.models import SemanticTextualSimailarityWrapper
from transformers_tf_finetune.utils import LRScheduler, get_device_strategy, get_logger, path_join, set_random_seed

# fmt: off
KORSTS_TRAIN_URI = "https://raw.githubusercontent.com/kakaobrain/KorNLUDatasets/master/KorSTS/sts-train.tsv"
KORSTS_DEV_URI = "https://raw.githubusercontent.com/kakaobrain/KorNLUDatasets/master/KorSTS/sts-dev.tsv"
KORSTS_TEST_URI = "https://raw.githubusercontent.com/kakaobrain/KorNLUDatasets/master/KorSTS/sts-test.tsv"

parser = argparse.ArgumentParser(description="Script to train KorSTS Task with BART")
parser.add_argument("--pretrained-model", type=str, required=True, help="transformers pretrained path")
parser.add_argument("--pretrained-tokenizer", type=str, required=True, help="pretrained tokenizer fast pretrained path")
parser.add_argument("--train-dataset-path", default=KORSTS_TRAIN_URI, help="kor sts train dataset if using local file")
parser.add_argument("--dev-dataset-path", default=KORSTS_DEV_URI, help="kor sts dev dataset if using local file")
parser.add_argument("--test-dataset-path", default=KORSTS_TEST_URI, help="kor sts test dataset if using local file")
parser.add_argument("--output-path", default="output", help="output directory to save log and model checkpoints")
parser.add_argument("--epochs", type=int, default=5)
parser.add_argument("--learning-rate", type=float, default=5e-5)
parser.add_argument("--min-learning-rate", type=float, default=1e-5)
parser.add_argument("--warmup-rate", type=float, default=0.06)
parser.add_argument("--warmup-steps", type=int)
parser.add_argument("--batch-size", type=int, default=128)
parser.add_argument("--dev-batch-size", type=int, default=512)
parser.add_argument("--tensorboard-update-freq", type=int, default=1)
parser.add_argument("--mixed-precision", action="store_true", help="Use mixed precision FP16")
parser.add_argument("--seed", type=int, help="Set random seed")
parser.add_argument("--device", type=str, default="CPU", choices=["CPU", "GPU", "TPU"], help="device to use (TPU or GPU or CPU)")
parser.add_argument("--use-auth-token", action="store_true", help="use huggingface-cli credential for private model")
parser.add_argument("--from-pytorch", action="store_true", help="load from pytorch weight")
# fmt: on


def load_dataset(dataset_path: str, tokenizer: AutoTokenizer, shuffle: bool = False) -> tf.data.Dataset:
    """
    Load KorSTS dataset from local file or web

    :param dataset_path: local file path or file uri
    :param tokenizer: PreTrainedTokenizer for tokenizing
    :param shuffle: whether shuffling lines or not
    :returns: KorSTS dataset, number of dataset
    """
    if dataset_path.startswith("https://"):
        with urllib.request.urlopen(dataset_path) as response:
            data = response.read().decode("utf-8")
    else:
        with open(dataset_path) as f:
            data = f.read()
    lines = data.splitlines()[1:]
    if shuffle:
        random.shuffle(lines)

    bos = tokenizer.bos_token or tokenizer.cls_token
    eos = tokenizer.eos_token or tokenizer.sep_token

    sentences1 = []
    sentences2 = []
    normalized_labels = []
    for *_, score, sentence1, sentence2 in csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE):
        sentences1.append(bos + sentence1 + eos)
        sentences2.append(bos + sentence2 + eos)
        normalized_labels.append(float(score) / 5.0)

    inputs1 = dict(
        tokenizer(
            sentences1,
            padding=True,
            return_tensors="tf",
            return_token_type_ids=False,
            return_attention_mask=True,
        )
    )
    inputs2 = dict(
        tokenizer(
            sentences2,
            padding=True,
            return_tensors="tf",
            return_token_type_ids=False,
            return_attention_mask=True,
        )
    )

    dataset = tf.data.Dataset.from_tensor_slices(((inputs1, inputs2), normalized_labels))
    return dataset


def main(args: argparse.Namespace):
    logger = get_logger(__name__)

    if args.seed:
        set_random_seed(args.seed)
        logger.info(f"Set random seed to {args.seed}")

    # Copy config file
    assert not tf.io.gfile.exists(args.output_path), f'output path: "{args.output_path}" is already exists!'
    tf.io.gfile.makedirs(args.output_path)
    with tf.io.gfile.GFile(path_join(args.output_path, "argument_configs.txt"), "w") as fout:
        for k, v in vars(args).items():
            fout.write(f"{k}: {v}\n")

    with get_device_strategy(args.device).scope():
        if args.mixed_precision:
            logger.info("Use Mixed Precision FP16")
            mixed_type = "mixed_bfloat16" if args.device == "TPU" else "mixed_float16"
            policy = tf.keras.mixed_precision.experimental.Policy(mixed_type)
            tf.keras.mixed_precision.experimental.set_policy(policy)

        logger.info("[+] Load Tokenizer")
        tokenizer = AutoTokenizer.from_pretrained(args.pretrained_tokenizer, use_auth_token=args.use_auth_token)

        # Construct Dataset
        logger.info("[+] Load Datasets")
        train_dataset = load_dataset(args.train_dataset_path, tokenizer, True)
        train_dataset = train_dataset.batch(args.batch_size)
        dev_dataset = load_dataset(args.dev_dataset_path, tokenizer).batch(args.dev_batch_size)
        test_dataset = load_dataset(args.test_dataset_path, tokenizer).batch(args.dev_batch_size)

        # Model Initialize
        logger.info("[+] Model Initialize")
        model = TFAutoModel.from_pretrained(
            args.pretrained_model, use_auth_token=args.use_auth_token, from_pt=args.from_pytorch
        )
        model_sts = SemanticTextualSimailarityWrapper(model=model, embedding_dropout=0.1)

        # Model Compile
        logger.info("[+] Model compiling complete")
        model_sts.compile(
            optimizer=tf.keras.optimizers.Adam(
                LRScheduler(
                    len(train_dataset) * args.epochs,
                    args.learning_rate,
                    args.min_learning_rate,
                    args.warmup_rate,
                    args.warmup_steps,
                ),
            ),
            loss=[PearsonCorrelationLoss(), tf.keras.losses.MeanSquaredError()],
            loss_weights=[0.25, 0.75],
            metrics=[
                BinaryF1Score(),
                PearsonCorrelationMetric(name="pearson_coef"),
                SpearmanCorrelationMetric(name="spearman_coef"),
            ],
        )

        # Training
        logger.info("[+] Start training")
        checkpoint_path = path_join(args.output_path, "best_model.ckpt")
        model_sts.fit(
            train_dataset,
            validation_data=dev_dataset,
            epochs=args.epochs,
            callbacks=[
                tf.keras.callbacks.ModelCheckpoint(
                    checkpoint_path,
                    save_weights_only=True,
                    save_best_only=True,
                    monitor="val_spearman_coef",
                    mode="max",
                    verbose=1,
                ),
                tf.keras.callbacks.TensorBoard(
                    path_join(args.output_path, "logs"), update_freq=args.tensorboard_update_freq
                ),
            ],
        )
        logger.info("[+] Load and Save Best Model")
        model_sts.load_weights(checkpoint_path)
        model_sts.model.save_pretrained(path_join(args.output_path, "pretrained_model"))

        logger.info("[+] Start testing")
        preds = []
        labels = []
        f1 = BinaryF1Score()
        for inputs, label in test_dataset:
            pred = model_sts(inputs, training=False)
            preds.extend(pred.numpy())
            labels.extend(label.numpy())
            f1.update_state(label, pred)

        pearson_score = pearson_correlation_coefficient(labels, preds)
        spearman_score = spearman_correlation_coefficient(labels, preds)
        logger.info(
            f"[+] Dev F1 Score: {f1.result():.4f}, "
            f"Dev Pearson: {pearson_score:.4f}, "
            f"Dev Spearman: {spearman_score:.4f}"
        )


if __name__ == "__main__":
    sys.exit(main(parser.parse_args()))
