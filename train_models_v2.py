from transformers import DataCollatorWithPadding, DataCollatorForSeq2Seq, Trainer, TrainingArguments, Seq2SeqTrainer, Seq2SeqTrainingArguments, LlamaTokenizer
from peft import get_peft_config, get_peft_model, prepare_model_for_int8_training, get_peft_model_state_dict, LoraConfig, TaskType
from sklearn.metrics import classification_report
from datasets import Dataset, DatasetDict
from argparse import ArgumentParser
from torch.utils.data import DataLoader
from time import time
from tqdm import tqdm
import pandas as pd
import numpy as np
import evaluate
import json
import os
import torch
import wandb

from util_modeling import get_model_objects, is_language_model, is_large_language_model
from util_icl import generate_classification_prompt, get_static_exemplars
from util_data import get_formatted_dataset
from adaptive_methods import GenericDataset

# Set global tokenizer for computing metrics
GLOBAL_TOKENIZER = None


def get_dataset(dataset_name, max_examples):
    local_dataset_paths = {
        "boss_sentiment": {
            "train": "datasets/boss_benchmark/SentimentAnalysis/amazon/train.tsv",
            "test": "datasets/boss_benchmark/SentimentAnalysis/amazon/test.tsv",
        },
        "boss_toxicity": {
            "train": "datasets/boss_benchmark/ToxicDetection/civil_comments/train.tsv",
            "test": "datasets/boss_benchmark/ToxicDetection/civil_comments/test.tsv",
        },
    }

    if dataset_name in local_dataset_paths:
        train_set = pd.read_csv(local_dataset_paths[dataset_name]["train"], sep="\t").dropna()
        train_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
        if max_examples is not None:
            train_set = train_set.sample(max_examples)

        test_set = pd.read_csv(local_dataset_paths[dataset_name]["test"], sep="\t").dropna()
        test_set.rename(columns={"Text": "text", "Label": "label"}, inplace=True)
        if max_examples is not None:
            test_set = test_set.sample(max_examples)

        return DatasetDict(
            {
                "train": Dataset.from_pandas(train_set),
                "test": Dataset.from_pandas(test_set),
            }
        )

    # return load_dataset(dataset_name)
    dataset = get_formatted_dataset(dataset_name, max_examples=None)
    if dataset_name == "sst2":
        dataset["test"] = dataset["validation"]

    if max_examples is not None:
        dataset["train"] = dataset["train"].select(range(max_examples)) if max_examples < len(dataset["train"]) else dataset["train"]
        dataset["test"] = dataset["test"].select(range(max_examples)) if max_examples < len(dataset["test"]) else dataset["test"]

    return dataset


def compute_metrics(eval_preds):
    predictions = None
    labels = None
    if not isinstance(eval_preds.label_ids[0], int):
        tokenizer = LlamaTokenizer.from_pretrained("decapoda-research/llama-7b-hf")
        raw_predictions = [tokenizer.decode(word_dist.argmax(-1)).split("Label:")[-1].split("\n")[0].lower().strip() for word_dist in eval_preds.predictions]
        labels = [int(tokenizer.decode([id for id in label_ids if id > 0])[-1]) for label_ids in eval_preds.label_ids]
        verbalizers = {
            "pos": 1,
            "positive": 1,
            "1": 1,
            "neg": 0,
            "negative": 0,
            "0": 0,
            "neutral": 2,
            "neut": 2,
            "toxic": 1,
            "non-toxic": 0,
            "word": 0,
            "sports": 1,
            "business": 2,
            "sci/tech": 3,
        }
        predictions = []
        for pred in raw_predictions:
            if pred in verbalizers:
                predictions.append(verbalizers[pred])
            else:
                try:
                    predictions.append(int(pred))
                except:
                    predictions.append(-1)
    else:
        logits, labels = eval_preds
        predictions = np.argmax(logits, axis=-1)

    report = classification_report(labels, predictions, output_dict=True)
    return {"eval_f1": report["macro avg"]["f1-score"], "eval_acc": report["accuracy"]}


def tokenize_t5(example, tokenizer):
    inputs = example["text"]
    labels = example["label"]
    source_encodings = tokenizer(inputs, truncation=True, max_length=512)
    target_encodings = tokenizer([str(label) for label in labels], truncation=True, max_length=512)
    source_encodings["label"] = target_encodings["input_ids"]
    return source_encodings


def tokenize_llm(example, tokenizer, dataset_name):
    entries = zip(example["text"], example["label"])
    prompts = [f"{generate_classification_prompt(entry[0], [], None, dataset_name)}{entry[1]}{tokenizer.eos_token}" for entry in entries]
    tokenized_input = tokenizer(prompts)
    tokenized_input["labels"] = tokenized_input["input_ids"].copy()
    return tokenized_input


def fine_tune_model():
    args = get_cli_args()
    num_epochs = 5
    dataset_name = args.dataset
    model_name = args.base_model

    experiment_id = f"training_{int(time())}_{args.dataset}_{args.base_model.replace('/', '_')}"
    create_exp_dir(args, experiment_id)

    wandb_run = None
    if args.use_wandb:
        project_name = "In-Context Domain Transfer Improves Out-of-Domain Robustness"
        wandb_run = wandb.init(project=project_name, group="training", name=experiment_id, config=args)

    dataset = get_dataset(dataset_name, args.max_examples)
    tokenizer, model = get_model_objects(model_name, num_labels=args.num_labels, training=True)
    GLOBAL_TOKENIZER = tokenizer
    data_collator = DataCollatorForSeq2Seq(tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True)
    if is_large_language_model(model_name):
        peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1)
        model = prepare_model_for_int8_training(model)
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()

    tokenized_datasets = None
    if is_large_language_model(model_name):
        tokenized_datasets = dataset.map(lambda example: tokenize_llm(example, tokenizer, dataset_name), batched=True)
    elif is_language_model(model_name):
        tokenized_datasets = dataset.map(lambda example: tokenize_t5(example, tokenizer), batched=True, remove_columns=["text"])
    else:
        tokenized_datasets = dataset.map(lambda example: tokenizer(example["text"], truncation=True, max_length=512), batched=True, remove_columns=["text"])

    trainer = None
    # if is_language_model(model_name):
    #     training_args = Seq2SeqTrainingArguments(
    #         output_dir=f"trained_models/{experiment_id}/model",
    #         per_device_train_batch_size=32,
    #         num_train_epochs=num_epochs,
    #         warmup_ratio=0.1,
    #         weight_decay=0.01,
    #         learning_rate=2e-5,
    #         logging_dir=f"trained_models/{experiment_id}/logs",
    #         metric_for_best_model="eval_f1",
    #         evaluation_strategy="epoch",
    #         save_strategy="epoch",
    #         load_best_model_at_end=True,
    #     )
    #     trainer = Seq2SeqTrainer(
    #         model, training_args, train_dataset=tokenized_datasets["train"], eval_dataset=tokenized_datasets["test"], data_collator=data_collator, tokenizer=tokenizer, compute_metrics=compute_metrics
    #     )
    # else:
    tokenized_datasets = tokenized_datasets.remove_columns(dataset["train"].column_names)
    training_args = TrainingArguments(
        output_dir=f"trained_models/{experiment_id}/model",
        per_device_train_batch_size=32,
        num_train_epochs=num_epochs,
        warmup_ratio=0.1,
        weight_decay=0.01,
        learning_rate=3e-4 if is_language_model(model_name) else 2e-5,
        logging_dir=f"trained_models/{experiment_id}/logs",
        metric_for_best_model="eval_f1",
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        run_name=experiment_id,
        report_to="wandb",
    )
    if is_large_language_model(model_name):
        # training_args.gradient_accumulation_steps = 16
        training_args.per_device_train_batch_size = 128
        training_args.fp16 = True
    if args.use_wandb:
        training_args.wandb_project = project_name
        training_args.run_name = experiment_id

    trainer = Trainer(
        model, training_args, train_dataset=tokenized_datasets["train"], eval_dataset=tokenized_datasets["test"], data_collator=data_collator, tokenizer=tokenizer, compute_metrics=compute_metrics
    )
    model.config.use_cache = False
    old_state_dict = model.state_dict
    model.state_dict = (lambda self, *_, **__: get_peft_model_state_dict(self, old_state_dict())).__get__(model, type(model))
    model = torch.compile(model)
    trainer.train()

    # Save best model and tokenizer to its own directory
    trainer.save_model(f"trained_models/{experiment_id}/best_F1={trainer.state.best_metric}")


def get_cli_args():
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--num_labels", type=int, required=True)
    parser.add_argument("--base_model", type=str, required=False, default="bert-base-uncased")
    parser.add_argument("--max_examples", type=int, required=False, default=None)
    parser.add_argument("--use_wandb", action="store_true")
    args = parser.parse_args()
    return args


def create_exp_dir(args, experiment_id):
    if not os.path.exists("trained_models"):
        os.mkdir("trained_models")
    os.mkdir(f"trained_models/{experiment_id}")
    json.dump(vars(args), open(f"trained_models/{experiment_id}/config.json", "w"), indent=4)


if __name__ == "__main__":
    fine_tune_model()
